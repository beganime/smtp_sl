from django.test import TestCase, override_settings
from django.urls import reverse


class DeploymentReadinessTests(TestCase):
    def test_health_checks_database(self):
        response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')

    @override_settings(MANAGER_REGISTRATION_ENABLED=False)
    def test_manager_registration_is_disabled(self):
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 403)
