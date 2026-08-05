import json
import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from .models import Client, Mailbox, Region
from .search import fuzzy_score
from .telegram import TELEGRAM_MESSAGE_LIMIT, telegram_api_request
from .telegram_bot import (
    find_mailboxes_for_bot,
    recent_letters_for_bot,
    search_letters_for_bot,
    search_mailboxes_for_bot,
)


logger = logging.getLogger('mailer')

BUTTON_MAILBOX = '🔎 Найти ящик'
BUTTON_LETTERS = '✉️ Найти письма'
BUTTON_ADD = '➕ Добавить почту'
BUTTON_SITE = '🌐 Открыть сайт'
BUTTON_CANCEL = '❌ Отмена'
SITE_URL = 'https://tmmail.ru/'
PUBLIC_HELP_TEXT = (
    '📬 Возможности SMTP_SL\n\n'
    '🔎 Найти ящик — проверить наличие, активность и количество писем.\n'
    '✉️ Найти письма — поиск по отправителю, теме, email или тексту.\n'
    '➕ Добавить почту — пошагово подключить новый ящик к аккаунту.\n'
    '🌐 Открыть сайт — перейти на tmmail.ru.\n\n'
    'Команды:\n'
    '/mail запрос — найти ящик\n'
    '/letters запрос — найти письма\n'
    '/recent email — последние письма ящика\n'
    '/add — добавить ящик\n'
    '/cancel — отменить текущее действие\n'
    '/start — главное меню'
)

PROVIDER_DEFAULTS = {
    'yandex': ('imap.yandex.ru', 993, 'smtp.yandex.ru', 465),
    'mailru': ('imap.mail.ru', 993, 'smtp.mail.ru', 465),
    'gmail': ('imap.gmail.com', 993, 'smtp.gmail.com', 465),
    'sanly': ('mail.sanly.tm', 993, 'mail.sanly.tm', 465),
}
PROVIDER_LABELS = dict(Mailbox.PROVIDER_CHOICES)


def _reply_keyboard():
    return {
        'keyboard': [
            [{'text': BUTTON_MAILBOX}, {'text': BUTTON_LETTERS}],
            [{'text': BUTTON_ADD}],
            [{'text': BUTTON_SITE}],
        ],
        'resize_keyboard': True,
        'is_persistent': True,
        'input_field_placeholder': 'Выберите действие или напишите запрос',
    }


def _cancel_keyboard():
    return {
        'keyboard': [[{'text': BUTTON_CANCEL}]],
        'resize_keyboard': True,
        'one_time_keyboard': False,
        'input_field_placeholder': 'Введите данные или нажмите «Отмена»',
    }


def _inline_keyboard(rows):
    return {'inline_keyboard': rows}


def _send(chat_id, text, reply_markup=None):
    data = {
        'chat_id': str(chat_id),
        'text': (text or '')[:TELEGRAM_MESSAGE_LIMIT],
        'disable_web_page_preview': 'true',
    }
    if reply_markup:
        data['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
    return telegram_api_request('sendMessage', data)


def _delete_message(chat_id, message_id):
    try:
        telegram_api_request('deleteMessage', {
            'chat_id': str(chat_id),
            'message_id': str(message_id),
        })
    except Exception:
        logger.info('Could not delete a sensitive Telegram message.', exc_info=True)


def _answer_callback(callback_id, text=''):
    data = {'callback_query_id': callback_id}
    if text:
        data['text'] = text[:180]
    try:
        telegram_api_request('answerCallbackQuery', data)
    except Exception:
        logger.info('Could not answer Telegram callback.', exc_info=True)


def _account_label(user):
    profile = getattr(user, 'manager_profile', None)
    device = getattr(profile, 'device', '') or ''
    return f'{user.username} · {device}' if device else user.username


def find_manager_accounts(query, limit=6):
    query = (query or '').strip()
    if len(query) < 2:
        return []
    candidates = []
    for user in get_user_model().objects.filter(is_active=True).select_related('manager_profile'):
        profile = getattr(user, 'manager_profile', None)
        score = fuzzy_score(
            query,
            user.username,
            user.get_full_name(),
            getattr(profile, 'phone', ''),
            getattr(profile, 'device', ''),
            getattr(profile, 'city', ''),
        )
        if score >= 0.42:
            candidates.append((score, user))
    candidates.sort(key=lambda item: (-item[0], item[1].pk))
    return [user for _, user in candidates[:limit]]


def create_mailbox_from_bot(data):
    email = (data.get('email') or '').strip().casefold()
    password = data.get('password') or ''
    provider = data.get('provider') or ''
    manager_id = data.get('manager_id')
    is_active = bool(data.get('is_active'))

    validate_email(email)
    if provider not in PROVIDER_LABELS:
        raise ValidationError('Неизвестный почтовый провайдер.')
    if not password:
        raise ValidationError('Пароль не может быть пустым.')

    manager = get_user_model().objects.select_related('manager_profile').get(
        pk=manager_id,
        is_active=True,
    )
    profile = getattr(manager, 'manager_profile', None)
    region = getattr(profile, 'region', None)
    if region is None:
        region = Region.objects.filter(name='Лебап').first() or Region.objects.first()

    if provider in PROVIDER_DEFAULTS:
        imap_host, imap_port, smtp_host, smtp_port = PROVIDER_DEFAULTS[provider]
    else:
        imap_host = (data.get('imap_host') or '').strip()
        smtp_host = (data.get('smtp_host') or '').strip()
        imap_port, smtp_port = 993, 465
        if not imap_host or not smtp_host:
            raise ValidationError('Для другого провайдера нужны IMAP- и SMTP-серверы.')

    with transaction.atomic():
        if Mailbox.objects.filter(email__iexact=email).exists():
            raise ValidationError('Такой ящик уже есть в базе.')
        client, _ = Client.objects.get_or_create(email=email)
        mailbox = Mailbox(
            manager=manager,
            client=client,
            region=region,
            owner_phone=getattr(profile, 'phone', '') or '',
            email=email,
            display_name=email.partition('@')[0],
            provider=provider,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_use_ssl=True,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_use_ssl=True,
            is_active=is_active,
        )
        mailbox.set_password(password)
        mailbox.save()
    return mailbox


class TelegramBotUI:
    def __init__(self):
        self.sessions = {}

    def configure(self):
        telegram_api_request('setMyCommands', {
            'commands': json.dumps([
                {'command': 'start', 'description': 'Открыть главное меню'},
                {'command': 'mail', 'description': 'Найти почтовый ящик'},
                {'command': 'letters', 'description': 'Найти письма'},
                {'command': 'recent', 'description': 'Последние письма ящика'},
                {'command': 'add', 'description': 'Добавить новый ящик'},
                {'command': 'cancel', 'description': 'Отменить текущее действие'},
                {'command': 'help', 'description': 'Показать справку'},
            ], ensure_ascii=False),
        })

    def show_menu(self, chat_id, intro=None):
        text = intro or (
            '📬 SMTP_SL\n\n'
            'Здесь можно искать ящики и письма, проверять активность и добавлять новую почту.\n\n'
            'Выберите действие на клавиатуре внизу.'
        )
        return _send(chat_id, text, _reply_keyboard())

    def send_mailbox_search(self, chat_id, query):
        text = search_mailboxes_for_bot(query)
        _, mailboxes = find_mailboxes_for_bot(query)
        if not mailboxes:
            return _send(chat_id, text, _reply_keyboard())
        rows = [
            [{
                'text': f'📨 Последние 5 · {mailbox.email}'[:60],
                'callback_data': f'recentmb:{mailbox.pk}',
            }]
            for mailbox in mailboxes
        ]
        rows.append([{'text': '🏠 Главное меню', 'callback_data': 'menu'}])
        return _send(chat_id, text, _inline_keyboard(rows))

    def handle_update(self, update):
        callback = update.get('callback_query')
        if callback:
            callback_chat = ((callback.get('message') or {}).get('chat') or {})
            if callback_chat.get('type') not in (None, 'private'):
                return None
            return self._handle_callback(callback)
        message = update.get('message') or {}
        if (message.get('chat') or {}).get('type') not in (None, 'private'):
            return None
        text = message.get('text')
        chat_id = (message.get('chat') or {}).get('id')
        if chat_id is None or text is None:
            return None
        return self._handle_message(str(chat_id), message)

    def _handle_message(self, chat_id, message):
        text = (message.get('text') or '').strip()
        command, _, argument = text.partition(' ')
        command = command.casefold().split('@', 1)[0]

        if command in ('/start', '/menu'):
            self.sessions.pop(chat_id, None)
            return self.show_menu(chat_id)
        if command == '/help':
            return _send(chat_id, PUBLIC_HELP_TEXT, _reply_keyboard())
        if command == '/cancel' or text == BUTTON_CANCEL:
            self.sessions.pop(chat_id, None)
            return self.show_menu(chat_id, '✅ Действие отменено.')
        if command in ('/mail', '/mailbox', '/status') and argument:
            return self.send_mailbox_search(chat_id, argument)
        if command in ('/letters', '/search') and argument:
            return _send(chat_id, search_letters_for_bot(argument), _reply_keyboard())
        if command == '/recent' and argument:
            return _send(chat_id, recent_letters_for_bot(argument), _reply_keyboard())
        if command == '/add' or text == BUTTON_ADD:
            if (message.get('chat') or {}).get('type') not in (None, 'private'):
                return _send(
                    chat_id,
                    '🔐 Добавлять почтовые ящики можно только в личном чате с ботом.',
                    _reply_keyboard(),
                )
            self.sessions[chat_id] = {'step': 'email'}
            return _send(
                chat_id,
                '➕ Добавление почты\n\nВведите полный адрес почтового ящика:',
                _cancel_keyboard(),
            )
        if text == BUTTON_MAILBOX:
            self.sessions[chat_id] = {'step': 'mailbox_search'}
            return _send(chat_id, 'Введите email или имя владельца ящика:', _cancel_keyboard())
        if text == BUTTON_LETTERS:
            self.sessions[chat_id] = {'step': 'letter_search'}
            return _send(chat_id, 'Введите отправителя, тему, email или текст письма:', _cancel_keyboard())
        if text == BUTTON_SITE:
            return _send(
                chat_id,
                'Откройте SMTP_SL в браузере:',
                _inline_keyboard([[{'text': '🌐 Открыть tmmail.ru', 'url': SITE_URL}]]),
            )

        session = self.sessions.get(chat_id)
        if not session:
            return self.send_mailbox_search(chat_id, text)
        return self._continue_session(chat_id, message, session)

    def _continue_session(self, chat_id, message, session):
        text = (message.get('text') or '').strip()
        step = session.get('step')
        if step == 'mailbox_search':
            self.sessions.pop(chat_id, None)
            return self.send_mailbox_search(chat_id, text)
        if step == 'letter_search':
            self.sessions.pop(chat_id, None)
            return _send(chat_id, search_letters_for_bot(text), _reply_keyboard())
        if step == 'email':
            email = text.casefold()
            try:
                validate_email(email)
            except ValidationError:
                return _send(chat_id, 'Адрес выглядит неверно. Введите email полностью, например name@gmail.com.')
            if Mailbox.objects.filter(email__iexact=email).exists():
                return _send(chat_id, 'Этот ящик уже есть в базе. Введите другой адрес или нажмите «Отмена».')
            session.update(step='password', email=email)
            return _send(
                chat_id,
                '🔐 Введите пароль почты или пароль приложения.\n\n'
                'Сообщение с паролем будет удалено из чата.',
            )
        if step == 'password':
            if not text:
                return _send(chat_id, 'Пароль не может быть пустым. Введите его ещё раз.')
            session.update(step='provider', password=text)
            _delete_message(chat_id, message.get('message_id'))
            return _send(
                chat_id,
                'Выберите почтового провайдера:',
                _inline_keyboard([
                    [
                        {'text': 'Gmail', 'callback_data': 'provider:gmail'},
                        {'text': 'Яндекс', 'callback_data': 'provider:yandex'},
                    ],
                    [
                        {'text': 'Mail.ru', 'callback_data': 'provider:mailru'},
                        {'text': 'Sanly.tm', 'callback_data': 'provider:sanly'},
                    ],
                    [{'text': 'Другой', 'callback_data': 'provider:other'}],
                    [{'text': '❌ Отмена', 'callback_data': 'cancel'}],
                ]),
            )
        if step == 'imap_host':
            session.update(step='smtp_host', imap_host=text)
            return _send(chat_id, 'Введите адрес SMTP-сервера, например smtp.example.com:')
        if step == 'smtp_host':
            session.update(step='account_query', smtp_host=text)
            return _send(chat_id, 'Введите логин, телефон или устройство аккаунта, куда добавить ящик:')
        if step == 'account_query':
            accounts = find_manager_accounts(text)
            if not accounts:
                return _send(chat_id, 'Аккаунт не найден. Попробуйте другой логин, телефон или название устройства.')
            rows = [
                [{'text': _account_label(user)[:55], 'callback_data': f'account:{user.pk}'}]
                for user in accounts
            ]
            rows.append([{'text': '❌ Отмена', 'callback_data': 'cancel'}])
            return _send(chat_id, 'Выберите аккаунт из найденных:', _inline_keyboard(rows))
        return self.show_menu(chat_id)

    def _handle_callback(self, callback):
        callback_id = callback.get('id')
        data = callback.get('data') or ''
        message = callback.get('message') or {}
        chat_id = str((message.get('chat') or {}).get('id', ''))
        if not chat_id:
            return None
        _answer_callback(callback_id)
        if data == 'cancel':
            self.sessions.pop(chat_id, None)
            return self.show_menu(chat_id, '✅ Действие отменено.')
        if data == 'menu':
            self.sessions.pop(chat_id, None)
            return self.show_menu(chat_id)
        if data.startswith('recentmb:'):
            try:
                mailbox_id = int(data.partition(':')[2])
                mailbox = Mailbox.objects.get(pk=mailbox_id)
            except (ValueError, Mailbox.DoesNotExist):
                return _send(chat_id, 'Ящик больше не найден в базе.', _reply_keyboard())
            return _send(
                chat_id,
                search_letters_for_bot(mailbox.email, mailbox=mailbox, limit=5),
                _inline_keyboard([
                    [{
                        'text': '🔄 Обновить последние 5',
                        'callback_data': f'recentmb:{mailbox.pk}',
                    }],
                    [{'text': '🏠 Главное меню', 'callback_data': 'menu'}],
                ]),
            )

        session = self.sessions.get(chat_id)
        if not session:
            return self.show_menu(chat_id, 'Сценарий устарел. Начните действие заново.')
        if data.startswith('provider:') and session.get('step') == 'provider':
            provider = data.partition(':')[2]
            if provider not in PROVIDER_LABELS:
                return _send(chat_id, 'Неизвестный провайдер.')
            session['provider'] = provider
            if provider == 'other':
                session['step'] = 'imap_host'
                return _send(chat_id, 'Введите адрес IMAP-сервера, например imap.example.com:')
            session['step'] = 'account_query'
            return _send(chat_id, 'Введите логин, телефон или устройство аккаунта, куда добавить ящик:')
        if data.startswith('account:') and session.get('step') == 'account_query':
            try:
                manager_id = int(data.partition(':')[2])
                manager = get_user_model().objects.get(pk=manager_id, is_active=True)
            except (ValueError, get_user_model().DoesNotExist):
                return _send(chat_id, 'Аккаунт уже недоступен. Выполните поиск ещё раз.')
            session.update(step='tracking', manager_id=manager.pk)
            return _send(
                chat_id,
                f'Аккаунт: {_account_label(manager)}\n\nОтслеживать и синхронизировать этот ящик?',
                _inline_keyboard([
                    [
                        {'text': '✅ Да, отслеживать', 'callback_data': 'tracking:1'},
                        {'text': '⏸ Не отслеживать', 'callback_data': 'tracking:0'},
                    ],
                    [{'text': '❌ Отмена', 'callback_data': 'cancel'}],
                ]),
            )
        if data.startswith('tracking:') and session.get('step') == 'tracking':
            session['is_active'] = data.endswith(':1')
            try:
                mailbox = create_mailbox_from_bot(session)
            except (ValidationError, IntegrityError) as exc:
                self.sessions.pop(chat_id, None)
                message_text = '; '.join(getattr(exc, 'messages', [])) or 'Не удалось сохранить ящик.'
                return self.show_menu(chat_id, f'⚠️ {message_text}')
            except Exception:
                logger.exception('Telegram mailbox creation failed.')
                self.sessions.pop(chat_id, None)
                return self.show_menu(chat_id, '⚠️ Не удалось добавить ящик. Проверьте данные или обратитесь к администратору.')
            finally:
                session.pop('password', None)
            self.sessions.pop(chat_id, None)
            tracking = 'включено' if mailbox.is_active else 'выключено'
            return self.show_menu(
                chat_id,
                f'✅ Ящик добавлен\n\n'
                f'Email: {mailbox.email}\n'
                f'Провайдер: {mailbox.get_provider_display()}\n'
                f'Аккаунт: {_account_label(mailbox.manager)}\n'
                f'Отслеживание: {tracking}\n\n'
                'Активные ящики будут синхронизированы фоновым обработчиком.',
            )
        return _send(chat_id, 'Эта кнопка уже неактуальна. Начните действие заново.', _reply_keyboard())
