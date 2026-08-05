from django.contrib import admin
from django import forms
from django.core.mail import send_mail
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from unfold.admin import ModelAdmin
from unfold.decorators import action, display # Добавили display
from .models import Client, EmailAIAnalysis, GeminiConfiguration, InboundAttachment, InboundMessage, Mailbox, MailboxSyncRun, ManagerProfile, MosaicMonitorConfiguration, MosaicMonitorEvent, Newsletter, Region, SendingLog
from .tasks import process_newsletter_task, queue_ai_history_task

@admin.register(Client)
class ClientAdmin(ModelAdmin):
    list_display = ('email', 'full_name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('email', 'full_name')

@admin.register(Newsletter)
class NewsletterAdmin(ModelAdmin):
    list_display = ('subject', 'status', 'created_at', 'scheduled_time')
    list_filter = ('status',)
    search_fields = ('subject',)
    filter_horizontal = ('recipients',)
    
    actions = ['send_newsletters_now']
    actions_detail = ['test_send']

    @action(description="🚀 Отправить выбранные рассылки сейчас")
    def send_newsletters_now(self, request, queryset):
        count = 0
        for newsletter in queryset:
            if newsletter.status != 'sent':
                process_newsletter_task(newsletter.id)
                count += 1
        
        self.message_user(
            request, 
            f"Запущена фоновая отправка для {count} рассылок. Письма будут уходить с интервалом в 3 секунды.", 
            level=messages.SUCCESS
        )

    @action(description="Тестовая отправка")
    def test_send(self, request, object_id):
        newsletter = self.get_object(request, object_id)
        admin_email = request.user.email
        
        if not admin_email:
            self.message_user(request, "У вашего аккаунта (admin) не указан email!", level=messages.ERROR)
            return redirect(request.META.get('HTTP_REFERER', '.'))

        try:
            send_mail(
                subject=f"[ТЕСТ] {newsletter.subject}",
                message=newsletter.message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[admin_email],
                html_message=newsletter.html_message if newsletter.html_message else None,
                fail_silently=False,
            )
            self.message_user(request, f"Тестовое письмо успешно отправлено на {admin_email}.", level=messages.SUCCESS)
        except Exception as e:
            self.message_user(request, f"Ошибка при отправке: {str(e)}", level=messages.ERROR)
            
        return redirect(request.META.get('HTTP_REFERER', '.'))

@admin.register(SendingLog)
class SendingLogAdmin(ModelAdmin):
    # Заменили 'status' на 'show_status' для вывода красивых плашек
    list_display = ('newsletter', 'client', 'show_status', 'sent_at')
    list_filter = ('status', 'sent_at')
    search_fields = ('newsletter__subject', 'client__email')
    readonly_fields = ('newsletter', 'client', 'sent_at', 'status', 'error_message')
    
    # Кастомное красивое отображение статуса отправки
    @display(description="Статус отправки", label=True)
    def show_status(self, obj):
        if obj.status:
            return "Доставлено", "success" # Зеленая плашка
        return "Ошибка", "danger" # Красная плашка
    
    def has_add_permission(self, request):
        return False


@admin.register(Mailbox)
class MailboxAdmin(ModelAdmin):
    list_display = ('email', 'display_name', 'region', 'owner_phone', 'provider', 'manager', 'is_active', 'last_synced_at')
    list_filter = ('region', 'provider', 'is_active')
    search_fields = ('email', 'display_name', 'owner_phone', 'client__full_name')
    readonly_fields = ('password_encrypted', 'last_synced_at', 'sync_error', 'created_at')


@admin.register(MailboxSyncRun)
class MailboxSyncRunAdmin(ModelAdmin):
    list_display = ('id', 'manager', 'status', 'processed', 'imported', 'errors', 'created_at', 'finished_at')
    list_filter = ('status',)
    readonly_fields = ('created_at', 'started_at', 'finished_at')


class GeminiConfigurationForm(forms.ModelForm):
    api_keys = forms.CharField(
        label='Gemini API-ключи',
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={
            'rows': 4,
            'autocomplete': 'new-password',
            'placeholder': 'Один ключ на строку. Сохранённые ключи здесь не показываются.',
        }),
        help_text='Ключи шифруются. Можно указать несколько ключей для переключения при ошибке лимита.',
    )
    clear_api_keys = forms.BooleanField(label='Удалить сохранённые ключи', required=False)
    analyze_existing = forms.BooleanField(
        label='Поставить письма за последние 7 дней в очередь анализа',
        required=False,
        initial=True,
        help_text='Письма старше 7 дней не передаются в Gemini. История анализируется без Telegram-уведомлений.',
    )

    class Meta:
        model = GeminiConfiguration
        fields = (
            'enabled', 'api_keys', 'clear_api_keys', 'model_name',
            'importance_threshold', 'context_messages', 'max_body_chars',
            'request_delay_seconds', 'custom_instructions',
        )

    def clean_api_keys(self):
        value = self.cleaned_data.get('api_keys', '')
        return [item.strip() for item in value.replace(',', '\n').splitlines() if item.strip()]

    def clean(self):
        cleaned = super().clean()
        threshold = cleaned.get('importance_threshold')
        if threshold is not None and not 0 <= threshold <= 100:
            self.add_error('importance_threshold', 'Укажите значение от 0 до 100.')
        context = cleaned.get('context_messages')
        if context is not None and not 1 <= context <= 20:
            self.add_error('context_messages', 'Укажите от 1 до 20 писем.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('clear_api_keys'):
            instance.set_api_keys([])
        elif self.cleaned_data.get('api_keys'):
            instance.set_api_keys(self.cleaned_data['api_keys'])
        if commit:
            instance.save()
        return instance


@admin.register(GeminiConfiguration)
class GeminiConfigurationAdmin(ModelAdmin):
    form = GeminiConfigurationForm
    list_display = ('model_name', 'enabled', 'api_key_count_display', 'last_success_at', 'updated_at')
    readonly_fields = ('api_key_count_display', 'last_success_at', 'last_error', 'updated_at')
    actions_detail = ('test_connection', 'queue_history')
    fieldsets = (
        ('Подключение', {'fields': (
            'enabled', 'api_keys', 'clear_api_keys', 'api_key_count_display',
            'model_name',
        )}),
        ('Правила', {'fields': (
            'importance_threshold', 'context_messages', 'max_body_chars',
            'request_delay_seconds', 'custom_instructions', 'analyze_existing',
        )}),
        ('Состояние', {'fields': ('last_success_at', 'last_error', 'updated_at')}),
    )

    @display(description='Сохранено ключей')
    def api_key_count_display(self, obj):
        return obj.api_key_count if obj else 0

    def has_add_permission(self, request):
        return not GeminiConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.enabled and obj.api_key_count and form.cleaned_data.get('analyze_existing'):
            queue_ai_history_task(priority=20)
            self.message_user(
                request,
                'Настройки сохранены. Анализ существующих писем поставлен в фоновую очередь.',
                level=messages.SUCCESS,
            )

    @action(description='Проверить подключение Gemini')
    def test_connection(self, request, object_id):
        from .ai import validate_gemini_configuration
        obj = self.get_object(request, object_id)
        try:
            result = validate_gemini_configuration(obj)
            self.message_user(
                request,
                f'Gemini доступен: {result.get("displayName") or result.get("name") or obj.model_name}.',
                level=messages.SUCCESS,
            )
        except Exception as exc:
            self.message_user(request, f'Ошибка Gemini: {exc}', level=messages.ERROR)
        return redirect(request.META.get('HTTP_REFERER', '.'))

    @action(description='Анализировать письма за последние 7 дней')
    def queue_history(self, request, object_id):
        obj = self.get_object(request, object_id)
        if not obj.enabled or not obj.api_key_count:
            self.message_user(request, 'Сначала включите AI и сохраните API-ключ.', level=messages.ERROR)
        else:
            queue_ai_history_task(priority=20)
            self.message_user(request, 'Письма за последние 7 дней поставлены в очередь без Telegram-уведомлений.', level=messages.SUCCESS)
        return redirect(request.META.get('HTTP_REFERER', '.'))


@admin.register(EmailAIAnalysis)
class EmailAIAnalysisAdmin(ModelAdmin):
    list_display = (
        'message', 'status', 'is_important', 'importance_score',
        'university_name', 'university_group', 'topic', 'analyzed_at',
    )
    list_filter = ('status', 'is_important', 'is_university', 'university_group', 'topic')
    search_fields = (
        'message__subject', 'message__sender_email', 'university_name',
        'summary', 'action_required', 'suggested_reply',
    )
    readonly_fields = (
        'message', 'status', 'is_important', 'importance_score', 'is_university',
        'university_name', 'university_group', 'topic', 'summary', 'reason',
        'action_required', 'suggested_reply', 'deadline', 'extracted_links', 'extracted_logins',
        'extracted_passwords', 'model_name', 'error_message', 'analyzed_at', 'created_at',
    )

    def has_add_permission(self, request):
        return False


@admin.register(Region)
class RegionAdmin(ModelAdmin):
    list_display = ('name', 'city', 'mailbox_count')
    search_fields = ('name', 'city')

    @display(description='Почтовых ящиков')
    def mailbox_count(self, obj):
        return obj.mailboxes.count()


@admin.register(InboundMessage)
class InboundMessageAdmin(ModelAdmin):
    list_display = ('subject', 'sender_email', 'mailbox', 'category', 'received_at', 'is_read', 'is_replied')
    list_filter = ('category', 'is_read', 'is_replied', 'mailbox')
    search_fields = ('subject', 'sender_email', 'body_text')
    readonly_fields = ('mailbox', 'external_uid', 'sender_name', 'sender_email', 'recipient_email', 'subject', 'body_text', 'body_html', 'received_at', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(InboundAttachment)
class InboundAttachmentAdmin(ModelAdmin):
    list_display = ('original_name', 'message', 'content_type', 'size', 'created_at')
    search_fields = ('original_name', 'message__subject', 'message__mailbox__email')
    readonly_fields = ('message', 'file', 'original_name', 'content_type', 'size', 'part_index', 'created_at')

    def has_add_permission(self, request):
        return False


@admin.register(ManagerProfile)
class ManagerProfileAdmin(ModelAdmin):
    list_display = ('phone', 'device', 'city', 'region', 'job_title', 'user')
    list_filter = ('region', 'city')
    search_fields = ('phone', 'device', 'city', 'region__name', 'user__username')


@admin.register(MosaicMonitorConfiguration)
class MosaicMonitorConfigurationAdmin(ModelAdmin):
    list_display = ('enabled', 'sender_email', 'check_interval_minutes', 'last_checked_at', 'last_changed_at')
    readonly_fields = (
        'sender_password_encrypted', 'last_snapshot', 'last_checked_at',
        'last_changed_at', 'last_notification_at', 'last_error', 'updated_at',
    )

    def has_add_permission(self, request):
        return not MosaicMonitorConfiguration.objects.exists()


@admin.register(MosaicMonitorEvent)
class MosaicMonitorEventAdmin(ModelAdmin):
    list_display = ('event_type', 'summary', 'notification_sent', 'created_at')
    list_filter = ('event_type', 'notification_sent')
    readonly_fields = ('event_type', 'summary', 'details', 'notification_sent', 'created_at')

    def has_add_permission(self, request):
        return False
    
# Прячем сломанную админку background_tasks
from background_task.models import Task, CompletedTask
admin.site.unregister(Task)
admin.site.unregister(CompletedTask)
