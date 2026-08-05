import logging
import json
import re
from datetime import timedelta

import requests
import urllib3
from django.conf import settings
from django.utils import timezone

from .mail_content import html_to_text
from .models import InboundMessage


logger = logging.getLogger('mailer')
TELEGRAM_MESSAGE_LIMIT = 4096
UNIVERSITY_DOMAIN_SUFFIXES = (
    '.edu', '.edu.ru', '.ac.ru', '.edu.tm', '.edu.tr', '.ac.uk',
)
KNOWN_UNIVERSITY_DOMAINS = {
    'unn.ru', 'etu.ru', 'spbu.ru', 'dvfu.ru', 'mai.ru', 'kantiana.ru',
    'bsmu.by', 'bashgmu.ru', 'sechenov.ru', 'msmu.ru', 'mirea.ru',
    'misis.ru', 'hse.ru', 'rudn.ru', 'kpfu.ru', 'knitu.ru', 'kstu.ru',
    'bmstu.ru', 'mipt.ru', 'msu.ru', 'sgmu.ru', 'samsmu.ru',
}
PUBLIC_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yandex.ru', 'yandex.com', 'ya.ru',
    'mail.ru', 'inbox.ru', 'list.ru', 'bk.ru', 'internet.ru',
    'outlook.com', 'hotmail.com', 'live.com', 'icloud.com', 'me.com',
    'yahoo.com', 'proton.me', 'protonmail.com',
}
SERVICE_SENDER_RE = re.compile(
    r'(?:^|[@. _-])(?:google|youtube|market|gosuslugi|facebook|'
    r'instagram|linkedin|tiktok|telegram|pinterest|twitter|netflix|spotify)'
    r'(?:[.@ _-]|$)',
    re.IGNORECASE,
)
UNIVERSITY_IDENTITY_RE = re.compile(
    r'(университет|university|институт|institute|академи|college|'
    r'при[её]мн\w*\s+комисс|admission|'
    r'\b(?:бгму|маи|лэти|бфу|спбгу|книту|каи|двфу|мгу|мгту|'
    r'ргму|сгму|кфу|рудн|вшэ|мисис|мфти|ранхигс)\b)',
    re.IGNORECASE,
)
UNIVERSITY_CONTENT_RE = re.compile(
    r'(университет|university|вуз|институт|академи|при[её]мн\w*\s+комисс|'
    r'абитуриент|поступлен|зачислен|вступительн\w+\s+(?:испытан|экзамен)|'
    r'личн\w+\s+кабинет\w*\s+абитуриент)',
    re.IGNORECASE,
)

UNIVERSITY_TOPIC_RULES = (
    ('bgmu', ('bashgmu.ru', 'bsmu.by'), ('бгму', 'bashkir state medical', 'belarusian state medical')),
    ('mephi', ('mephi.ru', 'mifi.ru'), ('мифи', 'mephi', 'national research nuclear university')),
    ('dvfu', ('dvfu.ru',), ('двфу', 'dvfu', 'far eastern federal')),
    ('mai', ('mai.ru',), ('маи', 'moscow aviation institute')),
    ('rudn', ('rudn.ru',), ('рудн', 'rudn', 'peoples friendship university')),
    ('lobachevsky', ('unn.ru',), ('лобачевск', 'lobachevsky', 'ниу ннгу', 'ннгу')),
)


class TelegramDeliveryError(RuntimeError):
    def __init__(self, errors, delivered_chat_ids=None):
        self.errors = errors
        self.delivered_chat_ids = delivered_chat_ids or []
        super().__init__('; '.join(errors))


def telegram_is_configured():
    return bool(
        settings.TELEGRAM_NOTIFICATIONS_ENABLED
        and settings.TELEGRAM_BOT_TOKEN
        and telegram_chat_ids()
    )


def telegram_chat_ids():
    raw = getattr(settings, 'TELEGRAM_CHAT_IDS', '') or settings.TELEGRAM_CHAT_ID
    result = []
    for value in raw.replace(';', ',').replace(' ', ',').split(','):
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result


def telegram_forum_topics():
    """Parse ``key:thread_id`` pairs used for Telegram forum routing."""
    raw = getattr(settings, 'TELEGRAM_FORUM_TOPICS', '') or ''
    result = {}
    for item in raw.replace(';', ',').split(','):
        key, separator, thread_id = item.strip().partition(':')
        if separator and key.strip() and thread_id.strip().isdigit():
            result[key.strip().casefold()] = int(thread_id.strip())
    return result


def telegram_university_topic(message):
    sender_email = (message.sender_email or '').strip().casefold()
    domain = sender_email.rpartition('@')[2]
    haystack = ' '.join((
        message.sender_name or '', sender_email, message.subject or '',
        (message.body_text or '')[:3000],
    )).casefold().replace('ё', 'е')
    for topic_key, domains, markers in UNIVERSITY_TOPIC_RULES:
        if any(domain == known or domain.endswith('.' + known) for known in domains):
            return topic_key
        if any(marker.replace('ё', 'е') in haystack for marker in markers):
            return topic_key
    return 'unsorted'


def is_university_message(message):
    sender_name = (message.sender_name or '').strip()
    sender_email = (message.sender_email or '').strip().casefold()
    domain = sender_email.rpartition('@')[2]
    identity = f'{sender_name} {sender_email}'
    if SERVICE_SENDER_RE.search(identity):
        return False
    if any(domain == known or domain.endswith('.' + known) for known in KNOWN_UNIVERSITY_DOMAINS):
        return True
    if any(domain.endswith(suffix) for suffix in UNIVERSITY_DOMAIN_SUFFIXES):
        return True
    if UNIVERSITY_IDENTITY_RE.search(identity):
        return True
    # A quoted university letter inside an applicant's reply must not turn a
    # Gmail/Yandex/Mail.ru sender into a university notification.
    if domain in PUBLIC_EMAIL_DOMAINS:
        return False
    content = f'{message.subject or ""}\n{(message.body_text or "")[:3000]}'
    return len(UNIVERSITY_CONTENT_RE.findall(content)) >= 2


def format_inbound_notification(message):
    sender = message.sender_name.strip() if message.sender_name else ''
    if sender:
        sender = f'{sender} <{message.sender_email}>'
    else:
        sender = message.sender_email

    header = (
        '📬 Новое письмо\n\n'
        f'От: {sender}\n'
        f'Кому: {message.recipient_email}\n'
        f'Ящик: {message.mailbox.display_name or message.mailbox.email}\n'
        f'Тема: {message.subject or "Без темы"}\n'
        f'Папка: {message.get_category_display()}\n\n'
    )
    footer = f'\n\nОткрыть письмо: {settings.TELEGRAM_SITE_URL}/hub/team/inbox/{message.pk}/'
    body = (
        (message.body_text or '').replace('\x00', '').strip()
        or html_to_text(message.body_html)
        or '[Текст письма отсутствует]'
    )
    available = max(0, TELEGRAM_MESSAGE_LIMIT - len(header) - len(footer))
    if len(body) > available:
        marker = '\n… [текст сокращён]'
        body = body[:max(0, available - len(marker))].rstrip() + marker
    return header + body + footer


def send_telegram_text(text, chat_ids=None, message=None):
    token = settings.TELEGRAM_BOT_TOKEN
    chat_ids = list(chat_ids if chat_ids is not None else telegram_chat_ids())
    if not token or not chat_ids:
        raise RuntimeError('Не заполнены TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_IDS.')
    results = []
    errors = []
    delivered_chat_ids = []
    forum_chat_id = (getattr(settings, 'TELEGRAM_FORUM_CHAT_ID', '') or '').strip()
    forum_topics = telegram_forum_topics()
    topic_key = telegram_university_topic(message) if message is not None else 'unsorted'
    for chat_id in chat_ids:
        try:
            thread_id = forum_topics.get(topic_key) if str(chat_id) == forum_chat_id else None
            results.append(_send_telegram_text_to_chat(
                token, chat_id, text, message_thread_id=thread_id,
            ))
            delivered_chat_ids.append(chat_id)
        except Exception as exc:
            errors.append(f'{chat_id}: {exc}')
    if errors:
        raise TelegramDeliveryError(errors, delivered_chat_ids=delivered_chat_ids)
    return results[0] if len(results) == 1 else results


def _send_telegram_text_to_chat(token, chat_id, text, message_thread_id=None):
    data = {
        'chat_id': chat_id,
        'text': text[:TELEGRAM_MESSAGE_LIMIT],
        'disable_web_page_preview': 'true',
    }
    if message_thread_id is not None:
        data['message_thread_id'] = str(message_thread_id)
    return telegram_api_request('sendMessage', data)


def telegram_api_request(method, data=None, read_timeout=15):
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError('Не заполнен TELEGRAM_BOT_TOKEN.')
    data = data or {}
    try:
        if settings.TELEGRAM_API_IP:
            pool = urllib3.HTTPSConnectionPool(
                settings.TELEGRAM_API_IP,
                port=443,
                assert_hostname='api.telegram.org',
                server_hostname='api.telegram.org',
                cert_reqs='CERT_REQUIRED',
                ca_certs=requests.certs.where(),
                timeout=urllib3.Timeout(connect=10, read=read_timeout),
                retries=False,
            )
            try:
                response = pool.request(
                    'POST',
                    f'/bot{token}/{method}',
                    fields=data,
                    encode_multipart=False,
                    headers={'Host': 'api.telegram.org'},
                )
                status_code = response.status
                response_body = response.data.decode('utf-8', errors='replace')
            finally:
                pool.close()
        else:
            response = requests.post(
                f'https://api.telegram.org/bot{token}/{method}',
                data=data,
                timeout=(10, read_timeout),
            )
            status_code = response.status_code
            response_body = response.text
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
        safe_error = str(exc).replace(token, '[скрыто]')
        raise RuntimeError(f'Не удалось подключиться к Telegram API: {safe_error}') from None

    try:
        result = json.loads(response_body)
    except ValueError:
        result = {}
    if not 200 <= status_code < 300:
        try:
            description = result.get('description', '')
        except AttributeError:
            description = response_body[:300]
        raise RuntimeError(f'Telegram API вернул HTTP {status_code}: {description}')
    if not isinstance(result, dict) or not result.get('ok'):
        raise RuntimeError('Telegram API вернул некорректный ответ.')
    return result


def get_telegram_updates(offset=None, timeout=25):
    data = {
        'timeout': str(timeout),
        'allowed_updates': json.dumps(['message', 'callback_query']),
    }
    if offset is not None:
        data['offset'] = str(offset)
    return telegram_api_request(
        'getUpdates',
        data,
        read_timeout=timeout + 10,
    ).get('result', [])


def notify_inbound_message(message):
    if not message.telegram_notification_pending:
        return False
    delivered = []
    try:
        saved_state = json.loads(message.telegram_notification_error or '{}')
        if isinstance(saved_state, dict):
            delivered = list(saved_state.get('delivered', []))
    except (TypeError, ValueError):
        pass
    remaining = [chat_id for chat_id in telegram_chat_ids() if chat_id not in delivered]
    try:
        if remaining:
            send_telegram_text(
                format_inbound_notification(message), chat_ids=remaining, message=message,
            )
    except TelegramDeliveryError as exc:
        delivered.extend(chat_id for chat_id in exc.delivered_chat_ids if chat_id not in delivered)
        message.telegram_notification_error = json.dumps({
            'delivered': delivered,
            'errors': exc.errors,
        }, ensure_ascii=False)[:1000]
        message.save(update_fields=('telegram_notification_error',))
        logger.warning(
            'Telegram notification partially failed for inbound message %s: %s',
            message.pk,
            '; '.join(exc.errors),
        )
        return False
    except Exception as exc:
        safe_error = str(exc).replace(settings.TELEGRAM_BOT_TOKEN, '[скрыто]')[:1000]
        message.telegram_notification_error = safe_error
        message.save(update_fields=('telegram_notification_error',))
        logger.warning('Telegram notification failed for inbound message %s: %s', message.pk, safe_error)
        return False

    message.telegram_notification_pending = False
    message.telegram_notified_at = timezone.now()
    message.telegram_notification_error = ''
    message.save(update_fields=(
        'telegram_notification_pending',
        'telegram_notified_at',
        'telegram_notification_error',
    ))
    return True


def retry_pending_telegram_notifications(mailbox=None, limit=100):
    if not telegram_is_configured():
        return 0
    cutoff = timezone.now() - timedelta(
        days=int(getattr(settings, 'TELEGRAM_NOTIFICATION_MAX_AGE_DAYS', 3))
    )
    pending = InboundMessage.objects.filter(telegram_notification_pending=True)
    if mailbox is not None:
        pending = pending.filter(mailbox=mailbox)
    pending.filter(received_at__lt=cutoff).update(
        telegram_notification_pending=False,
        telegram_notification_error='',
    )
    pending.exclude(category='primary').update(
        telegram_notification_pending=False,
        telegram_notification_error='',
    )
    queryset = (
        pending
        .filter(
            received_at__gte=cutoff,
            mailbox__is_active=True,
            category='primary',
        )
        .select_related('mailbox')
        .order_by('received_at')
    )
    sent = 0
    for message in queryset[:limit]:
        if not is_university_message(message):
            message.telegram_notification_pending = False
            message.telegram_notification_error = ''
            message.save(update_fields=('telegram_notification_pending', 'telegram_notification_error'))
            continue
        sent += int(notify_inbound_message(message))
    return sent
