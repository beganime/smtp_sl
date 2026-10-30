import json
from unittest.mock import patch

from django.test import TestCase, override_settings


@override_settings(
    TMMAIL_PROVISION_API_TOKEN='manager-token',
    MAILU_API_TOKEN='mailu-token',
    MAILBOX_IMPORT_API_TOKEN='registry-token',
    TMMAIL_MAILBOX_DOMAIN='tmmail.ru',
)
class TMMailProvisionApiTests(TestCase):
    def payload(self):
        return {
            'event_id': 'approval-1',
            'sl_id': 'SL-2027-001',
            'email': 'ivan.ivanov2008@tmmail.ru',
            'password': 'Ivan_0710',
            'display_name': 'Ivan Ivanov',
        }

    def post(self, payload=None, token='manager-token'):
        return self.client.post(
            '/api/v1/tmmail/provision/',
            data=json.dumps(payload or self.payload()),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

    def test_rejects_unauthorized_request(self):
        self.assertEqual(self.post(token='wrong').status_code, 401)

    def test_rejects_foreign_domain(self):
        payload = self.payload()
        payload['email'] = 'student@example.com'
        self.assertEqual(self.post(payload).status_code, 400)

    @patch('mailer.tmmail_api.Mailbox.objects.filter')
    @patch('mailer.tmmail_api.register_mailbox')
    @patch('mailer.tmmail_api.create_mailu_user')
    @patch('mailer.tmmail_api.list_mailu_users', return_value=set())
    def test_creates_mailu_user_and_registry(
        self,
        list_users,
        create_user,
        register,
        mailbox_filter,
    ):
        mailbox_filter.return_value.first.return_value = None
        register.return_value = {'status': 'created'}

        response = self.post()

        self.assertEqual(response.status_code, 201)
        list_users.assert_called_once_with()
        create_user.assert_called_once()
        register.assert_called_once()

