import csv
import io
import re
import sys
from collections import Counter
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mailer.models import Client, Mailbox, ManagerProfile


EMAIL_TOKEN = re.compile(r'^[^@\s]+@[^@\s]+$')
ASCII_PASSWORD = re.compile(r'^[\x21-\x7e]{6,}$')


def parse_mailbox_rows(text):
    """Parse a whitespace-separated sequence of: full name, email, password."""
    tokens = text.split()
    email_indexes = [index for index, token in enumerate(tokens) if '@' in token]
    previous_end = 0

    for index in email_indexes:
        raw_email = tokens[index].strip(' ,;').replace(',', '.')
        password = tokens[index + 1] if index + 1 < len(tokens) else ''
        password_is_valid = bool(ASCII_PASSWORD.fullmatch(password)) and '@' not in password
        full_name = ' '.join(tokens[previous_end:index]).strip()

        if EMAIL_TOKEN.fullmatch(raw_email) and password_is_valid:
            yield full_name, raw_email.lower(), password
            previous_end = index + 2
        else:
            # A malformed row must not consume the first word of the next name.
            previous_end = index + 1


def parse_csv_rows(text):
    reader = csv.reader(io.StringIO(text.lstrip('\ufeff')))
    for row_number, row in enumerate(reader):
        if len(row) < 2:
            continue
        email = row[0].strip().lower()
        password = row[1].strip()
        if row_number == 0 and '@' not in email:
            continue
        if EMAIL_TOKEN.fullmatch(email) and password:
            yield '', email, password


def parse_sanly_input(text):
    first_line = next((line for line in text.splitlines() if line.strip()), '')
    parser = parse_csv_rows if ',' in first_line else parse_mailbox_rows
    yield from parser(text)


class Command(BaseCommand):
    help = 'Создаёт отдельный аккаунт Sanly.tm и импортирует в него ящики @sanly.tm.'

    def add_arguments(self, parser):
        parser.add_argument(
            'source',
            help='UTF-8 файл/CSV со строками «ФИО email пароль» или "-" для чтения CSV из stdin',
        )
        parser.add_argument('--login', default='Sanly.tm')
        parser.add_argument('--mobile', default='Sanly.tm')
        parser.add_argument('--device', default='Sanly.tm')
        parser.add_argument('--account-password', default='Sanly.tm')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        source_arg = options['source']
        if source_arg == '-':
            source_text = sys.stdin.read()
        else:
            source = Path(source_arg)
            if not source.is_file():
                raise CommandError(f'Файл не найден: {source}')
            source_text = source.read_text(encoding='utf-8-sig')

        rows = [
            row for row in parse_sanly_input(source_text)
            if row[1].endswith('@sanly.tm')
        ]
        unique_rows = {email: (name, email, password) for name, email, password in rows}
        if not unique_rows:
            raise CommandError('В файле не найдено ни одного корректного адреса @sanly.tm.')

        input_counts = Counter(email for _, email, _ in rows)
        input_duplicates = sorted(email for email, count in input_counts.items() if count > 1)
        existing = {
            email.lower()
            for email in Mailbox.objects.filter(email__in=unique_rows).values_list('email', flat=True)
        }
        self.stdout.write(
            f'Найдено Sanly: {len(rows)}; уникальных: {len(unique_rows)}; '
            f'уже существуют: {len(existing)}; будет добавлено: {len(unique_rows) - len(existing)}.'
        )
        self.stdout.write(
            'Дубли внутри файла: ' + (', '.join(input_duplicates) if input_duplicates else 'нет')
        )
        self.stdout.write(
            'Уже есть в базе: ' + (', '.join(sorted(existing)) if existing else 'нет')
        )
        if options['dry_run']:
            return

        User = get_user_model()
        with transaction.atomic():
            user, created = User.objects.get_or_create(username=options['login'])
            user.set_password(options['account_password'])
            user.is_active = True
            user.save(update_fields=('password', 'is_active'))
            ManagerProfile.objects.update_or_create(
                user=user,
                defaults={
                    'phone': options['mobile'],
                    'device': options['device'],
                    'city': '',
                    'region': None,
                },
            )

            added = 0
            for full_name, email, password in unique_rows.values():
                if email in existing:
                    continue
                display_name = full_name or email.rpartition('@')[0]
                client, _ = Client.objects.get_or_create(
                    email=email,
                    defaults={'full_name': display_name},
                )
                mailbox = Mailbox(
                    manager=user,
                    client=client,
                    owner_phone=options['mobile'],
                    email=email,
                    display_name=display_name,
                    provider='sanly',
                    imap_host='mail.sanly.tm',
                    imap_port=993,
                    imap_use_ssl=True,
                    smtp_host='mail.sanly.tm',
                    smtp_port=465,
                    smtp_use_ssl=True,
                )
                mailbox.set_password(password)
                mailbox.save()
                added += 1

        action = 'создан' if created else 'обновлён'
        self.stdout.write(self.style.SUCCESS(
            f'Аккаунт {options["login"]} {action}. Добавлено ящиков: {added}.'
        ))
