from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.conf import settings
from datetime import date, timedelta
import logging
import time

from django.db import OperationalError, close_old_connections
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.clickjacking import xframe_options_sameorigin

from .forms import CampaignForm, DirectMessageForm, MailboxForm, ManagerLoginForm, ManagerRegistrationForm, MosaicMonitorForm, ReplyForm
from .models import Campaign, EmailAIAnalysis, GeminiConfiguration, InboundAttachment, InboundMessage, Mailbox, MailboxSyncRun, ManagerProfile, MosaicMonitorConfiguration, MosaicMonitorEvent, OutgoingMessage, Region
from .mosaic import check_mosaic_calendars, send_mosaic_test_email
from .services import discover_emails, save_campaign_attachments, send_campaign, send_direct_message, send_reply, sync_mailbox
from .search import fuzzy_rank_mailboxes
from .tasks import analyze_inbound_message_task, process_mailbox_sync_task


logger = logging.getLogger(__name__)

MAIL_FOLDER_LABELS = {
    'primary': 'Основные',
    'google': 'Google',
    'spam': 'Спам',
    'sent': 'Отправленные',
}


def _mail_folder(value):
    return value if value in MAIL_FOLDER_LABELS else 'primary'


def home(request):
    if request.user.is_authenticated and hasattr(request.user, 'manager_profile'):
        return redirect('hub_dashboard')
    return redirect('login')


def mosaic_monitor_settings(request):
    config = MosaicMonitorConfiguration.load()
    form = MosaicMonitorForm(request.POST or None, instance=config)
    if request.method == 'POST' and form.is_valid():
        config = form.save()
        action = request.POST.get('action', 'save')
        if action == 'test':
            try:
                recipients = send_mosaic_test_email(config)
                messages.success(
                    request,
                    f'Тестовое письмо отправлено: {", ".join(recipients)}.',
                )
            except Exception as exc:
                messages.error(request, f'Не удалось отправить тестовое письмо: {exc}')
        elif action == 'check':
            try:
                result = check_mosaic_calendars(config)
                if result['baseline']:
                    messages.success(request, 'Первый снимок календарей сохранён. Уведомление не отправлялось.')
                elif result['changed']:
                    if result['opened']:
                        messages.success(
                            request,
                            f'Открытых дат: {len(result["opened"])}. Уведомление отправлено.',
                        )
                    else:
                        messages.success(
                            request,
                            'Календарь изменился, но новых открытых дат нет. Изменение сохранено в истории.',
                        )
                else:
                    messages.success(request, 'Проверка завершена: изменений нет.')
            except Exception as exc:
                messages.error(request, f'Проверка Mosaic Visa завершилась ошибкой: {exc}')
        else:
            messages.success(request, 'Настройки мониторинга сохранены.')
        return redirect('mosaic_monitor_settings')
    events = MosaicMonitorEvent.objects.all()[:30]
    return render(request, 'hub/mosaic_monitor.html', {
        'form': form,
        'config': config,
        'events': events,
        'password_is_set': config.password_is_set,
        'open_settings': request.method == 'POST' and not form.is_valid(),
    })


def manager_login(request):
    if request.user.is_authenticated and hasattr(request.user, 'manager_profile'):
        return redirect('hub_dashboard')
    form = ManagerLoginForm(request, request.POST or None)
    if request.method == 'POST':
        for attempt in range(3):
            try:
                if not form.is_valid():
                    break
                login(request, form.get_user())
                # Force the session write here so a transient SQLite lock can
                # be retried instead of surfacing later as an opaque HTTP 500.
                request.session.save()
                next_url = request.GET.get('next')
                if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                    return redirect(next_url)
                return redirect('hub_dashboard')
            except OperationalError:
                close_old_connections()
                if attempt == 2:
                    logger.exception('Manager login failed after SQLite retries')
                    form.add_error(None, 'База временно занята. Повторите вход через несколько секунд.')
                    return render(request, 'registration/login.html', {'form': form}, status=503)
                time.sleep(0.25 * (attempt + 1))
                form = ManagerLoginForm(request, request.POST)
    return render(request, 'registration/login.html', {'form': form})


def manager_logout(request):
    if request.method == 'POST':
        logout(request)
    return redirect('login')


def register(request):
    form = ManagerRegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('hub_dashboard')
    return render(request, 'registration/register.html', {
        'form': form,
        'available_cities': Region.objects.order_by('name').values_list('city', flat=True).distinct(),
    })


@login_required
def dashboard(request):
    mailboxes = Mailbox.objects.filter(manager=request.user, is_active=True).annotate(
        unread=Count('messages', filter=Q(messages__is_read=False, messages__category='primary'))
    )
    inbox = InboundMessage.objects.filter(mailbox__manager=request.user, category='primary')
    context = {
        'mailboxes': mailboxes[:6],
        'mailbox_count': mailboxes.count(),
        'unread_count': inbox.filter(is_read=False).count(),
        'replied_count': inbox.filter(is_replied=True).count(),
        'reply_pending_count': inbox.filter(is_replied=False).count(),
        'recent_messages': inbox.select_related('mailbox', 'mailbox__client')[:6],
    }
    return render(request, 'hub/dashboard.html', context)


@login_required
def mailbox_list(request):
    query = request.GET.get('q', '').strip()
    mailboxes = Mailbox.objects.filter(manager=request.user)
    if query:
        mailboxes = fuzzy_rank_mailboxes(mailboxes, query)
    mailboxes = mailboxes.annotate(
        unread=Count('messages', filter=Q(messages__is_read=False, messages__category='primary')),
        total=Count('messages', filter=Q(messages__category='primary')),
        google_total=Count('messages', filter=Q(messages__category='google')),
    )
    active_sync = MailboxSyncRun.objects.filter(
        manager=request.user,
        status__in=('queued', 'running'),
    ).first()
    return render(request, 'hub/mailboxes.html', {
        'mailboxes': mailboxes,
        'active_sync': active_sync,
        'query': query,
    })


@login_required
def mailbox_add(request):
    form = MailboxForm(request.POST or None, manager=request.user)
    if request.method == 'POST' and form.is_valid():
        mailbox = form.save(commit=False)
        mailbox.manager = request.user
        mailbox.save()
        try:
            imported = sync_mailbox(mailbox)
            messages.success(request, f'Ящик подключён. Загружено новых писем: {imported}.')
        except Exception as exc:
            messages.warning(request, f'Ящик сохранён, но подключение не удалось: {exc}')
        return redirect('mailbox_list')
    return render(request, 'hub/mailbox_form.html', {'form': form})


@login_required
def mailbox_edit(request, pk):
    mailbox = get_object_or_404(Mailbox, pk=pk, manager=request.user)
    form = MailboxForm(request.POST or None, instance=mailbox, manager=request.user)
    if request.method == 'POST' and form.is_valid():
        mailbox = form.save()
        if not mailbox.is_active:
            messages.success(request, 'Настройки сохранены. Отслеживание писем для ящика выключено.')
            return redirect('mailbox_list')
        try:
            imported = sync_mailbox(mailbox)
            messages.success(request, f'Настройки сохранены. Загружено новых писем: {imported}.')
        except Exception as exc:
            messages.warning(request, f'Настройки сохранены, но подключение не удалось: {exc}')
        return redirect('mailbox_list')
    return render(request, 'hub/mailbox_form.html', {'form': form, 'mailbox': mailbox})


@login_required
def mailbox_sync(request, pk):
    if request.method != 'POST':
        return redirect('mailbox_list')
    mailbox = get_object_or_404(Mailbox, pk=pk, manager=request.user)
    if not mailbox.is_active:
        messages.warning(request, 'Сначала включите отслеживание этого ящика.')
        return redirect('mailbox_list')
    try:
        imported = sync_mailbox(mailbox)
        messages.success(request, f'{mailbox.email}: загружено новых писем — {imported}.')
    except Exception as exc:
        messages.error(request, f'{mailbox.email}: ошибка IMAP — {exc}')
    return redirect('mailbox_list')


@login_required
def mailbox_toggle_tracking(request, pk):
    if request.method != 'POST':
        return redirect('mailbox_list')
    mailbox = get_object_or_404(Mailbox, pk=pk, manager=request.user)
    mailbox.is_active = not mailbox.is_active
    update_fields = ['is_active']
    if mailbox.is_active:
        mailbox.telegram_notifications_after = timezone.now()
        mailbox.sync_error = ''
        update_fields.extend(('telegram_notifications_after', 'sync_error'))
        state = 'включено'
    else:
        InboundMessage.objects.filter(mailbox=mailbox).update(
            telegram_notification_pending=False,
            telegram_notification_error='',
        )
        EmailAIAnalysis.objects.filter(
            message__mailbox=mailbox,
            status__in=('pending', 'processing', 'error'),
        ).delete()
        state = 'выключено'
    mailbox.save(update_fields=update_fields)
    messages.success(request, f'{mailbox.email}: отслеживание {state}.')
    return redirect('mailbox_list')


@login_required
def mailbox_sync_all(request):
    if request.method != 'POST':
        return redirect('mailbox_list')
    active_sync = MailboxSyncRun.objects.filter(
        manager=request.user,
        status__in=('queued', 'running'),
    ).first()
    if active_sync:
        messages.info(request, 'Синхронизация ваших ящиков уже выполняется в фоне.')
        return redirect('mailbox_list')

    sync_run = MailboxSyncRun.objects.create(manager=request.user)
    process_mailbox_sync_task(sync_run.pk)
    messages.success(request, 'Синхронизация поставлена в очередь. Ящики обновятся по одному в фоне.')
    return redirect('mailbox_list')


def _inbox_queryset(user, params):
    letters = InboundMessage.objects.filter(mailbox__manager=user).select_related(
        'mailbox', 'mailbox__client', 'ai_analysis',
    )
    folder = _mail_folder(params.get('folder', 'primary'))
    letters = letters.filter(category=folder)
    mailbox_id = params.get('mailbox')
    status = params.get('status')
    query = (params.get('q') or '').strip()
    if mailbox_id:
        letters = letters.filter(mailbox_id=mailbox_id)
    if status == 'unread':
        letters = letters.filter(is_read=False)
    elif status == 'pending':
        letters = letters.filter(is_replied=False)
    elif status == 'replied':
        letters = letters.filter(is_replied=True)
    if query:
        letters = letters.filter(Q(subject__icontains=query) | Q(sender_email__icontains=query) | Q(sender_name__icontains=query))
    return letters


@login_required
def inbox(request):
    mailbox_id = request.GET.get('mailbox')
    status = request.GET.get('status')
    query = request.GET.get('q', '').strip()
    folder = _mail_folder(request.GET.get('folder', 'primary'))
    letters = _inbox_queryset(request.user, request.GET)
    paginator = Paginator(letters, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    return render(request, 'hub/inbox.html', {
        'letters': page_obj.object_list,
        'page_obj': page_obj,
        'mailboxes': Mailbox.objects.filter(manager=request.user, is_active=True),
        'active_mailbox': mailbox_id,
        'active_status': status or '',
        'active_folder': folder,
        'folder_label': MAIL_FOLDER_LABELS[folder],
        'query': query,
        'base_query': query_params.urlencode(),
        'google_count': InboundMessage.objects.filter(mailbox__manager=request.user, category='google').count(),
        'spam_count': InboundMessage.objects.filter(mailbox__manager=request.user, category='spam').count(),
        'sent_count': InboundMessage.objects.filter(mailbox__manager=request.user, category='sent').count(),
    })


@login_required
def ai_dashboard(request):
    group = request.GET.get('group', '')
    important = request.GET.get('important', '')
    query = request.GET.get('q', '').strip()
    valid_groups = {choice for choice, _ in EmailAIAnalysis.UNIVERSITY_GROUP_CHOICES}
    analyses = (
        EmailAIAnalysis.objects
        .filter(status='completed', message__mailbox__is_active=True)
        .select_related('message', 'message__mailbox', 'message__mailbox__manager')
        .order_by('-message__received_at')
    )
    if group in valid_groups:
        analyses = analyses.filter(university_group=group)
    else:
        group = ''
    if important == '1':
        analyses = analyses.filter(is_important=True)
    if query:
        analyses = analyses.filter(
            Q(university_name__icontains=query)
            | Q(summary__icontains=query)
            | Q(action_required__icontains=query)
            | Q(message__subject__icontains=query)
            | Q(message__sender_email__icontains=query)
            | Q(message__recipient_email__icontains=query)
        )

    base = EmailAIAnalysis.objects.filter(status='completed', message__mailbox__is_active=True)
    group_counts = {
        row['university_group']: row['total']
        for row in base.values('university_group').annotate(total=Count('id'))
    }
    paginator = Paginator(analyses, 30)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    config = GeminiConfiguration.load()
    group_cards = [
        {'value': value, 'label': label, 'count': group_counts.get(value, 0)}
        for value, label in EmailAIAnalysis.UNIVERSITY_GROUP_CHOICES
    ]
    return render(request, 'hub/ai_dashboard.html', {
        'analyses': page_obj.object_list,
        'page_obj': page_obj,
        'base_query': query_params.urlencode(),
        'group_cards': group_cards,
        'active_group': group,
        'important_only': important == '1',
        'query': query,
        'config': config,
        'pending_count': EmailAIAnalysis.objects.filter(status__in=('pending', 'processing')).count(),
        'error_count': EmailAIAnalysis.objects.filter(status='error').count(),
        'important_count': base.filter(is_important=True).count(),
    })


@login_required
def queue_message_ai_analysis(request, pk):
    if request.method != 'POST':
        return redirect('ai_dashboard')
    message = get_object_or_404(InboundMessage, pk=pk, mailbox__is_active=True)
    from .ai import is_message_eligible_for_ai
    if not is_message_eligible_for_ai(message):
        messages.error(request, 'AI анализирует только письма, полученные за последние 7 дней.')
        return redirect('message_detail', pk=message.pk)
    analysis, _ = EmailAIAnalysis.objects.get_or_create(message=message)
    analysis.status = 'pending'
    analysis.error_message = ''
    analysis.save(update_fields=('status', 'error_message'))
    if not settings.AI_ANALYSIS_REMOTE_WORKER:
        analyze_inbound_message_task(message.pk, allow_telegram=False, priority=100)
    messages.success(request, 'Письмо поставлено в очередь AI-анализа.')
    target = request.POST.get('next', '')
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect('message_detail', pk=message.pk)


@login_required
def inbox_bulk(request):
    if request.method != 'POST':
        return redirect('inbox')
    action = request.POST.get('action')
    scope = request.POST.get('scope', 'selected')
    queryset = _inbox_queryset(request.user, request.POST)
    if scope != 'filtered':
        selected_ids = request.POST.getlist('selected')
        queryset = queryset.filter(pk__in=selected_ids)
    count = queryset.count()
    if action == 'mark_read':
        queryset.update(is_read=True)
        label = 'прочитано'
    elif action == 'mark_unread':
        queryset.update(is_read=False)
        label = 'отмечено непрочитанными'
    elif action == 'move_google':
        queryset.update(category='google')
        label = 'перемещено в уведомления Google'
    elif action == 'move_primary':
        queryset.update(category='primary')
        label = 'возвращено в основные'
    else:
        messages.error(request, 'Неизвестное массовое действие.')
        return redirect('inbox')
    messages.success(request, f'Писем {label}: {count}.')
    redirect_params = {key: request.POST.get(key, '') for key in ('folder', 'mailbox', 'status', 'q') if request.POST.get(key)}
    from urllib.parse import urlencode
    target = reverse('inbox')
    if redirect_params:
        target += '?' + urlencode(redirect_params)
    return redirect(target)


@login_required
def shared_mail(request):
    region_id = request.GET.get('region', '')
    profile_id = request.GET.get('profile', '')
    mailbox_id = request.GET.get('mailbox', '')
    folder = _mail_folder(request.GET.get('folder', 'primary'))
    query = request.GET.get('q', '').strip()
    period = request.GET.get('period', 'all')
    if period not in {'all', 'today', '3', '7', '30'}:
        period = 'all'
    date_value = request.GET.get('date', '').strip()
    selected_date = None
    if date_value:
        try:
            selected_date = date.fromisoformat(date_value)
        except ValueError:
            date_value = ''

    all_mailboxes = Mailbox.objects.filter(is_active=True).select_related(
        'region', 'manager', 'manager__manager_profile', 'client',
    )
    if query:
        # Search is deliberately global: managers can find an address or a
        # mailbox without first knowing its region or owning device.
        region_id = profile_id = mailbox_id = ''
        mailboxes = fuzzy_rank_mailboxes(all_mailboxes, query)
        letter_mailboxes = all_mailboxes
    else:
        mailboxes = all_mailboxes
        if region_id:
            mailboxes = mailboxes.filter(region_id=region_id)
        if profile_id:
            mailboxes = mailboxes.filter(manager__manager_profile__pk=profile_id)
        if mailbox_id:
            mailboxes = mailboxes.filter(pk=mailbox_id)
        letter_mailboxes = mailboxes

    letters = InboundMessage.objects.filter(mailbox__in=letter_mailboxes).select_related(
        'mailbox', 'mailbox__region', 'mailbox__manager__manager_profile', 'ai_analysis',
    ).prefetch_related('attachments')
    letters = letters.filter(category=folder)
    if query:
        fuzzy_mailbox_ids = list(mailboxes.values_list('pk', flat=True))
        letters = letters.filter(
            Q(subject__icontains=query)
            | Q(sender_email__icontains=query)
            | Q(sender_name__icontains=query)
            | Q(recipient_email__icontains=query)
            | Q(body_text__icontains=query)
            | Q(mailbox__email__icontains=query)
            | Q(mailbox__display_name__icontains=query)
            | Q(mailbox__owner_phone__icontains=query)
            | Q(mailbox__client__email__icontains=query)
            | Q(mailbox__client__full_name__icontains=query)
            | Q(mailbox__region__name__icontains=query)
            | Q(mailbox__region__city__icontains=query)
            | Q(mailbox__manager__manager_profile__phone__icontains=query)
            | Q(mailbox__manager__manager_profile__device__icontains=query)
            | Q(mailbox__manager__manager_profile__city__icontains=query)
            | Q(mailbox_id__in=fuzzy_mailbox_ids)
        )
    if selected_date:
        letters = letters.filter(received_at__date=selected_date)
    elif period == 'today':
        letters = letters.filter(received_at__date=timezone.localdate())
    elif period in {'3', '7', '30'}:
        first_day = timezone.localdate() - timedelta(days=int(period) - 1)
        letters = letters.filter(received_at__date__gte=first_day)

    paginator = Paginator(letters, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    query_params = request.GET.copy()
    query_params.pop('page', None)
    owners = ManagerProfile.objects.select_related('region', 'user').annotate(
        mailbox_count=Count('user__mailboxes', filter=Q(user__mailboxes__is_active=True)),
    ).filter(mailbox_count__gt=0).order_by('region__name', 'city', 'phone')
    if region_id:
        owners = owners.filter(region_id=region_id)

    return render(request, 'hub/shared_mail.html', {
        'regions': Region.objects.annotate(
            mailbox_count=Count('mailboxes', filter=Q(mailboxes__is_active=True)),
        ).filter(mailbox_count__gt=0),
        'owners': owners,
        'mailboxes': mailboxes,
        'selected_mailbox': mailboxes.first() if mailbox_id else None,
        'letters': page_obj.object_list,
        'page_obj': page_obj,
        'active_region': region_id,
        'active_profile': profile_id,
        'active_mailbox': mailbox_id,
        'active_folder': folder,
        'folder_label': MAIL_FOLDER_LABELS[folder],
        'query': query,
        'active_period': period,
        'active_date': date_value,
        'base_query': query_params.urlencode(),
        'return_to': request.get_full_path(),
    })


@login_required
def shared_mail_bulk(request):
    if request.method != 'POST':
        return redirect('shared_mail')

    action = request.POST.get('action')
    selected_ids = request.POST.getlist('selected')
    queryset = InboundMessage.objects.filter(
        pk__in=selected_ids,
        mailbox__is_active=True,
        category=_mail_folder(request.POST.get('folder', 'primary')),
    )
    count = queryset.count()
    if not selected_ids:
        messages.error(request, 'Сначала выберите хотя бы одно письмо.')
    elif action == 'mark_read':
        queryset.update(is_read=True)
        messages.success(request, f'Отмечено прочитанными: {count}.')
    elif action == 'mark_unread':
        queryset.update(is_read=False)
        messages.success(request, f'Отмечено непрочитанными: {count}.')
    else:
        messages.error(request, 'Неизвестное массовое действие.')

    from urllib.parse import urlencode
    redirect_params = {
        key: request.POST.get(key, '')
        for key in ('region', 'profile', 'mailbox', 'folder', 'q', 'period', 'date', 'page')
        if request.POST.get(key)
    }
    target = reverse('shared_mail')
    if redirect_params:
        target += '?' + urlencode(redirect_params)
    return redirect(target)


@login_required
def shared_message_mark_read(request, pk):
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)
    letter = get_object_or_404(InboundMessage, pk=pk, mailbox__is_active=True)
    if not letter.is_read:
        letter.is_read = True
        letter.save(update_fields=('is_read',))
    return JsonResponse({'ok': True})


@login_required
def shared_mailbox_sync(request, pk):
    if request.method != 'POST':
        return redirect('shared_mail')
    mailbox = get_object_or_404(Mailbox, pk=pk, is_active=True)
    try:
        imported = sync_mailbox(mailbox)
        messages.success(request, f'{mailbox.email}: загружено новых писем — {imported}.')
    except Exception as exc:
        messages.error(request, f'{mailbox.email}: ошибка IMAP — {exc}')

    from urllib.parse import urlencode
    redirect_params = {
        key: request.POST.get(key, '')
        for key in ('region', 'profile', 'mailbox', 'folder', 'q', 'period', 'date')
        if request.POST.get(key)
    }
    target = reverse('shared_mail')
    if redirect_params:
        target += '?' + urlencode(redirect_params)
    return redirect(target)


@login_required
def shared_message_detail(request, pk):
    letter = get_object_or_404(
        InboundMessage.objects.select_related(
            'mailbox', 'mailbox__client', 'mailbox__region',
            'mailbox__manager__manager_profile', 'ai_analysis',
        ).prefetch_related('attachments'),
        pk=pk,
        mailbox__is_active=True,
    )
    if not letter.is_read:
        letter.is_read = True
        letter.save(update_fields=('is_read',))
    form = ReplyForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        record = send_reply(
            manager=request.user,
            message=letter,
            body=form.cleaned_data['body'],
            attachments=form.cleaned_data['attachments'],
        )
        if record.status == 'sent':
            messages.success(request, 'Ответ отправлен от имени выбранного командного ящика.')
        else:
            messages.error(request, f'Письмо не отправлено: {record.error_message}')
        next_url = request.POST.get('next', '')
        target = reverse('shared_message_detail', args=[letter.pk])
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            from urllib.parse import urlencode
            target += '?' + urlencode({'next': next_url})
        return redirect(target)
    next_url = request.GET.get('next', '')
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        back_url = next_url
    else:
        next_url = ''
        back_url = reverse('shared_mail') + f'?profile={letter.mailbox.manager.manager_profile.pk}&mailbox={letter.mailbox_id}'
    return render(request, 'hub/message_detail.html', {
        'letter': letter,
        'form': form,
        'sent_replies': letter.replies.prefetch_related('attachments')[:5],
        'shared_mode': True,
        'back_url': back_url,
        'next_url': next_url,
    })


@login_required
def shared_compose(request):
    initial = {}
    mailbox_id = request.GET.get('mailbox')
    if mailbox_id and Mailbox.objects.filter(pk=mailbox_id, is_active=True).exists():
        initial['mailbox'] = mailbox_id
    form = DirectMessageForm(
        request.POST or None,
        request.FILES or None,
        manager=request.user,
        allow_shared=True,
        initial=initial,
    )
    if request.method == 'POST' and form.is_valid():
        record = send_direct_message(
            manager=request.user,
            mailbox=form.cleaned_data['mailbox'],
            recipient_email=form.cleaned_data['recipient_email'],
            subject=form.cleaned_data['subject'],
            body=form.cleaned_data['body'],
            attachments=form.cleaned_data['attachments'],
            allow_shared=True,
        )
        if record.status == 'sent':
            messages.success(request, f'Письмо отправлено на {record.recipient_email} от командного ящика.')
        else:
            messages.error(request, f'Письмо не отправлено: {record.error_message}')
        return redirect('shared_compose')
    history = OutgoingMessage.objects.filter(
        manager=request.user, in_reply_to__isnull=True,
    ).select_related('mailbox').prefetch_related('attachments')[:10]
    return render(request, 'hub/compose.html', {
        'form': form,
        'history': history,
        'shared_mode': True,
        'selected_mailbox': _compose_selected_mailbox(request),
    })


@login_required
def message_detail(request, pk):
    letter = get_object_or_404(
        InboundMessage.objects.select_related(
            'mailbox', 'mailbox__client', 'ai_analysis',
        ).prefetch_related('attachments'),
        pk=pk,
        mailbox__manager=request.user,
    )
    if not letter.is_read:
        letter.is_read = True
        letter.save(update_fields=('is_read',))
    form = ReplyForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        record = send_reply(
            manager=request.user,
            message=letter,
            body=form.cleaned_data['body'],
            attachments=form.cleaned_data['attachments'],
        )
        if record.status == 'sent':
            messages.success(request, 'Ответ отправлен и сохранён в истории.')
        else:
            messages.error(request, f'Письмо не отправлено: {record.error_message}')
        return redirect('message_detail', pk=letter.pk)
    return render(request, 'hub/message_detail.html', {
        'letter': letter,
        'form': form,
        'sent_replies': letter.replies.prefetch_related('attachments')[:5],
    })


@login_required
@xframe_options_sameorigin
def message_html(request, pk):
    letter = get_object_or_404(
        InboundMessage.objects.filter(Q(mailbox__manager=request.user) | Q(mailbox__is_active=True)),
        pk=pk,
    )
    response = HttpResponse(letter.body_html or '', content_type='text/html; charset=utf-8')
    response['Content-Security-Policy'] = (
        "default-src 'none'; img-src https: http: data:; "
        "style-src 'unsafe-inline' https:; font-src https: http: data:; "
        "form-action 'none'; base-uri 'none'; object-src 'none'; frame-src 'none'"
    )
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@login_required
def inbound_attachment_download(request, pk):
    attachment = get_object_or_404(
        InboundAttachment.objects.select_related('message__mailbox').filter(
            Q(message__mailbox__manager=request.user) | Q(message__mailbox__is_active=True),
        ),
        pk=pk,
    )
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=True,
        filename=attachment.original_name,
        content_type=attachment.content_type or 'application/octet-stream',
    )


@login_required
def compose(request):
    form = DirectMessageForm(
        request.POST or None,
        request.FILES or None,
        manager=request.user,
        allow_shared=True,
    )
    if request.method == 'POST' and form.is_valid():
        record = send_direct_message(
            manager=request.user,
            mailbox=form.cleaned_data['mailbox'],
            recipient_email=form.cleaned_data['recipient_email'],
            subject=form.cleaned_data['subject'],
            body=form.cleaned_data['body'],
            attachments=form.cleaned_data['attachments'],
            allow_shared=True,
        )
        if record.status == 'sent':
            messages.success(request, f'Письмо отправлено на {record.recipient_email}.')
        else:
            messages.error(request, f'Письмо не отправлено: {record.error_message}')
        return redirect('compose')
    history = OutgoingMessage.objects.filter(
        manager=request.user, in_reply_to__isnull=True,
    ).select_related('mailbox').prefetch_related('attachments')[:10]
    return render(request, 'hub/compose.html', {
        'form': form,
        'history': history,
        'shared_mode': True,
        'selected_mailbox': _compose_selected_mailbox(request),
    })


def _compose_selected_mailbox(request):
    mailbox_id = request.POST.get('mailbox') or request.GET.get('mailbox')
    if not mailbox_id:
        return None
    return Mailbox.objects.filter(pk=mailbox_id, is_active=True).select_related(
        'region', 'manager__manager_profile',
    ).first()


@login_required
def mailbox_search_api(request):
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})
    queryset = Mailbox.objects.filter(is_active=True)
    mailboxes = fuzzy_rank_mailboxes(queryset, query, limit=15)
    results = []
    for mailbox in mailboxes:
        profile = getattr(mailbox.manager, 'manager_profile', None)
        results.append({
            'id': mailbox.pk,
            'email': mailbox.email,
            'name': mailbox.display_name or mailbox.email,
            'owner': mailbox.owner_phone or getattr(profile, 'phone', ''),
            'region': str(mailbox.region or ''),
        })
    return JsonResponse({'results': results})


@login_required
def campaign_center(request):
    query = request.GET.get('q', '').strip()
    recipients = discover_emails(query)
    form = CampaignForm(request.POST or None, request.FILES or None, recipient_choices=recipients)
    if request.method == 'POST' and form.is_valid():
        campaign = form.save(commit=False)
        campaign.manager = request.user
        campaign.recipients = sorted(set(form.cleaned_data['recipients']))
        campaign.save()
        save_campaign_attachments(campaign, form.cleaned_data['attachments'])
        send_campaign(campaign)
        if campaign.failed_count:
            messages.warning(request, f'Отправлено: {campaign.sent_count}. Ошибок: {campaign.failed_count}.')
        else:
            messages.success(request, f'Рассылка отправлена: {campaign.sent_count} получателей.')
        return redirect('campaign_center')
    return render(request, 'hub/campaign.html', {
        'form': form,
        'recipients': recipients,
        'query': query,
        'campaigns': Campaign.objects.filter(manager=request.user)[:8],
    })
