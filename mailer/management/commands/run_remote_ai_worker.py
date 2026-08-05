import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from mailer.ai import request_gemini_prompt
from mailer.models import GeminiConfiguration


class Command(BaseCommand):
    help = 'Analyze server-side email jobs through Gemini from this computer.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one job and exit.')
        parser.add_argument('--poll-seconds', type=float, default=10.0)

    def handle(self, *args, **options):
        token = settings.AI_WORKER_TOKEN
        server_url = settings.AI_WORKER_SERVER_URL
        if not token:
            raise CommandError('AI_WORKER_TOKEN is not configured on this computer.')

        config = GeminiConfiguration.load()
        keys = config.get_api_keys()
        if not keys:
            raise CommandError('Gemini API key is not saved in the local admin configuration.')

        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'SMTP_SL-Local-AI-Worker/1.0',
        }
        poll_seconds = max(2.0, min(float(options['poll_seconds']), 300.0))
        self.stdout.write(
            f'[{timezone.now().isoformat()}] Local AI worker connected to {server_url}.'
        )

        while True:
            job = None
            billing_depleted = False
            try:
                response = requests.post(
                    f'{server_url}/api/ai-worker/lease/',
                    headers=headers,
                    json={},
                    timeout=(10, 60),
                )
                if response.status_code == 204:
                    if options['once']:
                        self.stdout.write('No pending AI jobs.')
                        return
                    time.sleep(poll_seconds)
                    continue
                if not response.ok:
                    raise RuntimeError(
                        f'Lease HTTP {response.status_code}: {response.text[:500]}'
                    )
                job = response.json()
                self.stdout.write(
                    f'Analyzing job {job["job_id"]}: {job.get("subject", "")[:100]}'
                )
                result = self._analyze_with_retries(job, keys)
                submission = {'job_id': job['job_id'], 'analysis': result}
            except KeyboardInterrupt:
                self.stdout.write('Worker stopped.')
                return
            except Exception as exc:
                billing_depleted = 'prepayment credits are depleted' in str(exc).lower()
                if isinstance(job, dict) and job.get('job_id'):
                    submission = {'job_id': job['job_id'], 'error': str(exc)[:2000]}
                else:
                    self.stderr.write(f'Worker connection error: {exc}')
                    if options['once']:
                        raise CommandError(str(exc))
                    time.sleep(max(poll_seconds, 15.0))
                    continue

            try:
                response = requests.post(
                    f'{server_url}/api/ai-worker/submit/',
                    headers=headers,
                    json=submission,
                    timeout=(10, 60),
                )
                if not response.ok:
                    raise RuntimeError(
                        f'Submit HTTP {response.status_code}: {response.text[:500]}'
                    )
                status = response.json()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Job {submission["job_id"]}: {status.get("status")}, '
                        f'importance={status.get("score", "-")}.'
                    )
                )
                if billing_depleted:
                    self.stderr.write('BILLING_CREDITS_DEPLETED')
            except Exception as exc:
                self.stderr.write(f'Could not submit job result: {exc}')
                if options['once']:
                    raise CommandError(str(exc))
                time.sleep(max(poll_seconds, 15.0))

            if options['once']:
                return
            delay = max(0.0, min(float(job.get('request_delay_seconds') or 0), 60.0))
            if delay:
                time.sleep(delay)

    def _analyze_with_retries(self, job, keys):
        last_error = None
        for attempt in range(5):
            try:
                return request_gemini_prompt(job['prompt'], job['model'], keys)
            except Exception as exc:
                last_error = exc
                text = str(exc).lower()
                if 'prepayment credits are depleted' in text:
                    break
                transient = any(marker in text for marker in (
                    'http 429', 'http 500', 'http 502', 'http 503', 'http 504',
                    'timeout', 'timed out', 'connection', 'некорректный json',
                    'unterminated string',
                ))
                if not transient or attempt == 4:
                    break
                malformed_json = (
                    'некорректный json' in text or 'unterminated string' in text
                )
                base_delay = 5 if malformed_json else 30
                time.sleep(min(base_delay * (2 ** attempt), 300))
        raise RuntimeError(str(last_error))
