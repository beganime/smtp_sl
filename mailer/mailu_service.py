import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings


class MailServiceError(Exception):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def _message(body, fallback):
    if isinstance(body, dict):
        return str(body.get('message') or body.get('error') or fallback)
    return fallback


def _request_json(url, *, method='GET', token='', payload=None, timeout=20):
    data = None
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b'{}')
    except HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw or b'{}')
        except (TypeError, ValueError):
            body = {'message': raw.decode('utf-8', errors='replace')[:500]}
        return exc.code, body
    except (URLError, TimeoutError, OSError) as exc:
        raise MailServiceError(f'Mail service is temporarily unavailable: {exc}') from exc


def _mailu_url(path):
    return f'{settings.MAILU_API_BASE_URL.rstrip("/")}/{path.lstrip("/")}'


def list_mailu_users():
    status, body = _request_json(_mailu_url('user'), token=settings.MAILU_API_TOKEN)
    if status != 200 or not isinstance(body, list):
        raise MailServiceError(_message(body, 'Mailu did not return a mailbox list.'), status=status)
    return {
        str(item.get('email') or '').strip().casefold()
        for item in body
        if isinstance(item, dict) and item.get('email')
    }


def create_mailu_user(*, email, password, display_name):
    status, body = _request_json(
        _mailu_url('user'),
        method='POST',
        token=settings.MAILU_API_TOKEN,
        payload={
            'email': email,
            'raw_password': password,
            'displayed_name': display_name,
            'comment': 'Created by ManagerSL approval workflow',
            'quota_bytes': settings.TMMAIL_DEFAULT_QUOTA_BYTES,
            'enabled': True,
            'change_pw_next_login': False,
            'enable_imap': True,
            'enable_pop': False,
            'allow_spoofing': False,
            'spam_enabled': True,
        },
    )
    if status != 200:
        raise MailServiceError(_message(body, 'Could not create mailbox in Mailu.'), status=status)
    return body


def delete_mailu_user(email, *, allow_missing=False):
    status, body = _request_json(
        _mailu_url(f'user/{quote(email, safe="@")}'),
        method='DELETE',
        token=settings.MAILU_API_TOKEN,
    )
    if status == 404 and allow_missing:
        return False
    if status != 200:
        raise MailServiceError(_message(body, 'Could not remove mailbox from Mailu.'), status=status)
    return True


def register_mailbox(*, email, password, display_name):
    status, body = _request_json(
        settings.TMMAIL_REGISTRY_API_URL,
        method='POST',
        token=settings.MAILBOX_IMPORT_API_TOKEN,
        payload={
            'email': email,
            'password': password,
            'provider': 'other',
            'display_name': display_name,
            'is_active': True,
            'imap_host': 'mail.tmmail.ru',
            'imap_port': 993,
            'imap_use_ssl': True,
            'smtp_host': 'mail.tmmail.ru',
            'smtp_port': 465,
            'smtp_use_ssl': True,
        },
    )
    if status not in (201, 409):
        raise MailServiceError(_message(body, 'Could not add mailbox to SMTP_SL.'), status=status)
    return body
