import time
from background_task import background
from django.core.mail import get_connection, EmailMultiAlternatives
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import EmailAIAnalysis, GeminiConfiguration, InboundMessage, Mailbox, MailboxSyncRun, Newsletter, SendingLog
from .services import sync_mailbox

@background(schedule=0, queue='outgoing')
def process_newsletter_task(newsletter_id):
    try:
        newsletter = Newsletter.objects.get(id=newsletter_id)
        recipients = newsletter.recipients.filter(is_active=True)
        
        # 1. Открываем ЕДИНОЕ соединение с Яндексом
        connection = get_connection()
        connection.open()
        
        for client in recipients:
            # Проверяем, не отправляли ли мы уже письмо этому клиенту в этой рассылке
            # (защита от дублей, если скрипт перезапускался)
            if SendingLog.objects.filter(newsletter=newsletter, client=client, status=True).exists():
                continue

            try:
                # 2. Формируем письмо
                msg = EmailMultiAlternatives(
                    subject=newsletter.subject,
                    body=newsletter.message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[client.email],
                    connection=connection # Передаем открытое соединение!
                )
                
                # Прикрепляем HTML, если он есть
                if newsletter.html_message:
                    msg.attach_alternative(newsletter.html_message, "text/html")
                
                # Отправляем
                msg.send(fail_silently=False)
                
                # Логируем успех
                SendingLog.objects.create(
                    newsletter=newsletter, client=client, status=True
                )
                
            except Exception as e:
                # Логируем ошибку
                SendingLog.objects.create(
                    newsletter=newsletter, client=client, status=False, error_message=str(e)
                )
                
                # Если соединение разорвалось, пробуем переподключиться для следующих писем
                try:
                    connection.close()
                    connection.open()
                except:
                    pass
            
            # Пауза 3 секунды, чтобы Яндекс не ругался на скорость
            time.sleep(3)
        
        # 3. Закрываем соединение после цикла
        connection.close()
        
        # Меняем статус рассылки
        newsletter.status = 'sent'
        newsletter.save()

    except Newsletter.DoesNotExist:
        pass


@background(schedule=0, queue='ai-analysis')
def analyze_inbound_message_task(message_id, allow_telegram=False, retry_count=0):
    from .ai import analyze_inbound_message, ai_analysis_cutoff

    if not InboundMessage.objects.filter(
        pk=message_id,
        category='primary',
        received_at__gte=ai_analysis_cutoff(),
    ).exists():
        EmailAIAnalysis.objects.filter(
            message_id=message_id,
            status__in=('pending', 'processing', 'error'),
        ).delete()
        return
    existing = EmailAIAnalysis.objects.filter(message_id=message_id, status='completed').first()
    if existing:
        return
    analysis = analyze_inbound_message(message_id, allow_telegram=allow_telegram)
    if analysis is None:
        return
    transient_error = any(
        marker in (analysis.error_message or '').lower()
        for marker in (
            'http 429',
            'http 500',
            'http 502',
            'http 503',
            'http 504',
            'timed out',
            'timeout',
            'connection',
        )
    )
    if analysis.status == 'error' and transient_error and retry_count < 5:
        retry_delay = min(60 * (2 ** retry_count), 900)
        analyze_inbound_message_task(
            message_id,
            allow_telegram=allow_telegram,
            retry_count=retry_count + 1,
            schedule=retry_delay,
            priority=100 if allow_telegram else 0,
        )
    delay = max(0.0, min(float(GeminiConfiguration.load().request_delay_seconds or 0), 60.0))
    if delay:
        time.sleep(delay)
    close_old_connections()


@background(schedule=0, queue='telegram')
def notify_inbound_message_task(message_id, retry_count=0):
    from .telegram import is_university_message, notify_inbound_message

    close_old_connections()
    try:
        message = InboundMessage.objects.select_related('mailbox').get(
            pk=message_id,
            mailbox__is_active=True,
            telegram_notification_pending=True,
        )
    except InboundMessage.DoesNotExist:
        return
    if message.category != 'primary' or not is_university_message(message):
        message.telegram_notification_pending = False
        message.telegram_notification_error = ''
        message.save(update_fields=('telegram_notification_pending', 'telegram_notification_error'))
        return
    if not notify_inbound_message(message) and retry_count < 5:
        notify_inbound_message_task(
            message_id,
            retry_count=retry_count + 1,
            schedule=min(60 * (2 ** retry_count), 900),
            priority=100,
        )
    close_old_connections()


@background(schedule=0, queue='ai-analysis')
def queue_ai_history_task():
    """Queue recent unprocessed primary messages without Telegram backlog."""
    from .ai import ai_analysis_cutoff

    config = GeminiConfiguration.load()
    if not config.enabled or not config.get_api_keys():
        return
    cutoff = ai_analysis_cutoff()
    EmailAIAnalysis.objects.filter(
        message__received_at__lt=cutoff,
        status__in=('pending', 'processing', 'error'),
    ).delete()
    message_ids = list(
        InboundMessage.objects
        .filter(category='primary', received_at__gte=cutoff)
        .exclude(ai_analysis__status='completed')
        .order_by('-received_at')
        .values_list('pk', flat=True)
    )
    for message_id in message_ids:
        EmailAIAnalysis.objects.get_or_create(message_id=message_id)
        if not settings.AI_ANALYSIS_REMOTE_WORKER:
            analyze_inbound_message_task(message_id, allow_telegram=False, priority=0)
    close_old_connections()


@background(schedule=0, queue='mail-sync')
def process_mailbox_sync_task(sync_run_id, limit=100, delay=2, retries=2, retry_delay=10):
    """Synchronize mailboxes sequentially outside the web request."""
    close_old_connections()
    try:
        sync_run = MailboxSyncRun.objects.select_related('manager').get(pk=sync_run_id)
    except MailboxSyncRun.DoesNotExist:
        return

    sync_run.status = 'running'
    sync_run.started_at = timezone.now()
    sync_run.error_message = ''
    sync_run.save(update_fields=('status', 'started_at', 'error_message'))

    try:
        queryset = Mailbox.objects.filter(is_active=True).exclude(password_encrypted='').order_by('pk')
        if sync_run.manager_id:
            queryset = queryset.filter(manager_id=sync_run.manager_id)
        mailbox_ids = list(queryset.values_list('pk', flat=True))

        for mailbox_id in mailbox_ids:
            close_old_connections()
            mailbox = Mailbox.objects.get(pk=mailbox_id)
            imported = 0
            failed = False
            for attempt in range(retries + 1):
                try:
                    imported = sync_mailbox(mailbox, limit=limit)
                    break
                except Exception:
                    if attempt >= retries:
                        failed = True
                        break
                    time.sleep(retry_delay)

            sync_run.processed += 1
            sync_run.imported += imported
            if failed:
                sync_run.errors += 1
            sync_run.save(update_fields=('processed', 'imported', 'errors'))
            close_old_connections()
            if delay > 0 and mailbox_id != mailbox_ids[-1]:
                time.sleep(delay)

        sync_run.status = 'completed'
    except Exception as exc:
        sync_run.status = 'failed'
        sync_run.error_message = str(exc)
    finally:
        sync_run.finished_at = timezone.now()
        sync_run.save(update_fields=('status', 'error_message', 'finished_at'))
        close_old_connections()
