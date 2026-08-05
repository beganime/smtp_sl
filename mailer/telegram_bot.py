import re

from django.db.models import Q
from django.utils import timezone

from .models import InboundMessage, Mailbox
from .search import fuzzy_rank_mailboxes, fuzzy_score, normalize_search
from .telegram import TELEGRAM_MESSAGE_LIMIT


EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', re.IGNORECASE)
HELP_TEXT = (
    '🔎 Поиск SMTP_SL\n\n'
    'Просто отправьте email или ФИО владельца ящика.\n\n'
    '/mail запрос — найти ящик, статус и количество писем\n'
    '/letters запрос — найти письма по отправителю, теме или тексту\n'
    '/recent email — последние письма конкретного ящика\n'
    '/help — показать эту инструкцию\n\n'
    'Поиск ящиков понимает небольшие опечатки.'
)


def _mailbox_search_query(text):
    email = EMAIL_RE.search(text or '')
    if email:
        return email.group(0)
    cleaned = re.sub(
        r'(?i)\b(есть|ли|данная|эта|почта|почту|ящик|ящика|найди|найти|проверь|проверить|базе)\b',
        ' ',
        text or '',
    )
    return ' '.join(cleaned.split())


def _format_mailbox(mailbox):
    messages = mailbox.messages.all()
    total = messages.count()
    unread = messages.filter(is_read=False).count()
    latest = messages.order_by('-received_at').first()
    status = '✅ активен и отслеживается' if mailbox.is_active else '⏸ не отслеживается'
    lines = [
        f'📮 {mailbox.email}',
        f'Имя: {mailbox.display_name or "не указано"}',
        f'Статус: {status}',
        f'Писем: {total} · непрочитанных: {unread}',
    ]
    if mailbox.sync_error:
        lines.append(f'Синхронизация: ⚠️ {mailbox.sync_error[:180]}')
    elif mailbox.last_synced_at:
        lines.append(f'Синхронизация: {timezone.localtime(mailbox.last_synced_at):%d.%m.%Y %H:%M}')
    else:
        lines.append('Синхронизация: ещё не запускалась')
    if latest:
        lines.extend([
            '',
            f'Последнее письмо: {timezone.localtime(latest.received_at):%d.%m.%Y %H:%M}',
            f'От: {latest.sender_name or latest.sender_email}',
            f'Тема: {latest.subject or "Без темы"}',
        ])
    return '\n'.join(lines)


def search_mailboxes_for_bot(query, limit=5):
    query, matches = find_mailboxes_for_bot(query, limit=limit)
    if len(normalize_search(query)) < 2:
        return 'Введите хотя бы 2 символа email или ФИО.'
    if not matches:
        return f'Ящик по запросу «{query}» не найден.'
    return '\n\n──────────\n\n'.join(_format_mailbox(mailbox) for mailbox in matches)


def find_mailboxes_for_bot(query, limit=5):
    query = _mailbox_search_query(query)
    if len(normalize_search(query)) < 2:
        return query, []
    matches = list(fuzzy_rank_mailboxes(Mailbox.objects.all(), query, limit=limit))
    return query, matches


def _message_matches_fuzzy(message, query):
    score = fuzzy_score(
        query,
        message.sender_name,
        message.sender_email,
        message.recipient_email,
        message.subject,
        message.mailbox.email,
        message.mailbox.display_name,
    )
    return score


def search_letters_for_bot(query, mailbox=None, limit=5):
    query = (query or '').strip()
    if len(normalize_search(query)) < 2:
        return 'Введите хотя бы 2 символа для поиска писем.'
    queryset = InboundMessage.objects.select_related('mailbox').order_by('-received_at')
    if mailbox is not None:
        queryset = queryset.filter(mailbox=mailbox)
    exact = queryset.filter(
        Q(subject__icontains=query)
        | Q(sender_name__icontains=query)
        | Q(sender_email__icontains=query)
        | Q(recipient_email__icontains=query)
        | Q(body_text__icontains=query)
        | Q(mailbox__email__icontains=query)
        | Q(mailbox__display_name__icontains=query)
    )[:limit]
    matches = list(exact)
    if not matches:
        scored = [
            (_message_matches_fuzzy(message, query), message)
            for message in queryset[:1000]
        ]
        matches = [
            message for score, message in sorted(scored, key=lambda item: -item[0])
            if score >= 0.54
        ][:limit]
    if not matches:
        return f'Писем по запросу «{query}» не найдено.'
    blocks = []
    for message in matches:
        snippet = ' '.join((message.body_text or '').split())[:240]
        blocks.append(
            f'✉️ {message.subject or "Без темы"}\n'
            f'От: {message.sender_name or message.sender_email} <{message.sender_email}>\n'
            f'Кому: {message.recipient_email}\n'
            f'Ящик: {message.mailbox.email}\n'
            f'Дата: {timezone.localtime(message.received_at):%d.%m.%Y %H:%M}\n'
            f'Папка: {message.get_category_display()}\n'
            f'{snippet or "[текст отсутствует]"}\n'
            f'Открыть: https://tmmail.ru/hub/team/inbox/{message.pk}/'
        )
    return '\n\n──────────\n\n'.join(blocks)[:TELEGRAM_MESSAGE_LIMIT]


def recent_letters_for_bot(query):
    mailbox_query = _mailbox_search_query(query)
    matches = list(fuzzy_rank_mailboxes(Mailbox.objects.all(), mailbox_query, limit=2))
    if not matches:
        return f'Ящик по запросу «{mailbox_query}» не найден.'
    if len(matches) > 1:
        return 'Найдено несколько ящиков. Уточните полный email:\n' + '\n'.join(
            f'• {mailbox.email}' for mailbox in matches
        )
    return search_letters_for_bot(mailbox_query, mailbox=matches[0], limit=5)


def handle_telegram_bot_text(text):
    text = (text or '').strip()
    command, _, argument = text.partition(' ')
    command = command.casefold().split('@', 1)[0]
    if command in ('/start', '/help'):
        return HELP_TEXT
    if command in ('/mail', '/mailbox', '/status'):
        return search_mailboxes_for_bot(argument)
    if command in ('/letters', '/search'):
        return search_letters_for_bot(argument)
    if command == '/recent':
        return recent_letters_for_bot(argument)
    if text.startswith('/'):
        return 'Неизвестная команда.\n\n' + HELP_TEXT
    query = _mailbox_search_query(text)
    if EMAIL_RE.search(text) or any(word in text.casefold() for word in ('почт', 'ящик', 'актив')):
        return search_mailboxes_for_bot(query)
    mailbox_result = search_mailboxes_for_bot(query)
    if 'не найден' not in mailbox_result:
        return mailbox_result
    return search_letters_for_bot(text)
