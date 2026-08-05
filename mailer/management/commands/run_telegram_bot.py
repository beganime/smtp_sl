import logging
import time

from django.core.management.base import BaseCommand

from mailer.telegram import (
    get_telegram_updates,
)
from mailer.telegram_bot_ui import TelegramBotUI


logger = logging.getLogger('mailer')


class Command(BaseCommand):
    help = 'Run the public interactive SMTP_SL Telegram bot.'

    def handle(self, *args, **options):
        bot = TelegramBotUI()
        try:
            bot.configure()
        except Exception as exc:
            # Command registration is optional. A temporary Telegram outage must
            # not keep systemd in a restart loop; polling below will retry.
            logger.warning('Could not configure Telegram bot commands: %s', exc)
        offset = None
        self.stdout.write('Public Telegram bot started.')
        while True:
            try:
                updates = get_telegram_updates(offset=offset, timeout=25)
                for update in updates:
                    offset = int(update['update_id']) + 1
                    bot.handle_update(update)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logger.exception('Telegram search bot polling failed: %s', exc)
                time.sleep(5)
