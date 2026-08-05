from django.core.management.base import BaseCommand

from mailer.models import MailboxSyncRun
from mailer.tasks import process_mailbox_sync_task


class Command(BaseCommand):
    help = 'Ставит последовательную синхронизацию всех ящиков в фоновую очередь.'

    def handle(self, *args, **options):
        active = MailboxSyncRun.objects.filter(
            manager__isnull=True,
            status__in=('queued', 'running'),
        ).first()
        if active:
            self.stdout.write(f'Синхронизация уже поставлена в очередь: запуск #{active.pk}.')
            return

        sync_run = MailboxSyncRun.objects.create()
        process_mailbox_sync_task(sync_run.pk)
        self.stdout.write(self.style.SUCCESS(f'Синхронизация поставлена в очередь: запуск #{sync_run.pk}.'))
