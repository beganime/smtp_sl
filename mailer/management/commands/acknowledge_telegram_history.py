from django.core.management.base import BaseCommand
from django.utils import timezone

from mailer.models import InboundMessage


class Command(BaseCommand):
    help = 'Снимает очередь Telegram-уведомлений со всех уже загруженных писем.'

    def handle(self, *args, **options):
        pending = InboundMessage.objects.filter(telegram_notification_pending=True)
        count = pending.count()
        pending.update(
            telegram_notification_pending=False,
            telegram_notified_at=timezone.now(),
            telegram_notification_error='',
        )
        self.stdout.write(self.style.SUCCESS(f'Исторических уведомлений подтверждено: {count}.'))
