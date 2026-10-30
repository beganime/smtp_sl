"""Publish bounded mail metadata to the canonical ManagerSL student registry."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import requests

from django.conf import settings

from .models import InboundMessage


class ManagerRegistryError(RuntimeError):
    pass


def manager_registry_is_configured():
    return bool(
        getattr(settings, 'MANAGER_SL_EMAIL_REGISTRY_URL', '')
        and getattr(settings, 'MANAGER_SL_EMAIL_REGISTRY_TOKEN', '')
    )


def _importance(analysis):
    if not analysis or analysis.status != 'completed' or not analysis.is_important:
        return 'normal'
    return 'urgent' if analysis.importance_score >= 85 else 'important'


def build_manager_payload(message):
    analysis = getattr(message, 'ai_analysis', None)
    source_version = 2 if analysis and analysis.status == 'completed' else 1
    source_id = f'{message.mailbox_id}:{message.imap_folder}:{message.external_uid}'
    source_url_template = getattr(settings, 'MANAGER_SL_EMAIL_SOURCE_URL', '')
    try:
        source_url = source_url_template.format(message_id=message.pk)
    except (KeyError, ValueError):
        source_url = ''
    return {
        'event_id': str(uuid5(NAMESPACE_URL, f'smtp_sl:{source_id}:v{source_version}')),
        'source_service': 'smtp_sl',
        'source_id': source_id,
        'source_version': source_version,
        'sl_id': message.mailbox.canonical_sl_id,
        'mailbox': message.mailbox.email,
        'sender_email': message.sender_email,
        'sender_name': message.sender_name,
        'recipient_email': message.recipient_email,
        'subject': message.subject,
        'received_at': message.received_at.isoformat(),
        'category': message.category,
        'importance': _importance(analysis),
        'university_name': getattr(analysis, 'university_name', '') if analysis else '',
        'attachment_count': message.attachments.count(),
        # SMTP_SL remains the source of truth for the full body and attachments.
        'body_preview': (message.body_text or '').replace('\x00', '').strip()[:1000],
        'source_url': source_url,
        'is_read': message.is_read,
        'is_replied': message.is_replied,
        'processed': bool(message.is_read or message.is_replied),
    }


def sync_message_to_manager(message_id):
    if not manager_registry_is_configured():
        raise ManagerRegistryError('ManagerSL email registry is not configured.')
    message = InboundMessage.objects.select_related('mailbox', 'ai_analysis').get(pk=message_id)
    try:
        response = requests.post(
            settings.MANAGER_SL_EMAIL_REGISTRY_URL,
            json=build_manager_payload(message),
            headers={'Authorization': f'Bearer {settings.MANAGER_SL_EMAIL_REGISTRY_TOKEN}'},
            timeout=max(1, int(settings.MANAGER_SL_EMAIL_SYNC_TIMEOUT)),
        )
    except requests.RequestException as exc:
        raise ManagerRegistryError(f'ManagerSL request failed: {exc}') from exc
    if response.status_code not in (200, 201):
        detail = ''
        try:
            detail = str(response.json().get('detail') or '')
        except (TypeError, ValueError, AttributeError):
            detail = response.text[:300]
        raise ManagerRegistryError(
            f'ManagerSL returned HTTP {response.status_code}: {detail[:300]}'
        )
    return response.json()
