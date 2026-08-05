import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .ai import (
    SYSTEM_INSTRUCTION,
    RESPONSE_SCHEMA,
    ai_analysis_cutoff,
    apply_gemini_analysis,
    build_analysis_prompt,
    record_gemini_error,
    should_analyze_with_gemini,
)
from .models import EmailAIAnalysis, GeminiConfiguration


def _authorized(request):
    expected = settings.AI_WORKER_TOKEN
    supplied = request.headers.get('Authorization', '')
    if supplied.startswith('Bearer '):
        supplied = supplied[7:].strip()
    else:
        supplied = ''
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _json_body(request):
    if len(request.body) > 512 * 1024:
        raise ValueError('Request is too large.')
    value = json.loads(request.body or b'{}')
    if not isinstance(value, dict):
        raise ValueError('JSON object expected.')
    return value


@csrf_exempt
def lease_ai_job(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required.'}, status=405)
    if not _authorized(request):
        return JsonResponse({'error': 'Unauthorized.'}, status=401)
    if not settings.AI_ANALYSIS_REMOTE_WORKER:
        return JsonResponse({'error': 'Remote AI worker is disabled.'}, status=503)

    config = GeminiConfiguration.load()
    if not config.enabled:
        return JsonResponse({'error': 'AI analysis is disabled.'}, status=503)

    now = timezone.now()
    stale_processing = now - timedelta(minutes=15)
    retry_errors = now - timedelta(minutes=30)
    analysis = None
    for _ in range(100):
        with transaction.atomic():
            analysis = (
                EmailAIAnalysis.objects
                .select_for_update()
                .select_related('message', 'message__mailbox')
                .filter(
                    message__category='primary',
                    message__received_at__gte=ai_analysis_cutoff(),
                )
                .filter(
                    Q(status='pending')
                    | Q(status='processing', analyzed_at__lt=stale_processing)
                    | Q(status='error', analyzed_at__lt=retry_errors)
                )
                .order_by('-message__telegram_notification_pending', '-message__received_at')
                .first()
            )
            if analysis is None:
                return JsonResponse({}, status=204)
            if not should_analyze_with_gemini(analysis.message):
                if analysis.message.telegram_notification_pending:
                    analysis.message.telegram_notification_pending = False
                    analysis.message.telegram_notification_error = ''
                    analysis.message.save(update_fields=(
                        'telegram_notification_pending',
                        'telegram_notification_error',
                    ))
                analysis.delete()
                analysis = None
                continue
            analysis.status = 'processing'
            analysis.error_message = ''
            analysis.analyzed_at = now
            analysis.save(update_fields=('status', 'error_message', 'analyzed_at'))
            break
    if analysis is None:
        return JsonResponse({}, status=204)

    message = analysis.message
    return JsonResponse({
        'job_id': analysis.pk,
        'message_id': message.pk,
        'subject': message.subject,
        'sender_email': message.sender_email,
        'received_at': message.received_at.isoformat(),
        'model': config.model_name,
        'prompt': build_analysis_prompt(message, config),
        'system_instruction': SYSTEM_INSTRUCTION,
        'response_schema': RESPONSE_SCHEMA,
        'request_delay_seconds': config.request_delay_seconds,
    })


@csrf_exempt
def submit_ai_job(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required.'}, status=405)
    if not _authorized(request):
        return JsonResponse({'error': 'Unauthorized.'}, status=401)
    try:
        payload = _json_body(request)
        job_id = int(payload.get('job_id'))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    with transaction.atomic():
        analysis = (
            EmailAIAnalysis.objects
            .select_for_update()
            .select_related('message')
            .filter(pk=job_id)
            .first()
        )
        if analysis is None:
            return JsonResponse({'error': 'Job not found.'}, status=404)
        if analysis.status == 'completed':
            return JsonResponse({'status': 'already_completed'}, status=200)
        if analysis.message.received_at < ai_analysis_cutoff():
            analysis.delete()
            return JsonResponse({'error': 'Message is older than seven days.'}, status=410)

        config = GeminiConfiguration.load()
        error = str(payload.get('error') or '').strip()
        if error:
            record_gemini_error(analysis.message, error, config=config)
            return JsonResponse({'status': 'error_recorded'})

        data = payload.get('analysis')
        if not isinstance(data, dict):
            return JsonResponse({'error': 'Structured analysis object expected.'}, status=400)
        result = apply_gemini_analysis(
            analysis.message,
            data,
            config=config,
            allow_telegram=analysis.message.telegram_notification_pending,
        )
    return JsonResponse({
        'status': 'completed',
        'important': result.is_important,
        'score': result.importance_score,
    })
