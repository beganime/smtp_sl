from django.core.management.base import BaseCommand, CommandError

from mailer.telegram import send_telegram_text


class Command(BaseCommand):
    help = 'Отправляет тестовое уведомление в настроенную Telegram-группу.'

    def handle(self, *args, **options):
        try:
            send_telegram_text(
                '✅ Telegram-уведомления SMTP_SL подключены.\n\n'
                'Новые входящие письма будут появляться в этой группе.'
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS('Тестовое уведомление отправлено.'))
