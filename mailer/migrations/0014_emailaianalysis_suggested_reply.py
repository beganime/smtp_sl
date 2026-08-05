from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('mailer', '0013_geminiconfiguration_emailaianalysis'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailaianalysis',
            name='suggested_reply',
            field=models.TextField(blank=True, verbose_name='Предлагаемый ответ'),
        ),
    ]
