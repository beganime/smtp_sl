from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from mailer.models import InboundMessage


class Command(BaseCommand):
    help = 'Удаляет входящие письма старше установленного срока хранения.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None, help='Срок хранения в днях')
        parser.add_argument('--batch-size', type=int, default=500, help='Размер одной короткой транзакции')
        parser.add_argument('--dry-run', action='store_true', help='Только показать количество без удаления')

    def handle(self, *args, **options):
        days = options['days'] if options['days'] is not None else settings.MAIL_RETENTION_DAYS
        batch_size = options['batch_size']
        if days < 1:
            raise CommandError('Срок хранения должен быть не меньше одного дня.')
        if batch_size < 1:
            raise CommandError('Размер пакета должен быть положительным.')

        cutoff = timezone.now() - timedelta(days=days)
        queryset = InboundMessage.objects.filter(received_at__lt=cutoff)
        count = queryset.count()
        self.stdout.write(f'Граница хранения: {cutoff.isoformat()}; найдено старых писем: {count}.')
        if options['dry_run']:
            return

        deleted_messages = 0
        while True:
            ids = list(queryset.order_by('pk').values_list('pk', flat=True)[:batch_size])
            if not ids:
                break
            batch_count = len(ids)
            InboundMessage.objects.filter(pk__in=ids).delete()
            deleted_messages += batch_count
            self.stdout.write(f'Удалено: {deleted_messages}/{count}')
            close_old_connections()

        self.stdout.write(self.style.SUCCESS(f'Очистка завершена. Удалено писем: {deleted_messages}.'))
