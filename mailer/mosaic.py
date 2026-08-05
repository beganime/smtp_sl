import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from django.core.mail import EmailMessage, get_connection
from django.template.loader import render_to_string
from django.utils import timezone

from .models import MosaicMonitorConfiguration, MosaicMonitorEvent


USER_AGENT = 'SMTP_SL Mosaic appointment monitor/1.0 (+https://tmmail.ru/)'
DATE_RE = re.compile(r'^\d{1,2}\s+[A-Za-z]+\s+\d{4}$')
AVAILABLE_RE = re.compile(r'\b(available|book\s+now|reserve\s+now|select|choose)\b', re.IGNORECASE)
UNAVAILABLE_RE = re.compile(
    r'\b(no\s+(?:appointments?|slots?)\s+available|not\s+available|unavailable|'
    r'fully\s+booked|sold\s+out|closed)\b',
    re.IGNORECASE,
)


def _clean_text(value):
    return ' '.join((value or '').replace('\xa0', ' ').split())


class CalendarHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.in_h3 = False
        self.cells = []
        self.cell_text = []
        self.cell_actionable = False
        self.cell_links = []
        self.rows = []
        self.headings = []
        self.heading_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            self.in_table = True
        elif tag == 'tr' and self.in_table:
            self.in_row = True
            self.cells = []
        elif tag in ('td', 'th') and self.in_row:
            self.in_cell = True
            self.cell_text = []
            self.cell_actionable = False
            self.cell_links = []
        elif self.in_cell and tag in ('a', 'button', 'form'):
            self.cell_actionable = True
            if tag == 'a' and attrs.get('href'):
                self.cell_links.append(attrs['href'])
        elif self.in_cell and tag == 'input' and attrs.get('type', '').casefold() in ('submit', 'button'):
            self.cell_actionable = True
        if tag == 'h3':
            self.in_h3 = True
            self.heading_text = []

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.in_cell:
            self.cells.append({
                'text': _clean_text(' '.join(self.cell_text)),
                'actionable': self.cell_actionable,
                'links': sorted(set(self.cell_links)),
            })
            self.in_cell = False
        elif tag == 'tr' and self.in_row:
            if len(self.cells) >= 2 and DATE_RE.match(self.cells[0]['text']):
                date_text = self.cells[0]['text']
                status = self.cells[1]['text']
                actionable = self.cells[1]['actionable']
                self.rows.append({
                    'date': date_text,
                    'status': status,
                    'actionable': actionable,
                    'available': bool(
                        actionable
                        or (
                            AVAILABLE_RE.search(status)
                            and not UNAVAILABLE_RE.search(status)
                            and not status.casefold().startswith('reserved')
                        )
                    ),
                    'links': self.cells[1]['links'],
                })
            self.in_row = False
            self.cells = []
        elif tag == 'table':
            self.in_table = False
        if tag == 'h3' and self.in_h3:
            self.headings.append(_clean_text(' '.join(self.heading_text)))
            self.in_h3 = False

    def handle_data(self, data):
        if self.in_cell:
            self.cell_text.append(data)
        if self.in_h3:
            self.heading_text.append(data)


def parse_calendar_html(html):
    parser = CalendarHTMLParser()
    parser.feed(html)
    headings = [value for value in parser.headings if value]
    return {
        'title': headings[0] if headings else 'Mosaic Visa',
        'month_title': headings[1] if len(headings) > 1 else '',
        'rows': parser.rows,
    }


def _add_months(value, count):
    month_index = value.year * 12 + value.month - 1 + count
    return date(month_index // 12, month_index % 12 + 1, 1)


def _url_for_month(url, month):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['month'] = month.strftime('%Y-%m')
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_calendar_snapshot(config, http_session=None):
    urls = config.calendar_url_list()
    if not urls:
        raise ValueError('Не указаны страницы календарей Mosaic Visa.')
    months_ahead = max(0, min(int(config.months_ahead), 12))
    first_month = timezone.localdate().replace(day=1)
    months = [_add_months(first_month, offset) for offset in range(months_ahead + 1)]
    session = http_session or requests.Session()
    snapshot = {
        'version': 1,
        'fetched_at': timezone.now().isoformat(),
        'calendars': {},
    }
    for base_url in urls:
        calendar_pages = {}
        for month in months:
            month_key = month.strftime('%Y-%m')
            page_url = _url_for_month(base_url, month)
            response = session.get(
                page_url,
                headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en'},
                timeout=(10, 25),
            )
            response.raise_for_status()
            parsed = parse_calendar_html(response.text)
            if not parsed['rows']:
                raise RuntimeError(f'На странице {page_url} не найден календарь с датами.')
            calendar_pages[month_key] = {
                'url': page_url,
                'title': parsed['title'],
                'month_title': parsed['month_title'],
                'rows': parsed['rows'],
            }
        snapshot['calendars'][base_url] = calendar_pages
    snapshot['digest'] = _snapshot_digest(snapshot)
    return snapshot


def _snapshot_digest(snapshot):
    relevant = snapshot.get('calendars', {})
    payload = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def compare_snapshots(previous, current):
    previous_rows = _flatten_rows(previous)
    current_rows = _flatten_rows(current)
    changes = []
    opened = []
    for key in sorted(set(previous_rows) | set(current_rows)):
        before = previous_rows.get(key)
        after = current_rows.get(key)
        base = {
            'calendar': key[0],
            'month': key[1],
            'date': key[2],
            'title': (after or before).get('title', 'Mosaic Visa'),
            'links': list((after or {}).get('links') or []),
        }
        if before is None:
            change = {**base, 'kind': 'added', 'before': '', 'after': _row_label(after)}
        elif after is None:
            change = {**base, 'kind': 'removed', 'before': _row_label(before), 'after': ''}
        elif _row_signature(before) != _row_signature(after):
            change = {
                **base,
                'kind': 'changed',
                'before': _row_label(before),
                'after': _row_label(after),
            }
        else:
            continue
        changes.append(change)
        if after and after.get('available') and not (before or {}).get('available'):
            opened.append({
                **change,
                'booking_url': urljoin(key[0], (after.get('links') or [''])[0]) or key[0],
            })
    return {'changes': changes, 'opened': opened}


def _flatten_rows(snapshot):
    result = {}
    for calendar, pages in (snapshot or {}).get('calendars', {}).items():
        for month, page in pages.items():
            for row in page.get('rows', []):
                result[(calendar, month, row['date'])] = {
                    **row,
                    'title': page.get('title') or 'Mosaic Visa',
                }
    return result


def _row_signature(row):
    return (
        row.get('status', ''),
        bool(row.get('actionable')),
        bool(row.get('available')),
        tuple(row.get('links') or []),
    )


def _row_label(row):
    if not row:
        return 'нет строки'
    status = row.get('status') or 'без статуса'
    if row.get('available'):
        status += ' · ДОСТУПНА ЗАПИСЬ'
    return status


def _smtp_connection(config):
    if not config.password_is_set:
        raise ValueError('Не сохранён пароль приложения почты отправителя.')
    return get_connection(
        backend='django.core.mail.backends.smtp.EmailBackend',
        host='smtp.gmail.com',
        port=465,
        username=config.sender_email,
        password=config.get_sender_password(),
        use_ssl=True,
        use_tls=False,
        timeout=25,
    )


def _send_email(config, subject, body):
    recipients = config.recipient_list()
    if not recipients:
        raise ValueError('Не указан ни один получатель уведомлений.')
    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=config.sender_email,
        to=recipients,
        connection=_smtp_connection(config),
    )
    message.send(fail_silently=False)
    return recipients


def send_mosaic_test_email(config=None):
    config = config or MosaicMonitorConfiguration.load()
    body = render_to_string('mailer/mosaic_test_email.txt', {
        'config': config,
        'recipients': config.recipient_list(),
        'calendar_urls': config.calendar_url_list(),
        'sent_at': timezone.localtime(),
    })
    recipients = _send_email(
        config,
        f'{config.subject_prefix} Тест мониторинга очереди',
        body,
    )
    MosaicMonitorEvent.objects.create(
        event_type='test',
        summary=f'Тестовое письмо отправлено: {", ".join(recipients)}',
        notification_sent=True,
    )
    _trim_events()
    return recipients


def check_mosaic_calendars(config=None, notify=True):
    config = config or MosaicMonitorConfiguration.load()
    checked_at = timezone.now()
    try:
        current = fetch_calendar_snapshot(config)
        previous = config.last_snapshot or {}
        if not previous:
            config.last_snapshot = current
            config.last_checked_at = checked_at
            config.last_error = ''
            config.save(update_fields=('last_snapshot', 'last_checked_at', 'last_error', 'updated_at'))
            MosaicMonitorEvent.objects.create(
                event_type='baseline',
                summary='Создан первый снимок календарей без отправки уведомления.',
            )
            _trim_events()
            return {'baseline': True, 'changed': False, 'changes': [], 'opened': []}

        comparison = compare_snapshots(previous, current)
        changed = bool(comparison['changes'])
        has_openings = bool(comparison['opened'])
        if has_openings and notify:
            body = render_to_string('mailer/mosaic_change_email.txt', {
                'config': config,
                'opened': comparison['opened'][:100],
                'calendar_urls': config.calendar_url_list(),
                'checked_at': timezone.localtime(checked_at),
            })
            recipients = _send_email(
                config,
                _opening_subject(config, comparison['opened']),
                body,
            )
            config.last_notification_at = checked_at
            notification_sent = True
        else:
            recipients = []
            notification_sent = False

        config.last_snapshot = current
        config.last_checked_at = checked_at
        config.last_error = ''
        update_fields = ['last_snapshot', 'last_checked_at', 'last_error', 'updated_at']
        if changed:
            config.last_changed_at = checked_at
            update_fields.append('last_changed_at')
        if notification_sent:
            update_fields.append('last_notification_at')
        config.save(update_fields=update_fields)
        if changed:
            details = _format_event_details(comparison)
            MosaicMonitorEvent.objects.create(
                event_type='opening' if has_openings else 'change',
                summary=(
                    f'Открыта запись: {len(comparison["opened"])} '
                    f'{_plural_dates(len(comparison["opened"]))}.'
                    if has_openings else
                    'Календарь обновился. Новых открытых дат нет.'
                ),
                details=details,
                notification_sent=notification_sent,
            )
            _trim_events()
        return {
            'baseline': False,
            'changed': changed,
            'changes': comparison['changes'],
            'opened': comparison['opened'],
            'recipients': recipients,
        }
    except Exception as exc:
        error = str(exc)[:2000]
        previous_error = config.last_error
        config.last_checked_at = checked_at
        config.last_error = error
        config.save(update_fields=('last_checked_at', 'last_error', 'updated_at'))
        if error != previous_error:
            MosaicMonitorEvent.objects.create(
                event_type='error',
                summary='Ошибка проверки календарей Mosaic Visa.',
                details=error,
            )
            _trim_events()
        raise


def _format_change_line(item):
    arrow = {
        'added': 'добавлено',
        'removed': 'удалено',
        'changed': 'изменено',
    }.get(item['kind'], item['kind'])
    return (
        f'{item["title"]} · {item["date"]}: {arrow}; '
        f'{item["before"] or "—"} → {item["after"] or "—"}'
    )


def _opening_subject(config, opened):
    titles = []
    for item in opened:
        title = item.get('title') or 'Mosaic Visa'
        if title not in titles:
            titles.append(title)
    place = ', '.join(titles[:3])
    suffix = f' — {place}' if place else ''
    return f'{config.subject_prefix} ОТКРЫТА ЗАПИСЬ{suffix}'


def _plural_dates(count):
    if count % 10 == 1 and count % 100 != 11:
        return 'дата'
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return 'даты'
    return 'дат'


def _format_event_details(comparison):
    lines = []
    if comparison['opened']:
        lines.append('ОТКРЫТА ЗАПИСЬ:')
        for item in comparison['opened'][:100]:
            lines.append(
                f'- {item["title"]} · {item["date"]}: '
                f'{item["after"]}; {item.get("booking_url") or item["calendar"]}'
            )
    grouped = {}
    for item in comparison['changes']:
        title = item.get('title') or 'Mosaic Visa'
        counters = grouped.setdefault(title, {'added': 0, 'removed': 0, 'changed': 0})
        counters[item['kind']] = counters.get(item['kind'], 0) + 1
    if grouped:
        if lines:
            lines.append('')
        lines.append('СВОДКА ИЗМЕНЕНИЙ:')
        for title, counters in grouped.items():
            lines.append(
                f'- {title}: добавлено {counters["added"]}, '
                f'удалено {counters["removed"]}, изменено {counters["changed"]}.'
            )
    return '\n'.join(lines)


def _trim_events(limit=200):
    old_ids = list(
        MosaicMonitorEvent.objects.order_by('-created_at').values_list('pk', flat=True)[limit:]
    )
    if old_ids:
        MosaicMonitorEvent.objects.filter(pk__in=old_ids).delete()
