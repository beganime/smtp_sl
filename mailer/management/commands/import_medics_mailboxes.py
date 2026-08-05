import csv
import re
from collections import Counter
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mailer.models import Client, Mailbox, ManagerProfile, Region


EMAIL_RE = re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', re.IGNORECASE)
PROVIDER_SETTINGS = {
    'gmail.com': ('gmail', 'imap.gmail.com', 'smtp.gmail.com'),
    'mail.ru': ('mailru', 'imap.mail.ru', 'smtp.mail.ru'),
    'list.ru': ('mailru', 'imap.mail.ru', 'smtp.mail.ru'),
    'sanly.tm': ('sanly', 'mail.sanly.tm', 'mail.sanly.tm'),
}


def parse_medics_tsv(text):
    rows = csv.reader(text.lstrip('\ufeff').splitlines(), delimiter='\t')
    parsed = []
    for row_number, row in enumerate(rows, 1):
        if row_number == 1 and row and 'почт' in ' '.join(row).casefold():
            continue
        if len(row) < 3:
            continue
        full_name = row[0].strip()
        password = row[-1].strip()
        if not full_name or not password:
            continue
        for email in EMAIL_RE.findall(row[1]):
            parsed.append((full_name, email.casefold(), password))
    return parsed


class Command(BaseCommand):
    help = 'Импортирует смешанный список медицинских ящиков в отдельный аккаунт.'

    def add_arguments(self, parser):
        parser.add_argument('source', help='TSV-файл с колонками ФИО, Почта, Код')
        parser.add_argument('--login', default='sanly.tm/medics')
        parser.add_argument('--mobile', default='sanly.tm/medics')
        parser.add_argument('--device', default='sanly.tm/medics')
        parser.add_argument('--account-password', default='sanly.tm/medics')
        parser.add_argument('--region', default='Лебап')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        source = Path(options['source'])
        if not source.is_file():
            raise CommandError(f'Файл не найден: {source}')
        parsed = parse_medics_tsv(source.read_text(encoding='utf-8-sig'))
        counts = Counter(email for _, email, _ in parsed)
        duplicates = sorted(email for email, count in counts.items() if count > 1)
        unique = {}
        for full_name, email, password in parsed:
            unique[email] = (full_name, email, password)
        if not unique:
            raise CommandError('В файле не найдено корректных почтовых адресов.')

        existing = {
            mailbox.email.casefold(): mailbox
            for mailbox in Mailbox.objects.filter(email__in=unique).select_related('manager')
        }
        moved = sum(
            1 for mailbox in existing.values()
            if mailbox.manager.username != options['login']
        )
        self.stdout.write(
            f'Адресов: {len(parsed)}; уникальных: {len(unique)}; новых: '
            f'{len(unique) - len(existing)}; существующих: {len(existing)}; '
            f'будет перенесено: {moved}.'
        )
        self.stdout.write(
            'Дубли файла: ' + (', '.join(duplicates) if duplicates else 'нет')
        )
        if options['dry_run']:
            return

        User = get_user_model()
        with transaction.atomic():
            user, account_created = User.objects.get_or_create(username=options['login'])
            user.set_password(options['account_password'])
            user.is_active = True
            user.save(update_fields=('password', 'is_active'))
            region = Region.objects.filter(name=options['region']).first()
            ManagerProfile.objects.update_or_create(
                user=user,
                defaults={
                    'phone': options['mobile'],
                    'device': options['device'],
                    'city': region.city if region else '',
                    'region': region,
                },
            )

            created_count = 0
            moved_count = 0
            updated_count = 0
            for full_name, email, password in unique.values():
                domain = email.rpartition('@')[2]
                provider, imap_host, smtp_host = PROVIDER_SETTINGS.get(
                    domain,
                    ('other', '', ''),
                )
                client, _ = Client.objects.update_or_create(
                    email=email,
                    defaults={'full_name': full_name, 'is_active': True},
                )
                mailbox = existing.get(email)
                if mailbox is None:
                    mailbox = Mailbox(email=email)
                    created_count += 1
                elif mailbox.manager_id != user.pk:
                    moved_count += 1
                else:
                    updated_count += 1
                mailbox.manager = user
                mailbox.client = client
                mailbox.region = mailbox.region or region
                mailbox.owner_phone = options['mobile']
                mailbox.display_name = full_name
                mailbox.provider = provider
                mailbox.imap_host = imap_host
                mailbox.imap_port = 993
                mailbox.imap_use_ssl = True
                mailbox.smtp_host = smtp_host
                mailbox.smtp_port = 465
                mailbox.smtp_use_ssl = True
                mailbox.is_active = True
                mailbox.sync_error = ''
                mailbox.set_password(password)
                mailbox.save()

        action = 'создан' if account_created else 'обновлён'
        self.stdout.write(self.style.SUCCESS(
            f'Аккаунт {options["login"]} {action}. Новых: {created_count}; '
            f'перенесено: {moved_count}; обновлено в аккаунте: {updated_count}.'
        ))
