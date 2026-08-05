from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

class Client(models.Model):
    email = models.EmailField('Email', unique=True)
    full_name = models.CharField('ФИО', max_length=255, blank=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)

    class Meta:
        verbose_name = 'Клиент'
        verbose_name_plural = 'Клиенты'

    def __str__(self):
        return f"{self.email} ({self.full_name})" if self.full_name else self.email


class Newsletter(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Черновик'),
        ('scheduled', 'Запланирована'),
        ('sent', 'Отправлена'),
    ]

    subject = models.CharField('Тема письма', max_length=255)
    message = models.TextField('Содержимое (Plain Text)')
    html_message = models.TextField('Содержимое (HTML)', blank=True, help_text="Опционально. Если заполнено, отправится красивое HTML письмо.")
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    scheduled_time = models.DateTimeField('Запланированное время', null=True, blank=True)
    recipients = models.ManyToManyField(Client, verbose_name='Получатели', limit_choices_to={'is_active': True})

    class Meta:
        verbose_name = 'Рассылка'
        verbose_name_plural = 'Рассылки'

    def __str__(self):
        return self.subject


class SendingLog(models.Model):
    newsletter = models.ForeignKey(Newsletter, on_delete=models.CASCADE, verbose_name='Рассылка')
    client = models.ForeignKey(Client, on_delete=models.CASCADE, verbose_name='Клиент')
    sent_at = models.DateTimeField('Время попытки', auto_now_add=True)
    status = models.BooleanField('Успешно', default=False)
    error_message = models.TextField('Текст ошибки', blank=True)

    class Meta:
        verbose_name = 'Лог отправки'
        verbose_name_plural = 'Логи отправок'

    def __str__(self):
        return f"Лог {self.pk}: {self.newsletter.subject} -> {self.client.email}"


class ManagerProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='manager_profile', verbose_name='Пользователь')
    phone = models.CharField('Телефон', max_length=32, unique=True)
    device = models.CharField('Устройство', max_length=120)
    city = models.CharField('Город', max_length=120, blank=True)
    region = models.ForeignKey('Region', on_delete=models.SET_NULL, null=True, blank=True, related_name='manager_profiles', verbose_name='Регион')
    job_title = models.CharField('Должность', max_length=120, blank=True, default='Менеджер')

    class Meta:
        verbose_name = 'Профиль менеджера'
        verbose_name_plural = 'Профили менеджеров'

    def __str__(self):
        return f'{self.phone} · {self.device}'


class Region(models.Model):
    name = models.CharField('Регион', max_length=80, unique=True)
    city = models.CharField('Административный центр', max_length=80)

    class Meta:
        verbose_name = 'Регион'
        verbose_name_plural = 'Регионы'
        ordering = ('name',)

    def __str__(self):
        return f'{self.name} · {self.city}'


class Mailbox(models.Model):
    PROVIDER_CHOICES = [
        ('yandex', 'Яндекс'),
        ('mailru', 'Mail.ru'),
        ('gmail', 'Gmail'),
        ('sanly', 'Sanly.tm'),
        ('other', 'Другой'),
    ]
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mailboxes', verbose_name='Менеджер')
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name='mailboxes', verbose_name='Клиент')
    region = models.ForeignKey(Region, on_delete=models.PROTECT, null=True, blank=True, related_name='mailboxes', verbose_name='Регион')
    owner_phone = models.CharField('Владелец телефона', max_length=32, blank=True, help_text='Телефон ответственного пользователя или устройства.')
    email = models.EmailField('Почтовый ящик', unique=True)
    display_name = models.CharField('Название', max_length=160, blank=True)
    provider = models.CharField('Провайдер', max_length=20, choices=PROVIDER_CHOICES, default='other')
    imap_host = models.CharField('IMAP сервер', max_length=255, blank=True)
    imap_port = models.PositiveIntegerField('IMAP порт', default=993)
    imap_use_ssl = models.BooleanField('IMAP SSL', default=True)
    smtp_host = models.CharField('SMTP сервер', max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField('SMTP порт', default=465)
    smtp_use_ssl = models.BooleanField('SMTP SSL', default=True)
    password_encrypted = models.TextField('Зашифрованный пароль почты', blank=True, editable=False)
    is_active = models.BooleanField('Активен', default=True)
    last_synced_at = models.DateTimeField('Последняя синхронизация', null=True, blank=True)
    sync_error = models.TextField('Ошибка синхронизации', blank=True)
    telegram_notifications_after = models.DateTimeField(
        'Уведомлять в Telegram после',
        default=timezone.now,
        editable=False,
    )
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        verbose_name = 'Почтовый ящик'
        verbose_name_plural = 'Почтовые ящики'
        ordering = ('display_name', 'email')

    def __str__(self):
        return self.display_name or self.email

    def set_password(self, password):
        from .crypto import encrypt_secret
        # Passwords copied from spreadsheets and messengers can contain
        # non-breaking spaces.  imaplib builds an ASCII LOGIN command, so
        # those visually identical Unicode spaces otherwise cause an
        # ``ascii codec can't encode character`` error before authentication.
        password = str(password).translate({
            ord('\u00a0'): ' ',  # no-break space
            ord('\u2007'): ' ',  # figure space
            ord('\u202f'): ' ',  # narrow no-break space
        })
        self.password_encrypted = encrypt_secret(password)

    def get_password(self):
        from .crypto import decrypt_secret
        return decrypt_secret(self.password_encrypted)

    @property
    def is_configured(self):
        return bool(self.imap_host and self.password_encrypted)


class MailboxSyncRun(models.Model):
    STATUS_CHOICES = [
        ('queued', 'В очереди'),
        ('running', 'Выполняется'),
        ('completed', 'Завершена'),
        ('failed', 'Ошибка'),
    ]
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='mailbox_sync_runs',
        verbose_name='Менеджер',
        help_text='Пустое значение означает синхронизацию всех активных ящиков.',
    )
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='queued', db_index=True)
    processed = models.PositiveIntegerField('Обработано ящиков', default=0)
    imported = models.PositiveIntegerField('Загружено писем', default=0)
    errors = models.PositiveIntegerField('Ошибок', default=0)
    error_message = models.TextField('Ошибка запуска', blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    started_at = models.DateTimeField('Начато', null=True, blank=True)
    finished_at = models.DateTimeField('Завершено', null=True, blank=True)

    class Meta:
        verbose_name = 'Запуск синхронизации почты'
        verbose_name_plural = 'Запуски синхронизации почты'
        ordering = ('-created_at',)

    def __str__(self):
        scope = self.manager.username if self.manager_id else 'все ящики'
        return f'{scope}: {self.get_status_display()}'


def default_ai_instructions():
    return (
        'Считать важными письма от вузов о поступлении, документах, экзаменах, '
        'личном кабинете, оплате, зачислении, сроках и действиях студента. '
        'БГМУ, МАИ, ЛЭТИ, БФУ и другие университеты распознавать по полному '
        'названию, сокращению, домену и контексту. Реклама, магазины, Google, '
        'социальные сети и обычные массовые рассылки не важны.'
    )


class GeminiConfiguration(models.Model):
    enabled = models.BooleanField(
        'Включить AI-анализ',
        default=True,
        help_text='Если выключено, AI-анализ и Telegram-уведомления о новых письмах приостанавливаются.',
    )
    api_keys_encrypted = models.TextField('Зашифрованные API-ключи', blank=True, editable=False)
    model_name = models.CharField('Модель Gemini', max_length=100, default='gemini-2.5-flash')
    importance_threshold = models.PositiveSmallIntegerField(
        'Порог важности для Telegram',
        default=70,
        help_text='От 0 до 100. Уведомление отправляется только при оценке не ниже порога.',
    )
    context_messages = models.PositiveSmallIntegerField(
        'Писем в контексте',
        default=5,
        help_text='Текущее письмо и предыдущие письма от того же домена. Рекомендуется 5.',
    )
    max_body_chars = models.PositiveIntegerField(
        'Максимум символов письма',
        default=12000,
        help_text='Ограничивает объём текста одного письма, отправляемого на анализ.',
    )
    request_delay_seconds = models.FloatField(
        'Пауза между запросами, сек.',
        default=2.0,
        help_text='Снижает риск превышения лимита Gemini. Для бесплатного ключа можно увеличить.',
    )
    custom_instructions = models.TextField(
        'Дополнительные правила анализа',
        default=default_ai_instructions,
    )
    last_success_at = models.DateTimeField('Последний успешный анализ', null=True, blank=True)
    last_error = models.TextField('Последняя ошибка', blank=True)
    updated_at = models.DateTimeField('Настройки обновлены', auto_now=True)

    class Meta:
        verbose_name = 'Настройки Gemini'
        verbose_name_plural = 'Настройки Gemini'

    def __str__(self):
        return f'Gemini · {self.model_name}'

    def set_api_keys(self, values):
        from .crypto import encrypt_secret
        normalized = []
        for value in values:
            value = (value or '').strip()
            if value and value not in normalized:
                normalized.append(value)
        self.api_keys_encrypted = encrypt_secret('\n'.join(normalized))

    def get_api_keys(self):
        from .crypto import decrypt_secret
        value = decrypt_secret(self.api_keys_encrypted)
        return [item.strip() for item in value.splitlines() if item.strip()]

    @property
    def api_key_count(self):
        try:
            return len(self.get_api_keys())
        except ValueError:
            return 0

    @classmethod
    def load(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config


def default_mosaic_calendar_urls():
    return (
        'https://appointment.mosaicvisa.com/calendar/11\n'
        'https://appointment.mosaicvisa.com/calendar/12'
    )


class MosaicMonitorConfiguration(models.Model):
    enabled = models.BooleanField('Мониторинг включён', default=True)
    calendar_urls = models.TextField(
        'Страницы календарей',
        default=default_mosaic_calendar_urls,
        help_text='По одной ссылке в строке.',
    )
    check_interval_minutes = models.PositiveSmallIntegerField(
        'Интервал проверки, минут',
        default=5,
    )
    months_ahead = models.PositiveSmallIntegerField(
        'Месяцев вперёд',
        default=3,
        help_text='Проверяется текущий месяц и указанное число следующих месяцев.',
    )
    sender_email = models.EmailField(
        'Почта отправителя',
        default='akylpro2023@gmail.com',
    )
    sender_password_encrypted = models.TextField(
        'Зашифрованный пароль приложения',
        blank=True,
        editable=False,
    )
    recipients = models.TextField(
        'Получатели',
        default='begenchyagmurow2008@gmail.com',
        help_text='Email через запятую или по одному в строке.',
    )
    subject_prefix = models.CharField(
        'Префикс темы',
        max_length=120,
        default='[Mosaic Visa]',
    )
    last_snapshot = models.JSONField('Последний снимок', default=dict, blank=True, editable=False)
    last_checked_at = models.DateTimeField('Последняя проверка', null=True, blank=True)
    last_changed_at = models.DateTimeField('Последнее изменение', null=True, blank=True)
    last_notification_at = models.DateTimeField('Последнее уведомление', null=True, blank=True)
    last_error = models.TextField('Последняя ошибка', blank=True)
    updated_at = models.DateTimeField('Настройки обновлены', auto_now=True)

    class Meta:
        verbose_name = 'Монитор Mosaic Visa'
        verbose_name_plural = 'Монитор Mosaic Visa'

    def __str__(self):
        return 'Mosaic Visa monitor'

    @classmethod
    def load(cls):
        config, _ = cls.objects.get_or_create(pk=1)
        return config

    def set_sender_password(self, value):
        from .crypto import encrypt_secret
        self.sender_password_encrypted = encrypt_secret(value)

    def get_sender_password(self):
        from .crypto import decrypt_secret
        return decrypt_secret(self.sender_password_encrypted)

    @property
    def password_is_set(self):
        return bool(self.sender_password_encrypted)

    def recipient_list(self):
        import re
        values = re.split(r'[\s,;]+', self.recipients or '')
        result = []
        for value in values:
            value = value.strip()
            if value and value not in result:
                result.append(value)
        return result

    def calendar_url_list(self):
        result = []
        for value in (self.calendar_urls or '').splitlines():
            value = value.strip()
            if value and value not in result:
                result.append(value)
        return result


class MosaicMonitorEvent(models.Model):
    EVENT_CHOICES = [
        ('baseline', 'Базовый снимок'),
        ('opening', 'Открыта запись'),
        ('change', 'Изменение календаря'),
        ('test', 'Тестовое письмо'),
        ('error', 'Ошибка'),
    ]
    event_type = models.CharField('Тип', max_length=20, choices=EVENT_CHOICES)
    summary = models.CharField('Описание', max_length=500)
    details = models.TextField('Подробности', blank=True)
    notification_sent = models.BooleanField('Письмо отправлено', default=False)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'Событие монитора Mosaic Visa'
        verbose_name_plural = 'События монитора Mosaic Visa'
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.get_event_type_display()} · {self.created_at:%d.%m.%Y %H:%M}'


class InboundMessage(models.Model):
    CATEGORY_CHOICES = [
        ('primary', 'Основные'),
        ('google', 'Уведомления Google'),
        ('spam', 'Спам'),
        ('sent', 'Отправленные'),
    ]
    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name='messages', verbose_name='Почтовый ящик')
    imap_folder = models.CharField('IMAP папка', max_length=255, default='INBOX', db_index=True)
    external_uid = models.CharField('UID', max_length=255)
    sender_name = models.CharField('Имя отправителя', max_length=255, blank=True)
    sender_email = models.EmailField('Email отправителя')
    recipient_email = models.EmailField('Получатель')
    subject = models.CharField('Тема', max_length=500, blank=True, default='Без темы')
    body_text = models.TextField('Текст письма', blank=True)
    body_html = models.TextField('HTML письма', blank=True)
    received_at = models.DateTimeField('Получено')
    category = models.CharField('Папка', max_length=20, choices=CATEGORY_CHOICES, default='primary', db_index=True)
    is_read = models.BooleanField('Прочитано', default=False)
    is_replied = models.BooleanField('Ответ отправлен', default=False)
    attachments_synced = models.BooleanField('Вложения обработаны', default=False)
    telegram_notification_pending = models.BooleanField('Ожидает уведомления Telegram', default=False, db_index=True)
    telegram_notified_at = models.DateTimeField('Уведомление Telegram отправлено', null=True, blank=True)
    telegram_notification_error = models.TextField('Ошибка уведомления Telegram', blank=True)
    created_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        verbose_name = 'Входящее письмо'
        verbose_name_plural = 'Входящие письма'
        ordering = ('-received_at',)
        constraints = [models.UniqueConstraint(fields=('mailbox', 'imap_folder', 'external_uid'), name='unique_mailbox_folder_message_uid')]

    def __str__(self):
        return f'{self.subject} — {self.sender_email}'


class EmailAIAnalysis(models.Model):
    STATUS_CHOICES = [
        ('pending', 'В очереди'),
        ('processing', 'Анализируется'),
        ('completed', 'Готово'),
        ('error', 'Ошибка'),
    ]
    UNIVERSITY_GROUP_CHOICES = [
        ('medical', 'Медицинские вузы'),
        ('technical', 'Технические и авиационные вузы'),
        ('federal', 'Федеральные и классические вузы'),
        ('other_university', 'Другие вузы'),
        ('non_university', 'Не вуз'),
    ]
    TOPIC_CHOICES = [
        ('documents', 'Документы'),
        ('admission', 'Поступление и зачисление'),
        ('exam', 'Экзамены'),
        ('personal_account', 'Личный кабинет'),
        ('payment', 'Оплата'),
        ('deadline', 'Сроки'),
        ('security', 'Безопасность'),
        ('marketing', 'Реклама и рассылка'),
        ('other', 'Другое'),
    ]
    message = models.OneToOneField(
        InboundMessage,
        on_delete=models.CASCADE,
        related_name='ai_analysis',
        verbose_name='Письмо',
    )
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    is_important = models.BooleanField('Важное', default=False, db_index=True)
    importance_score = models.PositiveSmallIntegerField('Оценка важности', default=0, db_index=True)
    is_university = models.BooleanField('От вуза', default=False, db_index=True)
    university_name = models.CharField('Вуз', max_length=255, blank=True, db_index=True)
    university_group = models.CharField(
        'Группа',
        max_length=30,
        choices=UNIVERSITY_GROUP_CHOICES,
        default='non_university',
        db_index=True,
    )
    topic = models.CharField('Тема AI', max_length=30, choices=TOPIC_CHOICES, default='other', db_index=True)
    summary = models.TextField('Краткое содержание', blank=True)
    reason = models.TextField('Почему важно или не важно', blank=True)
    action_required = models.TextField('Что нужно сделать', blank=True)
    suggested_reply = models.TextField('Предлагаемый ответ', blank=True)
    deadline = models.CharField('Срок', max_length=160, blank=True)
    extracted_links = models.JSONField('Ссылки', default=list, blank=True)
    extracted_logins = models.JSONField('Логины', default=list, blank=True)
    extracted_passwords = models.JSONField('Пароли и коды', default=list, blank=True)
    model_name = models.CharField('Модель', max_length=100, blank=True)
    error_message = models.TextField('Ошибка', blank=True)
    analyzed_at = models.DateTimeField('Проанализировано', null=True, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        verbose_name = 'AI-анализ письма'
        verbose_name_plural = 'AI-анализ писем'
        ordering = ('-message__received_at',)

    def __str__(self):
        return f'{self.message.subject}: {self.get_status_display()}'


class InboundAttachment(models.Model):
    message = models.ForeignKey(InboundMessage, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField('Файл', upload_to='mail/inbound/%Y/%m/')
    original_name = models.CharField('Имя файла', max_length=255)
    content_type = models.CharField('MIME-тип', max_length=160, blank=True)
    size = models.PositiveBigIntegerField('Размер', default=0)
    part_index = models.PositiveIntegerField('Номер части')
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        ordering = ('part_index',)
        constraints = [
            models.UniqueConstraint(fields=('message', 'part_index'), name='unique_inbound_attachment_part'),
        ]

    def __str__(self):
        return self.original_name


@receiver(post_delete, sender=InboundAttachment)
def delete_inbound_attachment_file(sender, instance, **kwargs):
    if instance.file:
        instance.file.delete(save=False)


class OutgoingMessage(models.Model):
    STATUS_CHOICES = [('sent', 'Отправлено'), ('failed', 'Ошибка')]
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='outgoing_messages')
    mailbox = models.ForeignKey(Mailbox, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_messages')
    in_reply_to = models.ForeignKey(InboundMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')
    recipient_email = models.EmailField('Получатель')
    subject = models.CharField('Тема', max_length=500)
    body = models.TextField('Текст')
    status = models.CharField('Статус', max_length=16, choices=STATUS_CHOICES)
    error_message = models.TextField('Ошибка', blank=True)
    sent_at = models.DateTimeField('Отправлено', auto_now_add=True)

    class Meta:
        ordering = ('-sent_at',)


class OutgoingAttachment(models.Model):
    message = models.ForeignKey(OutgoingMessage, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField('Файл', upload_to='mail/outgoing/%Y/%m/')
    original_name = models.CharField('Имя файла', max_length=255)
    content_type = models.CharField('MIME-тип', max_length=160, blank=True)
    size = models.PositiveBigIntegerField('Размер', default=0)
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    def __str__(self):
        return self.original_name


class Campaign(models.Model):
    STATUS_CHOICES = [('draft', 'Черновик'), ('sent', 'Отправлена'), ('failed', 'С ошибками')]
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='campaigns')
    subject = models.CharField('Тема', max_length=255)
    body = models.TextField('Сообщение')
    recipients = models.JSONField('Получатели', default=list)
    status = models.CharField('Статус', max_length=16, choices=STATUS_CHOICES, default='draft')
    sent_count = models.PositiveIntegerField('Отправлено', default=0)
    failed_count = models.PositiveIntegerField('Ошибок', default=0)
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)


class CampaignAttachment(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField('Файл', upload_to='mail/campaigns/%Y/%m/')
    original_name = models.CharField('Имя файла', max_length=255)
    content_type = models.CharField('MIME-тип', max_length=160, blank=True)
    size = models.PositiveBigIntegerField('Размер', default=0)
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    def __str__(self):
        return self.original_name
