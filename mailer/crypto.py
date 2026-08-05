import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    configured_key = getattr(settings, 'MAILBOX_ENCRYPTION_KEY', '')
    if configured_key:
        key = configured_key.encode()
    else:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt_secret(value):
    return _fernet().encrypt(value.encode()).decode() if value else ''


def decrypt_secret(value):
    if not value:
        return ''
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError('Не удалось расшифровать пароль ящика. Проверьте MAILBOX_ENCRYPTION_KEY.') from exc
