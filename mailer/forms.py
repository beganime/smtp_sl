import re
from pathlib import Path
from urllib.parse import urlsplit

from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.validators import validate_email
from django.core.exceptions import ValidationError

from .models import Campaign, Mailbox, MosaicMonitorConfiguration, Region


ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.csv', '.rtf', '.odt', '.ods',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic',
    '.zip', '.rar', '.7z',
}
MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL_SIZE = 50 * 1024 * 1024
MAX_ATTACHMENTS_COUNT = 10


class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        cleaned = [super(MultipleFileField, self).clean(item, initial) for item in files]
        if len(cleaned) > MAX_ATTACHMENTS_COUNT:
            raise forms.ValidationError(f'Можно прикрепить не более {MAX_ATTACHMENTS_COUNT} файлов.')
        total_size = 0
        for uploaded in cleaned:
            extension = Path(uploaded.name).suffix.casefold()
            if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise forms.ValidationError(f'Формат {extension or "без расширения"} не поддерживается: {uploaded.name}.')
            if uploaded.size > MAX_ATTACHMENT_SIZE:
                raise forms.ValidationError(f'Файл {uploaded.name} больше 50 МБ.')
            total_size += uploaded.size
        if total_size > MAX_ATTACHMENTS_TOTAL_SIZE:
            raise forms.ValidationError('Общий размер вложений не должен превышать 50 МБ.')
        return cleaned


def normalize_phone(value):
    value = (value or '').strip()
    if re.search(r'[^\d+\s().-]', value):
        return value
    value = re.sub(r'[^\d+]', '', value)
    if value.startswith('00'):
        value = '+' + value[2:]
    return value


class ManagerLoginForm(forms.Form):
    phone = forms.CharField(label='Логин / телефон', max_length=32, widget=forms.TextInput(attrs={'autocomplete': 'username', 'placeholder': '+993 6x xx xx xx или логин'}))
    device = forms.CharField(label='Устройство', max_length=120, widget=forms.TextInput(attrs={'placeholder': 'Например, MacBook — офис'}))
    password = forms.CharField(label='Пароль', strip=False, widget=forms.PasswordInput(attrs={'autocomplete': 'current-password', 'placeholder': '••••••••'}))

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        phone = normalize_phone(cleaned.get('phone'))
        device = (cleaned.get('device') or '').strip()
        password = cleaned.get('password')
        if phone and password:
            self.user_cache = authenticate(self.request, username=phone, password=password)
            if self.user_cache is None:
                raise forms.ValidationError('Неверный телефон или пароль.')
            profile = getattr(self.user_cache, 'manager_profile', None)
            if not profile or profile.device.casefold() != device.casefold():
                self.user_cache = None
                raise forms.ValidationError('Это устройство не привязано к аккаунту.')
        cleaned['phone'] = phone
        return cleaned

    def get_user(self):
        return self.user_cache


class ManagerRegistrationForm(UserCreationForm):
    phone = forms.CharField(label='Телефон', max_length=32)
    device = forms.CharField(label='Устройство', max_length=120)
    city = forms.CharField(
        label='Город', max_length=120,
        widget=forms.TextInput(attrs={
            'list': 'city-options',
            'placeholder': 'Выберите или напишите свой город',
            'autocomplete': 'address-level2',
        }),
        help_text='Можно выбрать город из списка или указать новый.',
    )

    class Meta:
        model = get_user_model()
        fields = ('phone', 'device', 'city', 'password1', 'password2')

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data['phone'])
        if get_user_model().objects.filter(username=phone).exists():
            raise forms.ValidationError('Менеджер с таким телефоном уже существует.')
        return phone

    def clean_city(self):
        city = self.cleaned_data['city'].strip()
        if not city:
            raise forms.ValidationError('Укажите город.')
        return city

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data['phone']
        if commit:
            user.save()
            from .models import ManagerProfile
            city = self.cleaned_data['city']
            region = Region.objects.filter(city__iexact=city).first()
            ManagerProfile.objects.create(
                user=user,
                phone=user.username,
                device=self.cleaned_data['device'].strip(),
                city=city,
                region=region,
            )
        return user


class ReplyForm(forms.Form):
    body = forms.CharField(label='Ответ', widget=forms.Textarea(attrs={'rows': 8, 'placeholder': 'Введите ответ вузу…'}))
    attachments = MultipleFileField(
        label='Файлы', required=False,
        widget=MultipleFileInput(attrs={
            'accept': '.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.rtf,.odt,.ods,.jpg,.jpeg,.png,.gif,.webp,.heic,.zip,.rar,.7z',
        }),
    )


class DirectMessageForm(forms.Form):
    mailbox = forms.ModelChoiceField(
        label='Отправить с ящика',
        queryset=Mailbox.objects.none(),
        widget=forms.HiddenInput(),
    )
    recipient_email = forms.EmailField(
        label='Кому',
        widget=forms.EmailInput(attrs={'placeholder': 'admission@university.edu', 'autocomplete': 'email'}),
    )
    subject = forms.CharField(
        label='Тема', max_length=500,
        widget=forms.TextInput(attrs={'placeholder': 'Тема письма'}),
    )
    body = forms.CharField(
        label='Сообщение',
        widget=forms.Textarea(attrs={'rows': 12, 'placeholder': 'Напишите сообщение…'}),
    )
    attachments = MultipleFileField(
        label='Вложения', required=False,
        widget=MultipleFileInput(attrs={
            'accept': '.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.rtf,.odt,.ods,.jpg,.jpeg,.png,.gif,.webp,.heic,.zip,.rar,.7z',
        }),
    )

    def __init__(self, *args, manager=None, allow_shared=False, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = Mailbox.objects.none()
        if manager is not None:
            queryset = Mailbox.objects.filter(is_active=True)
            if not allow_shared:
                queryset = queryset.filter(manager=manager)
            queryset = queryset.select_related('region', 'manager__manager_profile')
        selected_id = None
        if self.is_bound:
            selected_id = self.data.get(self.add_prefix('mailbox'))
        else:
            selected_id = self.initial.get('mailbox') or self.fields['mailbox'].initial
        if selected_id:
            queryset = queryset.filter(pk=selected_id)
        else:
            queryset = queryset.none()
        self.fields['mailbox'].queryset = queryset
        self.fields['mailbox'].label_from_instance = lambda mailbox: (
            f'{mailbox.email} · '
            f'{mailbox.owner_phone or getattr(getattr(mailbox.manager, "manager_profile", None), "phone", "без владельца")} · '
            f'{mailbox.region or "без региона"}'
        )


class MailboxForm(forms.ModelForm):
    password = forms.CharField(
        label='Пароль почты / пароль приложения',
        strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password', 'placeholder': 'Не обычный пароль от почты'}),
        help_text='Для Sanly используйте пароль от ящика. Для Gmail, Яндекс и Mail.ru используйте пароль приложения.',
    )

    class Meta:
        model = Mailbox
        fields = (
            'email', 'display_name', 'client', 'region', 'owner_phone', 'provider',
            'imap_host', 'imap_port', 'imap_use_ssl',
            'smtp_host', 'smtp_port', 'smtp_use_ssl',
            'is_active',
        )
        widgets = {
            'email': forms.EmailInput(attrs={'placeholder': 'client@yandex.ru'}),
            'display_name': forms.TextInput(attrs={'placeholder': 'Клиент · Вуз'}),
            'owner_phone': forms.TextInput(attrs={'placeholder': '+993 6x xx xx xx'}),
            'imap_host': forms.TextInput(attrs={'placeholder': 'imap.yandex.ru'}),
            'smtp_host': forms.TextInput(attrs={'placeholder': 'smtp.yandex.ru'}),
        }

    def __init__(self, *args, manager=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['region'].required = True
        if self.instance and self.instance.pk:
            self.fields['password'].required = False
            self.fields['password'].help_text = 'Оставьте пустым, чтобы сохранить текущий пароль.'
        else:
            try:
                self.fields['region'].initial = Region.objects.get(name='Лебап')
            except Region.DoesNotExist:
                pass
            profile = getattr(manager, 'manager_profile', None) if manager is not None else None
            if profile:
                self.fields['owner_phone'].initial = profile.phone

    def clean(self):
        cleaned = super().clean()
        provider = cleaned.get('provider')
        defaults = {
            'yandex': ('imap.yandex.ru', 993, 'smtp.yandex.ru', 465),
            'mailru': ('imap.mail.ru', 993, 'smtp.mail.ru', 465),
            'gmail': ('imap.gmail.com', 993, 'smtp.gmail.com', 465),
            'sanly': ('mail.sanly.tm', 993, 'mail.sanly.tm', 465),
        }
        if provider in defaults:
            imap_host, imap_port, smtp_host, smtp_port = defaults[provider]
            cleaned['imap_host'] = imap_host
            cleaned['imap_port'] = imap_port
            cleaned['imap_use_ssl'] = True
            cleaned['smtp_host'] = smtp_host
            cleaned['smtp_port'] = smtp_port
            cleaned['smtp_use_ssl'] = True
        elif not cleaned.get('imap_host'):
            self.add_error('imap_host', 'Укажите IMAP-сервер для другого провайдера.')
        return cleaned

    def save(self, commit=True):
        mailbox = super().save(commit=False)
        if self.cleaned_data.get('password'):
            mailbox.set_password(self.cleaned_data['password'])
        if commit:
            mailbox.save()
        return mailbox


class MosaicMonitorForm(forms.ModelForm):
    sender_password = forms.CharField(
        label='Пароль приложения Gmail',
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={
            'autocomplete': 'new-password',
            'placeholder': 'Оставьте пустым, чтобы не менять',
        }),
        help_text='Хранится в зашифрованном виде и после сохранения не показывается.',
    )

    class Meta:
        model = MosaicMonitorConfiguration
        fields = (
            'enabled',
            'calendar_urls',
            'check_interval_minutes',
            'months_ahead',
            'sender_email',
            'recipients',
            'subject_prefix',
        )
        widgets = {
            'calendar_urls': forms.Textarea(attrs={'rows': 3}),
            'recipients': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': 'recipient@example.com\nsecond@example.com',
            }),
            'check_interval_minutes': forms.NumberInput(attrs={'min': 1, 'max': 1440}),
            'months_ahead': forms.NumberInput(attrs={'min': 0, 'max': 12}),
        }

    def clean_check_interval_minutes(self):
        value = self.cleaned_data['check_interval_minutes']
        if not 1 <= value <= 1440:
            raise forms.ValidationError('Интервал должен быть от 1 до 1440 минут.')
        return value

    def clean_months_ahead(self):
        value = self.cleaned_data['months_ahead']
        if not 0 <= value <= 12:
            raise forms.ValidationError('Можно проверять от 0 до 12 месяцев вперёд.')
        return value

    def clean_calendar_urls(self):
        values = []
        for value in (self.cleaned_data.get('calendar_urls') or '').splitlines():
            value = value.strip()
            if not value:
                continue
            parsed = urlsplit(value)
            if (
                parsed.scheme != 'https'
                or parsed.hostname != 'appointment.mosaicvisa.com'
                or not parsed.path.startswith('/calendar/')
            ):
                raise forms.ValidationError(
                    f'Недопустимая ссылка: {value}. Разрешены HTTPS-календари appointment.mosaicvisa.com.'
                )
            if value not in values:
                values.append(value)
        if not values:
            raise forms.ValidationError('Добавьте хотя бы одну страницу календаря.')
        return '\n'.join(values)

    def clean_recipients(self):
        raw = self.cleaned_data.get('recipients') or ''
        values = []
        for value in re.split(r'[\s,;]+', raw):
            value = value.strip().casefold()
            if not value:
                continue
            try:
                validate_email(value)
            except ValidationError:
                raise forms.ValidationError(f'Некорректный email получателя: {value}') from None
            if value not in values:
                values.append(value)
        if not values:
            raise forms.ValidationError('Добавьте хотя бы одного получателя.')
        return '\n'.join(values)

    def save(self, commit=True):
        config = super().save(commit=False)
        password = self.cleaned_data.get('sender_password')
        if password:
            config.set_sender_password(password)
        if commit:
            config.save()
        return config


class CampaignForm(forms.ModelForm):
    recipients = forms.MultipleChoiceField(label='Получатели', choices=(), widget=forms.CheckboxSelectMultiple)
    attachments = MultipleFileField(
        label='Вложения', required=False,
        widget=MultipleFileInput(attrs={
            'accept': '.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.rtf,.odt,.ods,.jpg,.jpeg,.png,.gif,.webp,.heic,.zip,.rar,.7z',
        }),
    )

    class Meta:
        model = Campaign
        fields = ('subject', 'body', 'recipients')
        widgets = {
            'subject': forms.TextInput(attrs={'placeholder': 'Тема письма'}),
            'body': forms.Textarea(attrs={'rows': 10, 'placeholder': 'Текст рассылки'}),
        }

    def __init__(self, *args, recipient_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['recipients'].choices = [(item['email'], item['email']) for item in recipient_choices]
