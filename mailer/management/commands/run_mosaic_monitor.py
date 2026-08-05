import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from mailer.models import MosaicMonitorConfiguration
from mailer.mosaic import check_mosaic_calendars


class Command(BaseCommand):
    help = 'Continuously monitor Mosaic Visa appointment calendars.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Run one check and exit.',
        )

    def handle(self, *args, **options):
        once = options['once']
        self.stdout.write('Mosaic Visa monitor started.')
        while True:
            close_old_connections()
            config = MosaicMonitorConfiguration.load()
            should_run = config.enabled
            if should_run and config.last_checked_at:
                due_at = config.last_checked_at + timedelta(
                    minutes=max(1, int(config.check_interval_minutes)),
                )
                should_run = timezone.now() >= due_at
            if should_run:
                try:
                    result = check_mosaic_calendars(config)
                    if result['baseline']:
                        self.stdout.write('Mosaic baseline snapshot stored.')
                    elif result['changed']:
                        self.stdout.write(
                            f'Mosaic changes detected: {len(result["changes"])}.'
                        )
                    else:
                        self.stdout.write('Mosaic check completed: no changes.')
                except Exception as exc:
                    self.stderr.write(f'Mosaic check failed: {exc}')
            if once:
                return
            close_old_connections()
            time.sleep(30)
