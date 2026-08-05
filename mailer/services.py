from collections import defaultdict
from datetime import datetime, timedelta, timezone as datetime_timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
import imaplib
import mimetypes
from pathlib import Path
import re

from django.apps import apps
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.files.base import ContentFile
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    Campaign, CampaignAttachment, InboundAttachment, InboundMessage, Mailbox,
    OutgoingAttachment, OutgoingMessage,
)
from .mail_content import html_to_text
from .telegram import is_university_message, retry_pending_telegram_notifications, telegram_is_configured


def classify_inbound_message(sender_email='', subject=''):
    """Keep Google service notifications out of the university response queue."""
    address = (sender_email or '').strip().casefold()
    domain = address.rpartition('@')[2]
    google_domains = (
        'google.com', 'accounts.google.com', 'notifications.google.com',
        'calendar.google.com', 'drive.google.com',
    )
    if any(domain == item or domain.endswith('.' + item) for item in google_domains):
        return 'google'
    return 'primary'


def _message_content(message):
    body_text = body_html = ''
    if message.is_multipart():
        plain_part = message.get_body(preferencelist=('plain',))
        html_part = message.get_body(preferencelist=('html',))
        if plain_part:
            body_text = plain_part.get_content()
        if html_part:
            body_html = html_part.get_content()
    else:
        content = message.get_content()
        if message.get_content_type() == 'text/html':
            body_html = content
        else:
            body_text = content
    if not body_text and body_html:
        body_text = html_to_text(body_html)
    return body_text, body_html


def _imap_folder_specs(connection, mailbox):
    """Return selectable IMAP folders using RFC 6154 special-use flags."""
    folders = [('INBOX', 'INBOX', 'primary')]
    seen_categories = {'primary'}
    try:
        status, rows = connection.list()
    except Exception:
        status, rows = 'NO', []
    if status == 'OK':
        for raw in rows or ():
            if not raw:
                continue
            text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
            match = re.match(r'^\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', text)
            if not match:
                continue
            flags = {item.casefold() for item in match.group(1).split()}
            category = None
            if '\\spam' in flags or '\\junk' in flags:
                category = 'spam'
            elif '\\sent' in flags:
                category = 'sent'
            if not category or category in seen_categories:
                continue
            select_arg = match.group(2).strip()
            folder_name = select_arg
            if folder_name.startswith('"') and folder_name.endswith('"'):
                folder_name = folder_name[1:-1].replace(r'\"', '"').replace(r'\\', '\\')
            folders.append((select_arg, folder_name, category))
            seen_categories.add(category)

    # Gmail normally advertises special-use flags. These fallbacks cover
    # accounts where LIST is restricted but the standard Gmail names work.
    if mailbox.provider == 'gmail':
        if 'spam' not in seen_categories:
            folders.append(('"[Gmail]/Spam"', '[Gmail]/Spam', 'spam'))
        if 'sent' not in seen_categories:
            folders.append(('"[Gmail]/Sent Mail"', '[Gmail]/Sent Mail', 'sent'))
    return folders


def _attachment_payloads(message):
    attachments = []
    for part_index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != 'attachment':
            continue
        safe_name = str(filename or f'attachment-{part_index}').replace('\\', '/').rsplit('/', 1)[-1][:255]
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b''
        attachments.append((part_index, safe_name, part.get_content_type()[:160], payload))
    return attachments


def _sync_imap_folder(
    connection, mailbox, select_arg, folder_name, folder_category, limit,
    telegram_notification_after,
):
    status, _ = connection.select(select_arg, readonly=True)
    if status != 'OK':
        if folder_category == 'primary':
            raise RuntimeError('Папка INBOX недоступна.')
        return 0
    status, data = connection.uid('search', None, 'ALL')
    if status != 'OK':
        raise RuntimeError(f'IMAP-сервер не вернул список писем для папки {folder_name}.')
    uids = (data[0] or b'').split()[-limit:]
    uid_texts = [uid.decode() for uid in uids]
    processed = dict(
        InboundMessage.objects.filter(
            mailbox=mailbox, imap_folder=folder_name, external_uid__in=uid_texts,
        ).values_list('external_uid', 'attachments_synced')
    )
    imported = 0
    retention_cutoff = timezone.now() - timedelta(days=int(getattr(settings, 'MAIL_RETENTION_DAYS', 30)))
    for uid, uid_text in zip(uids, uid_texts):
        if processed.get(uid_text) is True:
            continue
        status, payload = connection.uid('fetch', uid, '(RFC822)')
        if status != 'OK' or not payload or not isinstance(payload[0], tuple):
            continue
        parsed = BytesParser(policy=policy.default).parsebytes(payload[0][1])
        sender_name, sender_email = parseaddr(str(parsed.get('From', '')))
        _, recipient_email = parseaddr(str(parsed.get('To', mailbox.email)))
        try:
            received_at = parsedate_to_datetime(str(parsed.get('Date', '')))
            if received_at is None:
                raise ValueError
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=datetime_timezone.utc)
        except (TypeError, ValueError, OverflowError):
            received_at = timezone.now()
        if received_at < retention_cutoff:
            continue

        subject = str(parsed.get('Subject') or 'Без темы')[:500]
        body_text, body_html = _message_content(parsed)
        category = folder_category
        if category == 'primary':
            category = classify_inbound_message(sender_email, subject)
        message, created = InboundMessage.objects.get_or_create(
            mailbox=mailbox,
            imap_folder=folder_name,
            external_uid=uid_text,
            defaults={
                'sender_name': sender_name,
                'sender_email': sender_email or 'unknown@invalid.local',
                'recipient_email': recipient_email or mailbox.email,
                'subject': subject,
                'body_text': body_text,
                'body_html': body_html,
                'received_at': received_at,
                'category': category,
                'telegram_notification_pending': False,
            },
        )
        if not message.attachments_synced:
            for part_index, name, content_type, content in _attachment_payloads(parsed):
                InboundAttachment.objects.get_or_create(
                    message=message,
                    part_index=part_index,
                    defaults={
                        'file': ContentFile(content, name=name),
                        'original_name': name,
                        'content_type': content_type,
                        'size': len(content),
                    },
                )
            message.attachments_synced = True
            message.save(update_fields=('attachments_synced',))
        should_notify = (
            created
            and category == 'primary'
            and telegram_is_configured()
            and received_at > telegram_notification_after
            and is_university_message(message)
        )
        if should_notify:
            message.telegram_notification_pending = True
            message.telegram_notification_error = ''
            message.save(update_fields=('telegram_notification_pending', 'telegram_notification_error'))
            from .tasks import notify_inbound_message_task
            notify_inbound_message_task(message.pk, priority=100)
        imported += int(created)
    return imported


def sync_mailbox(mailbox, limit=100):
    """Import Inbox, Spam and Sent sequentially through one IMAP connection."""
    if not mailbox.is_configured:
        raise ValueError('Для ящика не заполнены IMAP-сервер или пароль почты.')
    sync_started_at = timezone.now()
    notification_max_age = timedelta(
        days=int(getattr(settings, 'TELEGRAM_NOTIFICATION_MAX_AGE_DAYS', 3))
    )
    telegram_notification_after = max(
        mailbox.last_synced_at or sync_started_at,
        mailbox.telegram_notifications_after,
        sync_started_at - notification_max_age,
    )
    retry_pending_telegram_notifications(mailbox=mailbox)
    connection = None
    imap_timeout = int(getattr(settings, 'MAILBOX_IMAP_TIMEOUT', 60))
    try:
        if mailbox.imap_use_ssl:
            connection = imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port, timeout=imap_timeout)
        else:
            connection = imaplib.IMAP4(mailbox.imap_host, mailbox.imap_port, timeout=imap_timeout)
        connection.login(mailbox.email, mailbox.get_password())
        imported = 0
        for select_arg, folder_name, category in _imap_folder_specs(connection, mailbox):
            imported += _sync_imap_folder(
                connection,
                mailbox,
                select_arg,
                folder_name,
                category,
                limit,
                telegram_notification_after,
            )
        mailbox.last_synced_at = timezone.now()
        mailbox.sync_error = ''
        mailbox.save(update_fields=('last_synced_at', 'sync_error'))
        return imported
    except Exception as exc:
        mailbox.sync_error = str(exc)
        mailbox.save(update_fields=('sync_error',))
        raise
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass


def discover_emails(query=''):
    """Find unique addresses in every concrete database field that can contain email."""
    found = defaultdict(lambda: {'sources': set(), 'device': False})
    query = (query or '').strip().casefold()

    for model in apps.get_models():
        fields = [
            field for field in model._meta.concrete_fields
            if field.get_internal_type() == 'EmailField' or 'email' in field.name.casefold()
        ]
        for field in fields:
            try:
                values = model._default_manager.values_list(field.name, flat=True).distinct()
            except Exception:
                continue
            for raw in values:
                email = str(raw or '').strip().casefold()
                if not email or (query and query not in email):
                    continue
                try:
                    validate_email(email)
                except ValidationError:
                    continue
                found[email]['sources'].add(model._meta.verbose_name)
                if model is Mailbox:
                    found[email]['device'] = True

    return [
        {'email': email, 'sources': sorted(meta['sources']), 'device': meta['device']}
        for email, meta in sorted(found.items())
    ]


def _uploaded_file_data(uploaded):
    uploaded.seek(0)
    content = uploaded.read()
    uploaded.seek(0)
    name = Path(uploaded.name).name[:255]
    content_type = getattr(uploaded, 'content_type', '') or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return name, content, content_type


def save_campaign_attachments(campaign, uploaded_files):
    for uploaded in uploaded_files or ():
        name, content, content_type = _uploaded_file_data(uploaded)
        CampaignAttachment.objects.create(
            campaign=campaign,
            file=ContentFile(content, name=name),
            original_name=name,
            content_type=content_type,
            size=len(content),
        )


def send_reply(*, manager, message, body, attachments=()):
    subject = message.subject if message.subject.lower().startswith('re:') else f'Re: {message.subject}'
    record = OutgoingMessage.objects.create(
        manager=manager,
        mailbox=message.mailbox,
        in_reply_to=message,
        recipient_email=message.sender_email,
        subject=subject,
        body=body,
        status='failed',
    )
    try:
        prepared_attachments = []
        for uploaded in attachments or ():
            name, content, content_type = _uploaded_file_data(uploaded)
            OutgoingAttachment.objects.create(
                message=record,
                file=ContentFile(content, name=name),
                original_name=name,
                content_type=content_type,
                size=len(content),
            )
            prepared_attachments.append((name, content, content_type))
        mailbox = message.mailbox
        if mailbox.is_configured and mailbox.smtp_host:
            connection = get_connection(
                host=mailbox.smtp_host,
                port=mailbox.smtp_port,
                username=mailbox.email,
                password=mailbox.get_password(),
                use_ssl=mailbox.smtp_use_ssl,
                use_tls=not mailbox.smtp_use_ssl,
            )
            email = EmailMessage(subject=subject, body=body, from_email=mailbox.email, to=[message.sender_email], connection=connection)
        else:
            email = EmailMessage(subject=subject, body=body, to=[message.sender_email], reply_to=[mailbox.email])
        for name, content, content_type in prepared_attachments:
            email.attach(name, content, content_type)
        email.send(fail_silently=False)
        record.status = 'sent'
        record.error_message = ''
        message.is_replied = True
        message.save(update_fields=('is_replied',))
    except Exception as exc:
        record.error_message = str(exc)
    record.save(update_fields=('status', 'error_message'))
    return record


def send_direct_message(*, manager, mailbox, recipient_email, subject, body, attachments=(), allow_shared=False):
    """Send a new message to an arbitrary address through a manager-owned mailbox."""
    if mailbox.manager_id != manager.pk and not allow_shared:
        raise ValueError('Выбранный почтовый ящик не принадлежит текущему менеджеру.')
    if not mailbox.is_active:
        raise ValueError('Выбранный почтовый ящик отключён.')
    record = OutgoingMessage.objects.create(
        manager=manager,
        mailbox=mailbox,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        status='failed',
    )
    try:
        prepared_attachments = []
        for uploaded in attachments or ():
            name, content, content_type = _uploaded_file_data(uploaded)
            OutgoingAttachment.objects.create(
                message=record,
                file=ContentFile(content, name=name),
                original_name=name,
                content_type=content_type,
                size=len(content),
            )
            prepared_attachments.append((name, content, content_type))

        if mailbox.is_configured and mailbox.smtp_host:
            connection = get_connection(
                host=mailbox.smtp_host,
                port=mailbox.smtp_port,
                username=mailbox.email,
                password=mailbox.get_password(),
                use_ssl=mailbox.smtp_use_ssl,
                use_tls=not mailbox.smtp_use_ssl,
            )
            email = EmailMessage(
                subject=subject, body=body, from_email=mailbox.email,
                to=[recipient_email], connection=connection,
            )
        else:
            email = EmailMessage(
                subject=subject, body=body, to=[recipient_email], reply_to=[mailbox.email],
            )
        for name, content, content_type in prepared_attachments:
            email.attach(name, content, content_type)
        email.send(fail_silently=False)
        record.status = 'sent'
        record.error_message = ''
    except Exception as exc:
        record.error_message = str(exc)
    record.save(update_fields=('status', 'error_message'))
    return record


def send_campaign(campaign):
    sent = failed = 0
    connection = get_connection()
    prepared_attachments = []
    for attachment in campaign.attachments.all():
        with attachment.file.open('rb') as stored:
            prepared_attachments.append((attachment.original_name, stored.read(), attachment.content_type or 'application/octet-stream'))
    for recipient in campaign.recipients:
        try:
            email = EmailMessage(campaign.subject, campaign.body, to=[recipient], connection=connection)
            for name, content, content_type in prepared_attachments:
                email.attach(name, content, content_type)
            email.send(fail_silently=False)
            sent += 1
        except Exception:
            failed += 1
    campaign.sent_count = sent
    campaign.failed_count = failed
    campaign.status = 'failed' if failed else 'sent'
    campaign.save(update_fields=('sent_count', 'failed_count', 'status'))
    return campaign
