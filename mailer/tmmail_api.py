import json
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .mailu_service import (
    MailServiceError,
    create_mailu_user,
    delete_mailu_user,
    list_mailu_users,
    register_mailbox,
)
from .models import Mailbox


def _authorized(request):
    supplied = request.headers.get('Authorization', '')
    supplied = supplied[7:].strip() if supplied.startswith('Bearer ') else ''
    expected = settings.TMMAIL_PROVISION_API_TOKEN
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _payload(request):
    if len(request.body) > 64 * 1024:
        raise ValueError('Request body is too large.')
    value = json.loads(request.body or b'{}')
    if not isinstance(value, dict):
        raise ValueError('JSON object expected.')
    return value


@csrf_exempt
def provision_mailbox(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required.'}, status=405)
    if not _authorized(request):
        return JsonResponse({'error': 'Unauthorized.'}, status=401)
    if not settings.MAILU_API_TOKEN or not settings.MAILBOX_IMPORT_API_TOKEN:
        return JsonResponse({'error': 'Mail provisioning is not configured.'}, status=503)

    try:
        payload = _payload(request)
        event_id = str(payload.get('event_id') or '').strip()
        sl_id = str(payload.get('sl_id') or '').strip()
        email = str(payload.get('email') or '').strip().casefold()
        password = str(payload.get('password') or '')
        display_name = str(payload.get('display_name') or '').strip()
        if not event_id or len(event_id) > 100:
            raise ValueError('event_id is required and must not exceed 100 characters.')
        if not sl_id or len(sl_id) > 32:
            raise ValueError('sl_id is required and must not exceed 32 characters.')
        validate_email(email)
        if email.rpartition('@')[2] != settings.TMMAIL_MAILBOX_DOMAIN:
            raise ValueError(f'Only @{settings.TMMAIL_MAILBOX_DOMAIN} mailboxes are allowed.')
        if not password or len(password) > 1000:
            raise ValueError('A valid password is required.')
        if not display_name or len(display_name) > 160:
            raise ValueError('display_name is required and must not exceed 160 characters.')
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    existing = Mailbox.objects.filter(email__iexact=email).first()
    if existing:
        return JsonResponse({
            'status': 'exists',
            'event_id': event_id,
            'sl_id': sl_id,
            'mailbox_id': existing.pk,
            'email': existing.email,
        })

    mailu_created = False
    try:
        if email not in list_mailu_users():
            create_mailu_user(email=email, password=password, display_name=display_name)
            mailu_created = True
        result = register_mailbox(email=email, password=password, display_name=display_name)
    except MailServiceError as exc:
        if mailu_created:
            try:
                delete_mailu_user(email, allow_missing=True)
            except MailServiceError:
                pass
        return JsonResponse({'error': str(exc), 'upstream_status': exc.status}, status=502)

    mailbox = Mailbox.objects.filter(email__iexact=email).first()
    return JsonResponse({
        'status': 'created',
        'event_id': event_id,
        'sl_id': sl_id,
        'mailbox_id': mailbox.pk if mailbox else None,
        'email': email,
        'registry': result,
    }, status=201)
