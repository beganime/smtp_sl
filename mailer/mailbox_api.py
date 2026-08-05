import json
import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import Client, Mailbox, MailboxSyncRun, Region
from .tasks import process_mailbox_sync_task


logger = logging.getLogger(__name__)

ACCOUNT_USERNAME = 'tmmail.ru'
PROVIDER_DEFAULTS = {
    'gmail': ('imap.gmail.com', 993, 'smtp.gmail.com', 465),
    'yandex': ('imap.yandex.ru', 993, 'smtp.yandex.ru', 465),
    'mailru': ('imap.mail.ru', 993, 'smtp.mail.ru', 465),
    'sanly': ('mail.sanly.tm', 993, 'mail.sanly.tm', 465),
}
PROVIDER_ALIASES = {
    'google': 'gmail',
    'gmail.com': 'gmail',
    'mail.ru': 'mailru',
    'mail': 'mailru',
    'yandex.ru': 'yandex',
    'sanly.tm': 'sanly',
}


def _response(request, payload=None, status=200):
    response = JsonResponse(payload or {}, status=status)
    origin = request.headers.get('Origin', '')
    if origin and origin == settings.MAILBOX_IMPORT_ALLOWED_ORIGIN:
        response['Access-Control-Allow-Origin'] = origin
        response['Vary'] = 'Origin'
    return response


def _authorized(request):
    expected = settings.MAILBOX_IMPORT_API_TOKEN
    supplied = request.headers.get('Authorization', '')
    if supplied.startswith('Bearer '):
        supplied = supplied[7:].strip()
    else:
        supplied = ''
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _json_body(request):
    if len(request.body) > 64 * 1024:
        raise ValueError('Request body is too large.')
    payload = json.loads(request.body or b'{}')
    if not isinstance(payload, dict):
        raise ValueError('JSON object expected.')
    return payload


def _bool_value(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in ('1', 'true', 'yes'):
        return True
    if isinstance(value, str) and value.casefold() in ('0', 'false', 'no'):
        return False
    raise ValueError('is_active must be a boolean.')


def _provider_for(email, supplied):
    supplied = (supplied or '').strip().casefold()
    supplied = PROVIDER_ALIASES.get(supplied, supplied)
    if supplied:
        if supplied not in dict(Mailbox.PROVIDER_CHOICES):
            raise ValueError('Unsupported provider.')
        return supplied
    domain = email.rpartition('@')[2]
    if domain == 'gmail.com':
        return 'gmail'
    if domain in ('mail.ru', 'list.ru', 'bk.ru', 'inbox.ru'):
        return 'mailru'
    if domain == 'sanly.tm':
        return 'sanly'
    if domain == 'ya.ru' or domain.startswith('yandex.'):
        return 'yandex'
    raise ValueError('provider is required for this email domain.')


def _positive_port(value, default):
    try:
        value = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise ValueError('Mail server port must be an integer.') from None
    if not 1 <= value <= 65535:
        raise ValueError('Mail server port is outside the valid range.')
    return value


def _prepare_mailbox_values(payload):
    email = str(payload.get('email') or '').strip().casefold()
    password = str(payload.get('password') or '')
    try:
        validate_email(email)
    except ValidationError:
        raise ValueError('A valid email is required.') from None
    if not password:
        raise ValueError('password is required.')
    if len(password) > 1000:
        raise ValueError('password is too long.')
    provider = _provider_for(email, payload.get('provider'))
    display_name = str(payload.get('display_name') or email.partition('@')[0]).strip()
    if len(display_name) > 160:
        raise ValueError('display_name is longer than 160 characters.')
    is_active = _bool_value(payload.get('is_active'), default=True)

    if provider in PROVIDER_DEFAULTS:
        imap_host, imap_port, smtp_host, smtp_port = PROVIDER_DEFAULTS[provider]
        imap_ssl = smtp_ssl = True
    else:
        imap_host = str(payload.get('imap_host') or '').strip()
        smtp_host = str(payload.get('smtp_host') or '').strip()
        if not imap_host or not smtp_host:
            raise ValueError('imap_host and smtp_host are required for provider=other.')
        imap_port = _positive_port(payload.get('imap_port'), 993)
        smtp_port = _positive_port(payload.get('smtp_port'), 465)
        imap_ssl = _bool_value(payload.get('imap_use_ssl'), default=True)
        smtp_ssl = _bool_value(payload.get('smtp_use_ssl'), default=True)
    return {
        'email': email,
        'password': password,
        'provider': provider,
        'display_name': display_name,
        'is_active': is_active,
        'imap_host': imap_host,
        'imap_port': imap_port,
        'imap_use_ssl': imap_ssl,
        'smtp_host': smtp_host,
        'smtp_port': smtp_port,
        'smtp_use_ssl': smtp_ssl,
    }


def _queue_manager_sync(manager):
    current = (
        MailboxSyncRun.objects
        .filter(manager=manager, status__in=('queued', 'running'))
        .order_by('-created_at')
        .first()
    )
    if current:
        return current, False
    sync_run = MailboxSyncRun.objects.create(manager=manager)
    process_mailbox_sync_task(sync_run.pk)
    return sync_run, True


@csrf_exempt
def create_mailbox(request):
    origin = request.headers.get('Origin', '')
    if origin and origin != settings.MAILBOX_IMPORT_ALLOWED_ORIGIN:
        return _response(request, {'error': 'Origin is not allowed.'}, status=403)
    if request.method == 'OPTIONS':
        response = _response(request, {}, status=204)
        response['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
        response['Access-Control-Max-Age'] = '86400'
        return response
    if request.method != 'POST':
        return _response(request, {'error': 'POST required.'}, status=405)
    if not _authorized(request):
        return _response(request, {'error': 'Unauthorized.'}, status=401)
    try:
        payload = _json_body(request)
        values = _prepare_mailbox_values(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(request, {'error': str(exc)}, status=400)

    manager = (
        get_user_model().objects
        .select_related('manager_profile')
        .filter(username=ACCOUNT_USERNAME, is_active=True)
        .first()
    )
    if manager is None or not hasattr(manager, 'manager_profile'):
        return _response(request, {'error': 'Target account is not configured.'}, status=503)
    existing = Mailbox.objects.filter(email__iexact=values['email']).first()
    if existing:
        return _response(request, {
            'status': 'exists',
            'error': 'Mailbox already exists.',
            'mailbox': {
                'id': existing.pk,
                'email': existing.email,
                'provider': existing.provider,
                'is_active': existing.is_active,
            },
        }, status=409)

    profile = manager.manager_profile
    region = profile.region or Region.objects.filter(name='Лебап').first() or Region.objects.first()
    try:
        with transaction.atomic():
            client, _ = Client.objects.get_or_create(
                email=values['email'],
                defaults={'full_name': values['display_name']},
            )
            mailbox = Mailbox(
                manager=manager,
                client=client,
                region=region,
                owner_phone=profile.phone,
                email=values['email'],
                display_name=values['display_name'],
                provider=values['provider'],
                imap_host=values['imap_host'],
                imap_port=values['imap_port'],
                imap_use_ssl=values['imap_use_ssl'],
                smtp_host=values['smtp_host'],
                smtp_port=values['smtp_port'],
                smtp_use_ssl=values['smtp_use_ssl'],
                is_active=values['is_active'],
            )
            mailbox.set_password(values['password'])
            mailbox.save()
    except IntegrityError:
        return _response(request, {
            'status': 'exists',
            'error': 'Mailbox already exists.',
        }, status=409)

    sync_run = None
    sync_queued = False
    if mailbox.is_active:
        try:
            sync_run, sync_queued = _queue_manager_sync(manager)
        except Exception:
            logger.exception('Could not queue sync for API-created mailbox %s.', mailbox.pk)
    logger.info('Mailbox %s created through import API as id=%s.', mailbox.email, mailbox.pk)
    return _response(request, {
        'status': 'created',
        'mailbox': {
            'id': mailbox.pk,
            'email': mailbox.email,
            'display_name': mailbox.display_name,
            'provider': mailbox.provider,
            'is_active': mailbox.is_active,
            'account': ACCOUNT_USERNAME,
        },
        'sync': {
            'queued': sync_queued,
            'run_id': sync_run.pk if sync_run else None,
        },
    }, status=201)
