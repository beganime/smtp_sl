import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from mailer.models import Mailbox
from mailer.services import sync_mailbox


class Command(BaseCommand):
    help = 'Последовательно загружает новые письма из активных настроенных IMAP-ящиков.'

    def add_arguments(self, parser):
        parser.add_argument('--mailbox', type=int, help='ID одного почтового ящика')
        parser.add_argument('--limit', type=int, default=100, help='Максимум последних писем на ящик')
        parser.add_argument('--delay', type=float, default=2, help='Пауза между ящиками в секундах')
        parser.add_argument('--retries', type=int, default=2, help='Повторные попытки для одного ящика')
        parser.add_argument('--retry-delay', type=float, default=10, help='Пауза перед повторной попыткой')

    def handle(self, *args, **options):
        queryset = (
            Mailbox.objects
            .filter(is_active=True)
            .exclude(password_encrypted='')
            .order_by('pk')
        )
        if options['mailbox']:
            queryset = queryset.filter(pk=options['mailbox'])

        mailbox_ids = list(queryset.values_list('pk', flat=True))
        total = errors = processed = 0
        for mailbox_id in mailbox_ids:
            processed += 1
            close_old_connections()
            mailbox = Mailbox.objects.get(pk=mailbox_id)
            for attempt in range(options['retries'] + 1):
                close_old_connections()
                try:
                    imported = sync_mailbox(mailbox, limit=options['limit'])
                    total += imported
                    self.stdout.write(self.style.SUCCESS(f'{mailbox.email}: +{imported}'))
                    break
                except Exception as exc:
                    if attempt >= options['retries']:
                        errors += 1
                        self.stderr.write(self.style.ERROR(f'{mailbox.email}: {exc}'))
                        break
                    self.stderr.write(self.style.WARNING(
                        f'{mailbox.email}: попытка {attempt + 1} не удалась ({exc}); '
                        f'повтор через {options["retry_delay"]:g} сек.'
                    ))
                    time.sleep(options['retry_delay'])
            close_old_connections()
            if options['delay'] > 0:
                time.sleep(options['delay'])

        self.stdout.write(
            f'Готово. Обработано ящиков: {processed}; новых писем: {total}; ошибок: {errors}.'
        )
