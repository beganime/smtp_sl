from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from unittest.mock import Mock, patch
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime
import json
import tempfile

from .forms import MailboxForm, ReplyForm
from .mail_content import html_to_text
from .management.commands.import_medics_mailboxes import parse_medics_tsv
from .management.commands.import_sanly_mailboxes import parse_mailbox_rows, parse_sanly_input
from .ai import analyze_inbound_message, extract_literal_links, should_analyze_with_gemini
from .models import Campaign, CampaignAttachment, Client, EmailAIAnalysis, GeminiConfiguration, InboundAttachment, InboundMessage, Mailbox, MailboxSyncRun, ManagerProfile, MosaicMonitorConfiguration, MosaicMonitorEvent, OutgoingAttachment, OutgoingMessage, Region
from .mosaic import check_mosaic_calendars, parse_calendar_html
from .services import _imap_folder_specs, classify_inbound_message, discover_emails, sync_mailbox
from .tasks import process_mailbox_sync_task
from .telegram import (
    format_inbound_notification, is_university_message,
    notify_inbound_message, retry_pending_telegram_notifications,
    telegram_university_topic,
)
from .telegram_bot import handle_telegram_bot_text
from .telegram_bot_ui import (
    BUTTON_ADD,
    BUTTON_SITE,
    TelegramBotUI,
    create_mailbox_from_bot,
)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class HubTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='+99361111111', password='Strong-pass-44')
        ManagerProfile.objects.create(user=self.user, phone=self.user.username, device='Office Mac')
        self.region = Region.objects.get(name='Лебап')
        self.client_record = Client.objects.create(email='student@example.com', full_name='Анна Клиент')
        self.mailbox = Mailbox.objects.create(
            manager=self.user,
            client=self.client_record,
            region=self.region,
            owner_phone=self.user.username,
            email='device-box@yandex.ru',
            display_name='Анна · МГУ',
            provider='yandex',
        )
        self.letter = InboundMessage.objects.create(
            mailbox=self.mailbox,
            external_uid='uid-1',
            sender_name='Приёмная комиссия',
            sender_email='admission@university.example',
            recipient_email=self.mailbox.email,
            subject='Документы получены',
            body_text='Заявление принято в обработку.',
            received_at=timezone.now(),
        )

    def test_login_requires_matching_device(self):
        bad = self.client.post(reverse('login'), {'phone': self.user.username, 'device': 'Other laptop', 'password': 'Strong-pass-44'})
        self.assertContains(bad, 'устройство не привязано', status_code=200)
        good = self.client.post(reverse('login'), {'phone': self.user.username, 'device': 'office mac', 'password': 'Strong-pass-44'})
        self.assertRedirects(good, reverse('hub_dashboard'))

    def test_login_accepts_named_account(self):
        named = get_user_model().objects.create_user(username='Sanly.tm', password='Sanly.tm')
        ManagerProfile.objects.create(user=named, phone='Sanly.tm', device='Sanly.tm')
        response = self.client.post(reverse('login'), {
            'phone': 'Sanly.tm',
            'device': 'Sanly.tm',
            'password': 'Sanly.tm',
        })
        self.assertRedirects(response, reverse('hub_dashboard'))

    @patch('mailer.views.process_mailbox_sync_task')
    def test_sync_all_is_queued_in_background(self, enqueue_mock):
        self.client.force_login(self.user)
        response = self.client.post(reverse('mailbox_sync_all'))
        self.assertRedirects(response, reverse('mailbox_list'))
        sync_run = MailboxSyncRun.objects.get(manager=self.user)
        enqueue_mock.assert_called_once_with(sync_run.pk)

    def test_mailbox_tracking_can_be_disabled_and_enabled(self):
        self.letter.telegram_notification_pending = True
        self.letter.save(update_fields=('telegram_notification_pending',))
        EmailAIAnalysis.objects.create(message=self.letter)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('mailbox_toggle_tracking', args=[self.mailbox.pk])
        )

        self.assertRedirects(response, reverse('mailbox_list'))
        self.mailbox.refresh_from_db()
        self.letter.refresh_from_db()
        self.assertFalse(self.mailbox.is_active)
        self.assertFalse(self.letter.telegram_notification_pending)
        self.assertFalse(EmailAIAnalysis.objects.filter(message=self.letter).exists())

        self.client.post(reverse('mailbox_toggle_tracking', args=[self.mailbox.pk]))
        self.mailbox.refresh_from_db()
        self.assertTrue(self.mailbox.is_active)

    def test_mailbox_list_searches_by_email_and_name(self):
        self.client.force_login(self.user)
        by_email = self.client.get(reverse('mailbox_list'), {'q': 'device-box'})
        self.assertContains(by_email, self.mailbox.email)
        missing = self.client.get(reverse('mailbox_list'), {'q': 'не-существует'})
        self.assertNotContains(missing, self.mailbox.email)
        self.assertContains(missing, 'ящики не найдены')

    def test_mailbox_search_tolerates_typo_and_api_stays_empty_without_query(self):
        self.client.force_login(self.user)
        typo = self.client.get(reverse('mailbox_list'), {'q': 'devcie-box'})
        self.assertContains(typo, self.mailbox.email)

        empty_api = self.client.get(reverse('mailbox_search_api'))
        self.assertEqual(empty_api.json(), {'results': []})
        api = self.client.get(reverse('mailbox_search_api'), {'q': 'devcie-box'})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json()['results'][0]['email'], self.mailbox.email)

    @patch('mailer.tasks.sync_mailbox')
    def test_background_sync_processes_mailboxes_sequentially(self, sync_mock):
        sync_mock.return_value = 2
        second = Mailbox.objects.create(
            manager=self.user,
            email='second-sync@example.com',
            password_encrypted='configured',
        )
        self.mailbox.password_encrypted = 'configured'
        self.mailbox.save(update_fields=('password_encrypted',))
        sync_run = MailboxSyncRun.objects.create(manager=self.user)

        process_mailbox_sync_task.now(sync_run.pk, delay=0, retries=0)

        sync_run.refresh_from_db()
        self.assertEqual(sync_run.status, 'completed')
        self.assertEqual(sync_run.processed, 2)
        self.assertEqual(sync_run.imported, 4)
        self.assertEqual(
            [call.args[0].pk for call in sync_mock.call_args_list],
            [self.mailbox.pk, second.pk],
        )

    def test_sanly_parser_does_not_consume_next_name_after_malformed_row(self):
        rows = list(parse_mailbox_rows(
            'Гурбан Ягмыров broken@example.com '
            'Ташполат Мырадов m.tashpolat@sanly.tm Tashpolat_0710'
        ))
        self.assertEqual(rows, [('Ташполат Мырадов', 'm.tashpolat@sanly.tm', 'Tashpolat_0710')])

    def test_sanly_parser_accepts_csv_and_email_as_password(self):
        rows = list(parse_sanly_input(
            'Почта (@sanly.tm),Пароль\n'
            'NEW@sanly.tm,Secret_0710\n'
            'same@sanly.tm,same@sanly.tm\n'
        ))
        self.assertEqual(rows, [
            ('', 'new@sanly.tm', 'Secret_0710'),
            ('', 'same@sanly.tm', 'same@sanly.tm'),
        ])

    def test_medics_parser_extracts_multiple_addresses_and_ignores_notes(self):
        rows = parse_medics_tsv(
            'ФИО\tПочта\tКод\n'
            'Чары Оразов\tchary@sanly.tm/chary@gmail.com\tChary_0710\n'
            'Севинч\tsevinch@sanly.tm/свои госуслуги\tSevinc_0710\n'
        )
        self.assertEqual(rows, [
            ('Чары Оразов', 'chary@sanly.tm', 'Chary_0710'),
            ('Чары Оразов', 'chary@gmail.com', 'Chary_0710'),
            ('Севинч', 'sevinch@sanly.tm', 'Sevinc_0710'),
        ])

    def test_home_opens_manager_login_instead_of_admin(self):
        self.assertRedirects(self.client.get('/'), reverse('login'))
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/'), reverse('hub_dashboard'))

    def test_manager_only_sees_own_messages(self):
        other = get_user_model().objects.create_user(username='+99362222222', password='pass')
        other_box = Mailbox.objects.create(manager=other, email='other@example.com')
        other_letter = InboundMessage.objects.create(
            mailbox=other_box, external_uid='uid-other', sender_email='sender@example.com',
            recipient_email='other@example.com', received_at=timezone.now(),
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('message_detail', args=[other_letter.pk])).status_code, 404)
        response = self.client.get(reverse('inbox'))
        self.assertContains(response, self.letter.subject)
        self.assertNotContains(response, 'sender@example.com')

    def test_google_notifications_are_hidden_from_primary_inbox(self):
        google = InboundMessage.objects.create(
            mailbox=self.mailbox, external_uid='google-1', sender_name='Google',
            sender_email='no-reply@accounts.google.com', recipient_email=self.mailbox.email,
            subject='Security alert', received_at=timezone.now(), category='google',
        )
        self.client.force_login(self.user)
        primary = self.client.get(reverse('inbox'))
        self.assertNotContains(primary, google.subject)
        google_folder = self.client.get(reverse('inbox'), {'folder': 'google'})
        self.assertContains(google_folder, google.subject)
        self.assertEqual(classify_inbound_message('drive-shares-dm-noreply@google.com'), 'google')
        self.assertEqual(classify_inbound_message('admission@gmail.com'), 'primary')

    def test_bulk_actions_are_scoped_to_manager(self):
        second = InboundMessage.objects.create(
            mailbox=self.mailbox, external_uid='uid-2', sender_email='uni2@example.com',
            recipient_email=self.mailbox.email, received_at=timezone.now(),
        )
        other = get_user_model().objects.create_user(username='+99363333333', password='pass')
        other_box = Mailbox.objects.create(manager=other, email='other-bulk@example.com')
        foreign = InboundMessage.objects.create(
            mailbox=other_box, external_uid='foreign', sender_email='foreign@example.com',
            recipient_email=other_box.email, received_at=timezone.now(),
        )
        self.client.force_login(self.user)
        response = self.client.post(reverse('inbox_bulk'), {
            'folder': 'primary', 'action': 'mark_read', 'scope': 'selected',
            'selected': [self.letter.pk, second.pk, foreign.pk],
        })
        self.assertEqual(response.status_code, 302)
        self.letter.refresh_from_db(); second.refresh_from_db(); foreign.refresh_from_db()
        self.assertTrue(self.letter.is_read)
        self.assertTrue(second.is_read)
        self.assertFalse(foreign.is_read)

    def test_inbox_paginates_and_mark_all_uses_filtered_scope(self):
        InboundMessage.objects.bulk_create([
            InboundMessage(
                mailbox=self.mailbox, external_uid=f'page-{number}', sender_email=f'uni{number}@example.com',
                recipient_email=self.mailbox.email, received_at=timezone.now(),
            ) for number in range(30)
        ])
        self.client.force_login(self.user)
        page = self.client.get(reverse('inbox'))
        self.assertEqual(len(page.context['letters']), 25)
        self.assertEqual(page.context['page_obj'].paginator.num_pages, 2)
        self.client.post(reverse('inbox_bulk'), {
            'folder': 'primary', 'action': 'mark_read', 'scope': 'filtered',
        })
        self.assertFalse(InboundMessage.objects.filter(mailbox=self.mailbox, category='primary', is_read=False).exists())

    def test_opening_and_replying_to_message(self):
        self.client.force_login(self.user)
        self.client.get(reverse('message_detail', args=[self.letter.pk]))
        self.letter.refresh_from_db()
        self.assertTrue(self.letter.is_read)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            document = SimpleUploadedFile('application.pdf', b'%PDF-1.4 test', content_type='application/pdf')
            response = self.client.post(reverse('message_detail', args=[self.letter.pk]), {
                'body': 'Спасибо, получили.', 'attachments': [document],
            })
            self.assertRedirects(response, reverse('message_detail', args=[self.letter.pk]))
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].attachments[0].filename, 'application.pdf')
            self.assertEqual(OutgoingMessage.objects.get().status, 'sent')
            self.assertEqual(OutgoingAttachment.objects.get().original_name, 'application.pdf')

    def test_direct_message_can_be_sent_to_any_address_with_attachment(self):
        self.client.force_login(self.user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            document = SimpleUploadedFile('offer.pdf', b'%PDF direct', content_type='application/pdf')
            response = self.client.post(reverse('compose'), {
                'mailbox': self.mailbox.pk,
                'recipient_email': 'rector@example.edu',
                'subject': 'Direct admission question',
                'body': 'Hello from SMTP_SL',
                'attachments': [document],
            })
            self.assertRedirects(response, reverse('compose'))
            record = OutgoingMessage.objects.get(in_reply_to__isnull=True)
            self.assertEqual((record.status, record.recipient_email), ('sent', 'rector@example.edu'))
            self.assertEqual(mail.outbox[0].attachments[0].filename, 'offer.pdf')

    def test_direct_message_accepts_files_selected_in_separate_batches(self):
        self.client.force_login(self.user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            first = SimpleUploadedFile('first.pdf', b'first', content_type='application/pdf')
            second = SimpleUploadedFile('second.jpg', b'second', content_type='image/jpeg')
            response = self.client.post(reverse('compose'), {
                'mailbox': self.mailbox.pk,
                'recipient_email': 'admission@example.edu',
                'subject': 'Two files',
                'body': 'Documents attached',
                'attachments': [first, second],
            })
            self.assertRedirects(response, reverse('compose'))
            record = OutgoingMessage.objects.get(in_reply_to__isnull=True)
            self.assertEqual(record.attachments.count(), 2)
            self.assertEqual(len(mail.outbox[0].attachments), 2)

    def test_compose_picker_does_not_render_every_mailbox(self):
        other = get_user_model().objects.create_user(username='+99365000001', password='Other-pass-1')
        ManagerProfile.objects.create(user=other, phone=other.username, device='Other phone')
        other_box = Mailbox.objects.create(
            manager=other,
            email='remote-choice@example.com',
            display_name='Удалённый ящик',
        )
        self.client.force_login(self.user)
        page = self.client.get(reverse('compose'))
        self.assertContains(page, 'mailbox-picker-search')
        self.assertNotContains(page, other_box.email)
        result = self.client.get(reverse('mailbox_search_api'), {'q': 'remote choise'})
        self.assertContains(result, other_box.email)

    def test_team_mail_allows_reading_and_replying_without_mailbox_management(self):
        other = get_user_model().objects.create_user(username='+99365555555', password='Other-pass-55')
        other_profile = ManagerProfile.objects.create(
            user=other, phone=other.username, device='Regional phone', city='Туркменабад', region=self.region,
        )
        other_box = Mailbox.objects.create(
            manager=other, region=self.region, owner_phone=other.username,
            email='regional-box@example.com', display_name='Региональный ящик',
        )
        other_letter = InboundMessage.objects.create(
            mailbox=other_box, external_uid='team-letter', sender_email='team-university@example.edu',
            recipient_email=other_box.email, subject='Ответ для другого телефона',
            body_text='Team message', received_at=timezone.now(),
        )
        self.client.force_login(self.user)
        page = self.client.get(reverse('shared_mail'), {'profile': other_profile.pk})
        self.assertContains(page, other_profile.phone)
        self.assertContains(page, other_box.email)
        self.assertContains(page, other_letter.subject)
        self.assertNotContains(page, 'Добавить ящик')
        self.assertEqual(self.client.get(reverse('message_detail', args=[other_letter.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('shared_message_detail', args=[other_letter.pk])).status_code, 200)
        response = self.client.post(reverse('shared_message_detail', args=[other_letter.pk]), {'body': 'Командный ответ'})
        self.assertRedirects(response, reverse('shared_message_detail', args=[other_letter.pk]))
        record = OutgoingMessage.objects.get(in_reply_to=other_letter)
        self.assertEqual((record.manager, record.mailbox, record.status), (self.user, other_box, 'sent'))

    def test_team_compose_can_send_from_another_active_mailbox(self):
        other = get_user_model().objects.create_user(username='+99365666666', password='Other-pass-66')
        ManagerProfile.objects.create(
            user=other, phone=other.username, device='Other device', city='Мары',
            region=Region.objects.get(name='Мары'),
        )
        other_box = Mailbox.objects.create(
            manager=other, region=Region.objects.get(name='Мары'), owner_phone=other.username,
            email='mary-team@example.com', display_name='Мары · команда',
        )
        self.client.force_login(self.user)
        response = self.client.post(reverse('shared_compose'), {
            'mailbox': other_box.pk,
            'recipient_email': 'admission@team-university.example',
            'subject': 'Письмо от другого региона',
            'body': 'Здравствуйте',
        })
        self.assertRedirects(response, reverse('shared_compose'))
        record = OutgoingMessage.objects.get(in_reply_to__isnull=True)
        self.assertEqual((record.manager, record.mailbox, record.status), (self.user, other_box, 'sent'))

    @patch('mailer.views.sync_mailbox')
    def test_team_mail_can_sync_another_active_mailbox(self, sync_mock):
        other = get_user_model().objects.create_user(username='+99365777777', password='Other-pass-77')
        profile = ManagerProfile.objects.create(
            user=other, phone=other.username, device='Shared device', region=self.region,
        )
        other_box = Mailbox.objects.create(
            manager=other, region=self.region, owner_phone=other.username,
            email='shared-sync@example.com', display_name='Shared sync box',
        )
        sync_mock.return_value = 3
        self.client.force_login(self.user)

        page = self.client.get(reverse('shared_mail'), {'profile': profile.pk, 'mailbox': other_box.pk})
        self.assertContains(page, 'Обновить почту')
        response = self.client.post(reverse('shared_mailbox_sync', args=[other_box.pk]), {
            'profile': profile.pk, 'mailbox': other_box.pk, 'folder': 'primary',
        })

        self.assertRedirects(
            response,
            reverse('shared_mail') + f'?profile={profile.pk}&mailbox={other_box.pk}&folder=primary',
        )
        sync_mock.assert_called_once_with(other_box)

    def test_team_search_is_global_across_messages_and_mailboxes(self):
        other = get_user_model().objects.create_user(username='+99365888888', password='Other-pass-88')
        profile = ManagerProfile.objects.create(
            user=other, phone=other.username, device='Searchable regional phone', region=self.region,
        )
        other_box = Mailbox.objects.create(
            manager=other, region=self.region, owner_phone=other.username,
            email='global-search@example.com', display_name='Global searchable box',
        )
        other_letter = InboundMessage.objects.create(
            mailbox=other_box, external_uid='global-search-letter',
            sender_email='admissions@global-university.example', recipient_email=other_box.email,
            subject='Unique scholarship response', body_text='Scholarship search body',
            received_at=timezone.now(),
        )
        self.client.force_login(self.user)

        # Even stale filters for the current user's phone must not limit a global search.
        response = self.client.get(reverse('shared_mail'), {
            'profile': self.user.manager_profile.pk,
            'q': 'global-university',
        })
        self.assertContains(response, other_letter.subject)
        self.assertEqual(response.context['active_profile'], '')

        mailbox_response = self.client.get(reverse('shared_mail'), {'q': 'Searchable regional phone'})
        self.assertContains(mailbox_response, other_box.email)
        self.assertContains(mailbox_response, profile.device)

    def test_team_mail_can_filter_by_recent_period_and_exact_date(self):
        old_letter = InboundMessage.objects.create(
            mailbox=self.mailbox,
            external_uid='old-team-letter',
            sender_email='archive@university.example',
            recipient_email=self.mailbox.email,
            subject='Старое письмо команды',
            body_text='Архивное письмо',
            received_at=timezone.now() - timedelta(days=10),
        )
        self.client.force_login(self.user)

        recent = self.client.get(reverse('shared_mail'), {'period': '3'})
        self.assertContains(recent, self.letter.subject)
        self.assertNotContains(recent, old_letter.subject)

        old_date = timezone.localtime(old_letter.received_at).date().isoformat()
        exact = self.client.get(reverse('shared_mail'), {'date': old_date})
        self.assertContains(exact, old_letter.subject)
        self.assertNotContains(exact, self.letter.subject)

    def test_team_mail_bulk_marks_selected_letters_read(self):
        second_letter = InboundMessage.objects.create(
            mailbox=self.mailbox,
            external_uid='second-team-letter',
            sender_email='second@university.example',
            recipient_email=self.mailbox.email,
            subject='Второе письмо команды',
            body_text='Ещё одно письмо',
            received_at=timezone.now(),
        )
        self.client.force_login(self.user)

        response = self.client.post(reverse('shared_mail_bulk'), {
            'selected': [self.letter.pk, second_letter.pk],
            'action': 'mark_read',
            'folder': 'primary',
            'period': '7',
            'page': '1',
        })

        self.assertRedirects(
            response,
            reverse('shared_mail') + '?folder=primary&period=7&page=1',
        )
        self.letter.refresh_from_db()
        second_letter.refresh_from_db()
        self.assertTrue(self.letter.is_read)
        self.assertTrue(second_letter.is_read)

    def test_team_message_expands_and_marks_itself_read(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse('shared_mail'), {
            'mailbox': self.mailbox.pk,
            'period': '7',
        })
        self.assertContains(page, 'team-letter-details')
        self.assertContains(page, 'team-letter-preview')
        self.assertContains(page, 'Копировать текст')
        self.assertContains(page, f'team-letter-text-{self.letter.pk}')
        self.assertContains(page, 'Ответить')
        self.assertContains(page, 'next=')

        response = self.client.post(reverse('shared_message_mark_read', args=[self.letter.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {'ok': True})
        self.letter.refresh_from_db()
        self.assertTrue(self.letter.is_read)

    def test_team_message_detail_keeps_filtered_return_url(self):
        self.client.force_login(self.user)
        return_to = reverse('shared_mail') + (
            f'?mailbox={self.mailbox.pk}&folder=primary&period=7&page=2'
        )
        response = self.client.get(
            reverse('shared_message_detail', args=[self.letter.pk]),
            {'next': return_to},
        )
        self.assertEqual(response.context['back_url'], return_to)
        self.assertEqual(response.context['next_url'], return_to)
        self.assertContains(response, 'name="next"')
        self.assertContains(response, 'Копировать текст письма')
        self.assertContains(response, 'id="message-copy-source"')

    def test_html_message_is_rendered_in_sandboxed_frame(self):
        self.client.force_login(self.user)
        self.letter.body_html = '<h1>Admission</h1><script>alert(1)</script>'
        self.letter.save(update_fields=('body_html',))
        detail = self.client.get(reverse('message_detail', args=[self.letter.pk]))
        self.assertContains(detail, 'sandbox')
        self.assertContains(detail, reverse('message_html', args=[self.letter.pk]))
        html = self.client.get(reverse('message_html', args=[self.letter.pk]))
        self.assertContains(html, '<h1>Admission</h1>', html=True)
        self.assertIn("default-src 'none'", html.headers['Content-Security-Policy'])
        self.assertEqual(html.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(html.headers['X-Frame-Options'], 'SAMEORIGIN')

    def test_email_discovery_marks_device_mailboxes(self):
        result = {item['email']: item for item in discover_emails()}
        self.assertIn('student@example.com', result)
        self.assertFalse(result['student@example.com']['device'])
        self.assertTrue(result['device-box@yandex.ru']['device'])

    def test_campaign_sends_only_selected_valid_discovered_addresses(self):
        self.client.force_login(self.user)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            photo = SimpleUploadedFile('photo.jpg', b'fake-jpeg', content_type='image/jpeg')
            response = self.client.post(reverse('campaign_center'), {
                'subject': 'Важная информация',
                'body': 'Текст сообщения',
                'recipients': ['student@example.com', 'device-box@yandex.ru'],
                'attachments': [photo],
            })
            self.assertRedirects(response, reverse('campaign_center'))
            campaign = Campaign.objects.get()
            self.assertEqual(campaign.sent_count, 2)
            self.assertEqual(len(mail.outbox), 2)
            self.assertTrue(all(message.attachments[0].filename == 'photo.jpg' for message in mail.outbox))
            self.assertEqual(CampaignAttachment.objects.get().original_name, 'photo.jpg')

    def test_attachment_validation_blocks_executables(self):
        executable = SimpleUploadedFile('danger.exe', b'MZ', content_type='application/octet-stream')
        form = ReplyForm(data={'body': 'Ответ'}, files={'attachments': executable})
        self.assertFalse(form.is_valid())
        self.assertIn('не поддерживается', str(form.errors['attachments']))

    def test_attachment_validation_allows_30_mb_archive(self):
        archive = SimpleUploadedFile('documents.zip', b'zip', content_type='application/zip')
        archive.size = 30 * 1024 * 1024
        form = ReplyForm(data={'body': 'Архив документов'}, files={'attachments': archive})
        self.assertTrue(form.is_valid(), form.errors)

    def test_attachment_validation_blocks_files_over_50_mb(self):
        archive = SimpleUploadedFile('documents.zip', b'zip', content_type='application/zip')
        archive.size = 51 * 1024 * 1024
        form = ReplyForm(data={'body': 'Архив документов'}, files={'attachments': archive})
        self.assertFalse(form.is_valid())
        self.assertIn('больше 50 МБ', str(form.errors['attachments']))

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='',
        TELEGRAM_CHAT_IDS='8245070720,6560350312',
        TELEGRAM_SITE_URL='https://tmmail.ru',
    )
    @patch('mailer.telegram.requests.post')
    def test_telegram_notification_contains_letter_details(self, post):
        post.return_value.status_code = 200
        post.return_value.text = '{"ok": true}'
        self.letter.telegram_notification_pending = True
        self.letter.body_text = 'Текст нового письма'
        self.letter.save(update_fields=('telegram_notification_pending', 'body_text'))

        self.assertTrue(notify_inbound_message(self.letter))

        self.assertEqual(post.call_count, 2)
        payload = post.call_args_list[0].kwargs['data']
        self.assertEqual(
            [call.kwargs['data']['chat_id'] for call in post.call_args_list],
            ['8245070720', '6560350312'],
        )
        self.assertIn('admission@university.example', payload['text'])
        self.assertIn(self.letter.recipient_email, payload['text'])
        self.assertIn('Текст нового письма', payload['text'])
        self.letter.refresh_from_db()
        self.assertFalse(self.letter.telegram_notification_pending)
        self.assertIsNotNone(self.letter.telegram_notified_at)

    def test_html_letter_is_converted_to_readable_telegram_text(self):
        self.letter.body_text = ''
        self.letter.body_html = (
            '<html><head><style>.hidden{display:none}</style></head>'
            '<body><h1>Заголовок</h1><p>Первая строка<br>Вторая строка</p>'
            '<script>alert("spam")</script></body></html>'
        )
        text = format_inbound_notification(self.letter)
        self.assertTrue(text.startswith('📬 Новое письмо'))
        self.assertIn('Заголовок\nПервая строка\nВторая строка', text)
        self.assertNotIn('display:none', text)
        self.assertNotIn('alert(', text)
        self.assertEqual(
            html_to_text('<p>Один</p><p>Два&nbsp;слова</p>'),
            'Один\nДва слова',
        )

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='8245070720',
        TELEGRAM_CHAT_IDS='',
    )
    @patch('mailer.telegram.requests.post')
    def test_spam_is_never_sent_to_telegram(self, post):
        self.letter.category = 'spam'
        self.letter.telegram_notification_pending = True
        self.letter.save(update_fields=('category', 'telegram_notification_pending'))

        self.assertEqual(retry_pending_telegram_notifications(), 0)

        self.letter.refresh_from_db()
        self.assertFalse(self.letter.telegram_notification_pending)
        post.assert_not_called()

    def test_only_university_messages_match_telegram_filter(self):
        self.assertTrue(is_university_message(self.letter))

        self.letter.sender_name = 'Google'
        self.letter.sender_email = 'no-reply@accounts.google.com'
        self.letter.subject = 'Оповещение системы безопасности'
        self.letter.body_text = 'Новый вход в аккаунт.'
        self.assertFalse(is_university_message(self.letter))

        self.letter.sender_name = 'Интернет-магазин'
        self.letter.sender_email = 'news@shop.example'
        self.letter.subject = 'Скидки этой недели'
        self.letter.body_text = 'Товары и рекламная рассылка.'
        self.assertFalse(is_university_message(self.letter))

        self.letter.sender_name = 'Приёмная комиссия БФУ'
        self.letter.sender_email = 'commission@yandex.ru'
        self.letter.subject = 'Вступительные испытания'
        self.assertTrue(is_university_message(self.letter))

        self.letter.sender_name = 'Абитуриент'
        self.letter.sender_email = 'student@gmail.com'
        self.letter.subject = 'Re: Инструкция по вступительным экзаменам БГМУ'
        self.letter.body_text = (
            'Спасибо. Ниже процитировано письмо приёмной комиссии университета '
            'о поступлении абитуриента.'
        )
        self.assertFalse(is_university_message(self.letter))

    def test_university_messages_are_classified_for_forum_topics(self):
        self.letter.sender_email = 'center@int.unn.ru'
        self.letter.sender_name = 'Lobachevsky University'
        self.assertEqual(telegram_university_topic(self.letter), 'lobachevsky')

        self.letter.sender_email = 'admission@rudn.ru'
        self.letter.sender_name = 'Приёмная комиссия'
        self.assertEqual(telegram_university_topic(self.letter), 'rudn')

        self.letter.sender_email = 'commission@unknown-university.example'
        self.letter.sender_name = 'Другой университет'
        self.assertEqual(telegram_university_topic(self.letter), 'unsorted')

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='',
        TELEGRAM_CHAT_IDS='8245070720,-1001234567890',
        TELEGRAM_FORUM_CHAT_ID='-1001234567890',
        TELEGRAM_FORUM_TOPICS='unsorted:10,lobachevsky:17,rudn:18',
    )
    @patch('mailer.telegram.requests.post')
    def test_forum_recipient_gets_university_topic_thread(self, post):
        post.return_value.status_code = 200
        post.return_value.text = '{"ok": true}'
        self.letter.sender_email = 'center@int.unn.ru'
        self.letter.telegram_notification_pending = True
        self.letter.save(update_fields=('sender_email', 'telegram_notification_pending'))

        self.assertTrue(notify_inbound_message(self.letter))

        private_payload = post.call_args_list[0].kwargs['data']
        forum_payload = post.call_args_list[1].kwargs['data']
        self.assertNotIn('message_thread_id', private_payload)
        self.assertEqual(forum_payload['message_thread_id'], '17')

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='8245070720',
        TELEGRAM_CHAT_IDS='',
    )
    @patch('mailer.telegram.requests.post')
    def test_google_category_is_never_sent_to_telegram(self, post):
        self.letter.category = 'google'
        self.letter.telegram_notification_pending = True
        self.letter.save(update_fields=('category', 'telegram_notification_pending'))

        self.assertEqual(retry_pending_telegram_notifications(), 0)

        self.letter.refresh_from_db()
        self.assertFalse(self.letter.telegram_notification_pending)
        post.assert_not_called()

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='',
        TELEGRAM_CHAT_IDS='8245070720,6560350312',
    )
    @patch('mailer.telegram.requests.post')
    def test_telegram_retry_does_not_duplicate_successful_recipient(self, post):
        post.side_effect = [
            Mock(status_code=200, text='{"ok": true}'),
            Mock(status_code=403, text='{"ok": false, "description": "bot was blocked"}'),
        ]
        self.letter.telegram_notification_pending = True
        self.letter.save(update_fields=('telegram_notification_pending',))

        self.assertFalse(notify_inbound_message(self.letter))
        self.letter.refresh_from_db()
        self.assertTrue(self.letter.telegram_notification_pending)
        self.assertIn('8245070720', self.letter.telegram_notification_error)

        post.reset_mock()
        post.side_effect = None
        post.return_value.status_code = 200
        post.return_value.text = '{"ok": true}'
        self.assertTrue(notify_inbound_message(self.letter))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['data']['chat_id'], '6560350312')

    def test_telegram_bot_searches_mailbox_status_and_letters_with_typos(self):
        mailbox_answer = handle_telegram_bot_text(
            'есть ли почта devcie-box@yandex.ru в базе'
        )
        self.assertIn(self.mailbox.email, mailbox_answer)
        self.assertIn('активен', mailbox_answer)
        self.assertIn('Писем: 1', mailbox_answer)

        letter_answer = handle_telegram_bot_text('/letters Докумнты получены')
        self.assertIn(self.letter.subject, letter_answer)
        self.assertIn(self.letter.sender_email, letter_answer)

    @patch('mailer.telegram_bot_ui.telegram_api_request')
    def test_public_telegram_bot_shows_menu_and_site_to_any_chat(self, api_request):
        api_request.return_value = {'ok': True, 'result': {}}
        bot = TelegramBotUI()

        bot.handle_update({
            'message': {
                'message_id': 10,
                'chat': {'id': 999999999},
                'text': '/start',
            },
        })
        start_payload = api_request.call_args.args[1]
        self.assertEqual(api_request.call_args.args[0], 'sendMessage')
        self.assertIn('keyboard', start_payload['reply_markup'])
        self.assertIn(BUTTON_ADD, start_payload['reply_markup'])

        bot.handle_update({
            'message': {
                'message_id': 11,
                'chat': {'id': 999999999},
                'text': BUTTON_SITE,
            },
        })
        site_payload = api_request.call_args.args[1]
        self.assertIn('https://tmmail.ru/', site_payload['reply_markup'])

    @patch('mailer.telegram_bot_ui.telegram_api_request')
    def test_telegram_bot_silently_ignores_groups(self, api_request):
        bot = TelegramBotUI()

        result = bot.handle_update({
            'message': {
                'message_id': 12,
                'chat': {'id': -1001234567890, 'type': 'supergroup'},
                'text': '/start',
            },
        })

        self.assertIsNone(result)
        api_request.assert_not_called()

    @patch('mailer.telegram_bot_ui.telegram_api_request')
    def test_mailbox_search_has_button_for_last_five_letters(self, api_request):
        api_request.return_value = {'ok': True, 'result': {}}
        bot = TelegramBotUI()

        bot.handle_update({
            'message': {
                'message_id': 20,
                'chat': {'id': 999999999, 'type': 'private'},
                'text': f'/mail {self.mailbox.email}',
            },
        })
        search_payload = api_request.call_args.args[1]
        self.assertIn(f'recentmb:{self.mailbox.pk}', search_payload['reply_markup'])

        bot.handle_update({
            'callback_query': {
                'id': 'callback-1',
                'data': f'recentmb:{self.mailbox.pk}',
                'message': {'chat': {'id': 999999999, 'type': 'private'}},
            },
        })
        letters_payload = api_request.call_args.args[1]
        self.assertIn(self.letter.subject, letters_payload['text'])
        self.assertIn('Обновить последние 5', letters_payload['reply_markup'])

    def test_telegram_bot_can_create_mailbox_for_selected_account(self):
        mailbox = create_mailbox_from_bot({
            'email': 'new.employee@gmail.com',
            'password': 'application-password',
            'provider': 'gmail',
            'manager_id': self.user.pk,
            'is_active': False,
        })

        self.assertEqual(mailbox.manager, self.user)
        self.assertEqual(mailbox.imap_host, 'imap.gmail.com')
        self.assertEqual(mailbox.smtp_host, 'smtp.gmail.com')
        self.assertFalse(mailbox.is_active)
        self.assertEqual(mailbox.get_password(), 'application-password')

    @override_settings(MANAGER_REGISTRATION_ENABLED=True)
    def test_registration_creates_profile_and_session(self):
        self.client.logout()
        response = self.client.post(reverse('register'), {
            'phone': '+993 63 33 33 33', 'device': 'Windows 11',
            'city': 'Туркменабад',
            'password1': 'Long-random-password-882', 'password2': 'Long-random-password-882',
        })
        self.assertRedirects(response, reverse('hub_dashboard'))
        profile = ManagerProfile.objects.get(phone='+99363333333', device='Windows 11')
        self.assertEqual(profile.city, 'Туркменабад')
        self.assertEqual(profile.region, self.region)

    @override_settings(MANAGER_REGISTRATION_ENABLED=True)
    def test_registration_accepts_a_custom_city(self):
        self.client.logout()
        page = self.client.get(reverse('register'))
        self.assertContains(page, 'list="city-options"')
        self.assertContains(page, 'Туркменабад')
        response = self.client.post(reverse('register'), {
            'phone': '+993 63 44 44 44', 'device': 'Android',
            'city': 'Новый город',
            'password1': 'Long-random-password-883', 'password2': 'Long-random-password-883',
        })
        self.assertRedirects(response, reverse('hub_dashboard'))
        profile = ManagerProfile.objects.get(phone='+99363444444')
        self.assertEqual(profile.city, 'Новый город')
        self.assertIsNone(profile.region)

    def test_mailbox_form_autoconfigures_yandex_and_encrypts_password(self):
        form = MailboxForm(data={
            'email': 'newbox@yandex.ru', 'display_name': 'Новый ящик', 'provider': 'yandex',
            'region': self.region.pk, 'owner_phone': '+99361111111',
            'password': 'app-secret', 'imap_port': 993, 'imap_use_ssl': True,
            'smtp_port': 465, 'smtp_use_ssl': True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        mailbox = form.save(commit=False)
        self.assertEqual(mailbox.imap_host, 'imap.yandex.ru')
        self.assertEqual(mailbox.smtp_host, 'smtp.yandex.ru')
        self.assertNotIn('app-secret', mailbox.password_encrypted)
        self.assertEqual(mailbox.get_password(), 'app-secret')

    def test_mailbox_form_autoconfigures_gmail(self):
        form = MailboxForm(data={
            'email': 'student@gmail.com', 'display_name': 'Gmail ящик', 'provider': 'gmail',
            'region': self.region.pk, 'owner_phone': '+99361111111',
            'password': 'sixteen-digit-app-password', 'imap_port': 993, 'imap_use_ssl': True,
            'smtp_port': 465, 'smtp_use_ssl': True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        mailbox = form.save(commit=False)
        self.assertEqual((mailbox.imap_host, mailbox.imap_port), ('imap.gmail.com', 993))
        self.assertEqual((mailbox.smtp_host, mailbox.smtp_port), ('smtp.gmail.com', 465))

    def test_mailbox_form_autoconfigures_sanly_with_mailbox_password(self):
        form = MailboxForm(data={
            'email': 'student@sanly.tm', 'display_name': 'Sanly ящик', 'provider': 'sanly',
            'region': self.region.pk, 'owner_phone': '+99361111111',
            'password': 'mailbox-password', 'imap_port': 993, 'imap_use_ssl': True,
            'smtp_port': 465, 'smtp_use_ssl': True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        mailbox = form.save(commit=False)
        self.assertEqual((mailbox.imap_host, mailbox.imap_port), ('mail.sanly.tm', 993))
        self.assertEqual((mailbox.smtp_host, mailbox.smtp_port), ('mail.sanly.tm', 465))
        self.assertTrue(mailbox.imap_use_ssl)
        self.assertTrue(mailbox.smtp_use_ssl)
        self.assertEqual(mailbox.get_password(), 'mailbox-password')

    @patch('mailer.services.imaplib.IMAP4_SSL')
    def test_imap_sync_imports_message_once(self, imap_class):
        raw = (b'From: University <admission@university.example>\r\n'
               b'To: device-box@yandex.ru\r\nSubject: Admission update\r\n'
               b'Date: Fri, 11 Jul 2026 10:00:00 +0000\r\n\r\nDocuments accepted.')
        connection = imap_class.return_value
        connection.select.return_value = ('OK', [b'1'])
        connection.uid.side_effect = [('OK', [b'501']), ('OK', [(b'501 (RFC822)', raw)])]
        self.mailbox.imap_host = 'imap.yandex.ru'
        self.mailbox.set_password('app-password')
        self.mailbox.save()
        self.assertEqual(sync_mailbox(self.mailbox), 1)
        imported = InboundMessage.objects.get(mailbox=self.mailbox, external_uid='501')
        self.assertEqual(imported.sender_email, 'admission@university.example')
        self.assertIn('Documents accepted', imported.body_text)

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='-1001234567890',
    )
    @patch('mailer.services.imaplib.IMAP4_SSL')
    def test_first_mailbox_sync_imports_history_without_telegram(self, imap_class):
        source = EmailMessage()
        source['From'] = 'University <admission@university.example>'
        source['To'] = self.mailbox.email
        source['Subject'] = 'Existing history'
        source['Date'] = format_datetime(timezone.now() - timedelta(minutes=5))
        source.set_content('Existing letter.')
        connection = imap_class.return_value
        connection.list.return_value = ('NO', [])
        connection.select.return_value = ('OK', [b'1'])
        connection.uid.side_effect = [
            ('OK', [b'901']),
            ('OK', [(b'901 (RFC822)', source.as_bytes())]),
        ]
        self.mailbox.imap_host = 'imap.yandex.ru'
        self.mailbox.set_password('app-password')
        self.mailbox.last_synced_at = None
        self.mailbox.save()

        self.assertEqual(sync_mailbox(self.mailbox), 1)

        imported = InboundMessage.objects.get(mailbox=self.mailbox, external_uid='901')
        self.assertFalse(imported.telegram_notification_pending)
        self.assertFalse(EmailAIAnalysis.objects.filter(message=imported).exists())

    @override_settings(
        TELEGRAM_NOTIFICATIONS_ENABLED=True,
        TELEGRAM_BOT_TOKEN='test-token',
        TELEGRAM_CHAT_ID='-1001234567890',
    )
    @patch('mailer.tasks.notify_inbound_message_task')
    @patch('mailer.services.imaplib.IMAP4_SSL')
    def test_subsequent_sync_queues_new_letter_for_telegram(self, imap_class, telegram_task):
        source = EmailMessage()
        source['From'] = 'University <admission@university.example>'
        source['To'] = self.mailbox.email
        source['Subject'] = 'Actually new'
        source['Date'] = format_datetime(timezone.now())
        source.set_content('New letter.')
        connection = imap_class.return_value
        connection.list.return_value = ('NO', [])
        connection.select.return_value = ('OK', [b'1'])
        connection.uid.side_effect = [
            ('OK', [b'902']),
            ('OK', [(b'902 (RFC822)', source.as_bytes())]),
        ]
        self.mailbox.imap_host = 'imap.yandex.ru'
        self.mailbox.set_password('app-password')
        self.mailbox.last_synced_at = timezone.now() - timedelta(hours=1)
        self.mailbox.telegram_notifications_after = timezone.now() - timedelta(hours=2)
        self.mailbox.save()
        self.assertEqual(sync_mailbox(self.mailbox), 1)

        imported = InboundMessage.objects.get(mailbox=self.mailbox, external_uid='902')
        self.assertTrue(imported.telegram_notification_pending)
        self.assertFalse(EmailAIAnalysis.objects.filter(message=imported).exists())
        telegram_task.assert_called_once_with(imported.pk, priority=100)

    def test_gemini_configuration_encrypts_multiple_keys(self):
        config = GeminiConfiguration.load()
        config.set_api_keys(['first-secret-key', 'second-secret-key'])
        config.save()
        self.assertNotIn('first-secret-key', config.api_keys_encrypted)
        self.assertEqual(config.get_api_keys(), ['first-secret-key', 'second-secret-key'])

    @patch('mailer.ai.requests.post')
    def test_ai_analysis_classifies_university_and_extracts_credentials(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            'candidates': [{
                'content': {'parts': [{'text': '''{
                    "is_important": true,
                    "importance_score": 94,
                    "is_university": true,
                    "university_name": "БГМУ",
                    "university_group": "medical",
                    "topic": "documents",
                    "summary": "Не хватает документа.",
                    "reason": "Вуз просит действие.",
                    "action_required": "Загрузить паспорт.",
                    "deadline": "до 30 июля",
                    "links": ["https://lk.example.edu/upload"],
                    "logins": ["student-17"],
                    "passwords": ["code-991"]
                }'''}]}
            }],
        }
        config = GeminiConfiguration.load()
        config.set_api_keys(['test-key'])
        config.save()
        self.letter.body_text = 'Загрузите паспорт: https://lk.example.edu/upload'
        self.letter.save(update_fields=('body_text',))

        analysis = analyze_inbound_message(self.letter.pk, allow_telegram=False)

        self.assertEqual(analysis.status, 'completed')
        self.assertTrue(analysis.is_important)
        self.assertEqual(analysis.university_group, 'medical')
        self.assertEqual(analysis.extracted_logins, ['student-17'])
        self.assertEqual(analysis.extracted_passwords, ['code-991'])
        self.assertIn('https://lk.example.edu/upload', analysis.extracted_links)
        headers = post.call_args.kwargs['headers']
        self.assertEqual(headers['x-goog-api-key'], 'test-key')

    def test_literal_link_extraction_reads_html_href(self):
        self.letter.body_text = 'Откройте https://lk.example.edu/start.'
        self.letter.body_html = '<a href="https://lk.example.edu/login">Кабинет</a>'
        links = extract_literal_links(self.letter)
        self.assertIn('https://lk.example.edu/login', links)

    def test_cheap_ai_filter_skips_noise_and_keeps_credentials(self):
        self.letter.sender_email = 'news@youtube.com'
        self.letter.subject = 'Новые рекомендации недели'
        self.letter.body_text = 'Посмотрите новые видео и подпишитесь на канал.'
        self.assertFalse(should_analyze_with_gemini(self.letter))

        self.letter.sender_email = 'admission@university.example'
        self.letter.subject = 'Доступ в личный кабинет'
        self.letter.body_text = 'Логин student-7, пароль Temp-991, вход: https://lk.example.edu'
        self.assertTrue(should_analyze_with_gemini(self.letter))

    def test_ai_dashboard_is_hidden_from_normal_message_detail(self):
        EmailAIAnalysis.objects.create(
            message=self.letter,
            status='completed',
            is_important=True,
            importance_score=91,
            is_university=True,
            university_name='БГМУ',
            university_group='medical',
            topic='documents',
            summary='Не хватает документа.',
            action_required='Загрузить паспорт.',
            extracted_links=['https://lk.example.edu'],
            extracted_logins=['student-17'],
            extracted_passwords=['code-991'],
            analyzed_at=timezone.now(),
        )
        self.client.force_login(self.user)
        dashboard = self.client.get(reverse('ai_dashboard'))
        self.assertContains(dashboard, 'БГМУ')
        self.assertContains(dashboard, 'Загрузить паспорт')
        detail = self.client.get(reverse('message_detail', args=[self.letter.pk]))
        self.assertNotContains(detail, 'AI-анализ · 91/100')
        self.assertNotContains(detail, 'Логин: student-17')

    @override_settings(
        AI_ANALYSIS_REMOTE_WORKER=True,
        AI_WORKER_TOKEN='worker-test-token',
    )
    def test_remote_ai_worker_leases_and_submits_structured_analysis(self):
        config = GeminiConfiguration.load()
        config.model_name = 'gemini-3.5-flash'
        config.save()
        pending = EmailAIAnalysis.objects.create(message=self.letter)
        headers = {'HTTP_AUTHORIZATION': 'Bearer worker-test-token'}

        lease = self.client.post(
            reverse('ai_worker_lease'),
            data='{}',
            content_type='application/json',
            **headers,
        )

        self.assertEqual(lease.status_code, 200)
        self.assertEqual(lease.json()['job_id'], pending.pk)
        self.assertEqual(lease.json()['model'], 'gemini-3.5-flash')
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'processing')

        result = {
            'job_id': pending.pk,
            'analysis': {
                'is_important': True,
                'importance_score': 93,
                'is_university': True,
                'university_name': 'БГМУ',
                'university_group': 'medical',
                'topic': 'documents',
                'summary': 'Нужно добавить документ.',
                'reason': 'Требуется действие.',
                'action_required': 'Загрузить паспорт.',
                'reply_draft': 'Здравствуйте! Паспорт загрузим сегодня.',
                'deadline': 'завтра',
                'links': ['https://lk.example.edu'],
                'logins': [],
                'passwords': [],
            },
        }
        submit = self.client.post(
            reverse('ai_worker_submit'),
            data=result,
            content_type='application/json',
            **headers,
        )

        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.json()['status'], 'completed')
        pending.refresh_from_db()
        self.assertEqual(pending.status, 'completed')
        self.assertTrue(pending.is_important)
        self.assertEqual(
            pending.suggested_reply,
            'Здравствуйте! Паспорт загрузим сегодня.',
        )

    def test_telegram_text_ignores_ai_and_uses_original_letter(self):
        EmailAIAnalysis.objects.create(
            message=self.letter,
            status='completed',
            is_important=True,
            importance_score=95,
            is_university=True,
            university_name='МАИ',
            university_group='technical',
            topic='personal_account',
            summary='Создан личный кабинет.',
            action_required='Войти и заполнить анкету.',
            extracted_links=['https://lk.mai.ru'],
            extracted_logins=['student-login'],
            extracted_passwords=['secret-code'],
            analyzed_at=timezone.now(),
        )
        self.letter.body_text = 'Оригинальный текст нового письма.'
        text = format_inbound_notification(self.letter)
        self.assertIn('Оригинальный текст нового письма.', text)
        self.assertNotIn('Войти и заполнить анкету', text)
        self.assertNotIn('student-login', text)
        self.assertNotIn('secret-code', text)

    @patch('mailer.services.imaplib.IMAP4_SSL')
    def test_imap_sync_stores_received_attachment(self, imap_class):
        from email.message import EmailMessage

        source = EmailMessage()
        source['From'] = 'University <admission@university.example>'
        source['To'] = self.mailbox.email
        source['Subject'] = 'Documents'
        source['Date'] = 'Tue, 21 Jul 2026 10:00:00 +0000'
        source.set_content('Attached document.')
        source.add_attachment(b'PDF test', maintype='application', subtype='pdf', filename='offer.pdf')

        connection = imap_class.return_value
        connection.list.return_value = ('NO', [])
        connection.select.return_value = ('OK', [b'1'])
        connection.uid.side_effect = [('OK', [b'777']), ('OK', [(b'777 (RFC822)', source.as_bytes())])]
        self.mailbox.imap_host = 'imap.yandex.ru'
        self.mailbox.set_password('app-password')
        self.mailbox.save()

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.assertEqual(sync_mailbox(self.mailbox), 1)
            attachment = InboundAttachment.objects.get(message__external_uid='777')
            self.assertEqual((attachment.original_name, attachment.size), ('offer.pdf', 8))
            self.client.force_login(self.user)
            response = self.client.get(reverse('inbound_attachment_download', args=[attachment.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'PDF test')

    def test_imap_special_use_folders_find_spam_and_sent(self):
        connection = type('Connection', (), {})()
        connection.list = lambda: ('OK', [
            b'(\\HasNoChildren \\Spam) "/" "[Gmail]/Spam"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
        ])
        self.mailbox.provider = 'gmail'
        folders = _imap_folder_specs(connection, self.mailbox)
        self.assertEqual([item[2] for item in folders], ['primary', 'spam', 'sent'])

    def test_mosaic_calendar_parser_detects_status_and_action(self):
        parsed = parse_calendar_html('''
            <h3>Ashgabat</h3><h3>August 2026</h3>
            <table>
              <tr><td><strong>3 August 2026</strong></td><td><h6>Reserved <b>0</b></h6></td></tr>
              <tr><td><strong>4 August 2026</strong></td><td><a href="/appointment/4">Book now</a></td></tr>
            </table>
        ''')
        self.assertEqual(parsed['title'], 'Ashgabat')
        self.assertEqual(len(parsed['rows']), 2)
        self.assertFalse(parsed['rows'][0]['available'])
        self.assertTrue(parsed['rows'][1]['available'])
        self.assertEqual(parsed['rows'][1]['links'], ['/appointment/4'])

    @patch('mailer.mosaic._send_email')
    @patch('mailer.mosaic.fetch_calendar_snapshot')
    def test_mosaic_monitor_saves_baseline_then_notifies_on_change(self, fetch, send_email):
        config = MosaicMonitorConfiguration.load()
        config.set_sender_password('app-password')
        config.save()
        baseline = {
            'version': 1,
            'digest': 'one',
            'calendars': {
                'https://appointment.mosaicvisa.com/calendar/11': {
                    '2026-08': {
                        'title': 'Ashgabat',
                        'rows': [{
                            'date': '3 August 2026',
                            'status': 'Reserved 0',
                            'actionable': False,
                            'available': False,
                            'links': [],
                        }],
                    },
                },
            },
        }
        changed = {
            **baseline,
            'digest': 'two',
            'calendars': {
                'https://appointment.mosaicvisa.com/calendar/11': {
                    '2026-08': {
                        'title': 'Ashgabat',
                        'rows': [{
                            'date': '3 August 2026',
                            'status': 'Book now',
                            'actionable': True,
                            'available': True,
                            'links': ['/appointment/3'],
                        }],
                    },
                },
            },
        }
        fetch.side_effect = [baseline, changed]
        send_email.return_value = ['begenchyagmurow2008@gmail.com']

        first = check_mosaic_calendars(config)
        second = check_mosaic_calendars(config)

        self.assertTrue(first['baseline'])
        self.assertTrue(second['changed'])
        self.assertEqual(len(second['opened']), 1)
        send_email.assert_called_once()
        subject = send_email.call_args.args[1]
        body = send_email.call_args.args[2]
        self.assertIn('ОТКРЫТА ЗАПИСЬ', subject)
        self.assertIn('3 August 2026', body)
        self.assertTrue(MosaicMonitorEvent.objects.filter(event_type='opening', notification_sent=True).exists())

    @patch('mailer.mosaic._send_email')
    @patch('mailer.mosaic.fetch_calendar_snapshot')
    def test_mosaic_monitor_logs_regular_change_without_email(self, fetch, send_email):
        config = MosaicMonitorConfiguration.load()
        baseline = {
            'version': 1,
            'digest': 'one',
            'calendars': {
                'https://appointment.mosaicvisa.com/calendar/11': {
                    '2026-08': {
                        'title': 'Ashgabat',
                        'rows': [{
                            'date': '3 August 2026', 'status': 'Reserved 0',
                            'actionable': False, 'available': False, 'links': [],
                        }],
                    },
                },
            },
        }
        changed = {
            **baseline,
            'digest': 'two',
            'calendars': {
                'https://appointment.mosaicvisa.com/calendar/11': {
                    '2026-08': {
                        'title': 'Ashgabat',
                        'rows': [{
                            'date': '3 August 2026', 'status': 'Reserved 1',
                            'actionable': False, 'available': False, 'links': [],
                        }],
                    },
                },
            },
        }
        fetch.side_effect = [baseline, changed]

        check_mosaic_calendars(config)
        result = check_mosaic_calendars(config)

        self.assertTrue(result['changed'])
        self.assertEqual(result['opened'], [])
        send_email.assert_not_called()
        event = MosaicMonitorEvent.objects.get(event_type='change')
        self.assertFalse(event.notification_sent)
        self.assertIn('Новых открытых дат нет', event.summary)
        self.assertIn('Ashgabat: добавлено 0, удалено 0, изменено 1', event.details)

    def test_mosaic_parser_does_not_treat_no_appointments_as_available(self):
        parsed = parse_calendar_html('''
            <h3>Ashgabat</h3><h3>August 2026</h3>
            <table>
              <tr><td>5 August 2026</td><td>No appointments available</td></tr>
            </table>
        ''')

        self.assertFalse(parsed['rows'][0]['available'])

    @patch('mailer.views.send_mosaic_test_email')
    def test_mosaic_settings_can_save_and_send_test(self, send_test):
        send_test.return_value = ['second@example.com']
        self.client.force_login(self.user)
        response = self.client.post(reverse('mosaic_monitor_settings'), {
            'enabled': 'on',
            'calendar_urls': (
                'https://appointment.mosaicvisa.com/calendar/11\n'
                'https://appointment.mosaicvisa.com/calendar/12'
            ),
            'check_interval_minutes': '5',
            'months_ahead': '3',
            'sender_email': 'akylpro2023@gmail.com',
            'sender_password': 'new-app-password',
            'recipients': 'first@example.com, second@example.com',
            'subject_prefix': '[Mosaic Visa]',
            'action': 'test',
        })
        self.assertRedirects(response, reverse('mosaic_monitor_settings'))
        config = MosaicMonitorConfiguration.load()
        self.assertEqual(config.recipient_list(), ['first@example.com', 'second@example.com'])
        self.assertEqual(config.get_sender_password(), 'new-app-password')
        send_test.assert_called_once_with(config)

    def test_mosaic_page_is_public_and_settings_are_hidden_in_dialog(self):
        self.client.logout()
        response = self.client.get(reverse('mosaic_monitor_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-mosaic-settings-open')
        self.assertContains(response, 'data-mosaic-settings-dialog')
        self.assertContains(response, 'noindex,nofollow')
        self.assertNotContains(response, 'Основная навигация')

    @override_settings(
        MAILBOX_IMPORT_API_TOKEN='test-import-token',
        MAILBOX_IMPORT_ALLOWED_ORIGIN='https://mail.tmmail.ru',
    )
    @patch('mailer.mailbox_api.process_mailbox_sync_task')
    def test_mailbox_import_api_creates_encrypted_mailbox_in_fixed_account(self, sync_task):
        api_user = get_user_model().objects.create_user(
            username='tmmail.ru',
            password='tmmail.ru',
        )
        ManagerProfile.objects.create(
            user=api_user,
            phone='tmmail.ru',
            device='tmmail.ru',
            region=self.region,
        )
        payload = {
            'email': 'api.student@gmail.com',
            'password': 'gmail-app-password',
            'display_name': 'API Student',
            'is_active': True,
        }
        response = self.client.post(
            reverse('api_create_mailbox'),
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-import-token',
            HTTP_ORIGIN='https://mail.tmmail.ru',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['mailbox']['account'], 'tmmail.ru')
        self.assertEqual(response['Access-Control-Allow-Origin'], 'https://mail.tmmail.ru')
        mailbox = Mailbox.objects.get(email='api.student@gmail.com')
        self.assertEqual(mailbox.manager, api_user)
        self.assertEqual(mailbox.provider, 'gmail')
        self.assertEqual(mailbox.imap_host, 'imap.gmail.com')
        self.assertNotEqual(mailbox.password_encrypted, payload['password'])
        self.assertEqual(mailbox.get_password(), payload['password'])
        sync_task.assert_called_once()

        duplicate = self.client.post(
            reverse('api_create_mailbox'),
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer test-import-token',
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()['status'], 'exists')

    @override_settings(
        MAILBOX_IMPORT_API_TOKEN='test-import-token',
        MAILBOX_IMPORT_ALLOWED_ORIGIN='https://mail.tmmail.ru',
    )
    def test_mailbox_import_api_requires_token_and_supports_cors_preflight(self):
        unauthorized = self.client.post(
            reverse('api_create_mailbox'),
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(unauthorized.status_code, 401)

        preflight = self.client.options(
            reverse('api_create_mailbox'),
            HTTP_ORIGIN='https://mail.tmmail.ru',
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type',
        )
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight['Access-Control-Allow-Origin'], 'https://mail.tmmail.ru')
        self.assertIn('Authorization', preflight['Access-Control-Allow-Headers'])

        forbidden = self.client.options(
            reverse('api_create_mailbox'),
            HTTP_ORIGIN='https://evil.example',
        )
        self.assertEqual(forbidden.status_code, 403)
