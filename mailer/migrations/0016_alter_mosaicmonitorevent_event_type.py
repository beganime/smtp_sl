from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0015_mosaic_monitor'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mosaicmonitorevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('baseline', 'Базовый снимок'),
                    ('opening', 'Открыта запись'),
                    ('change', 'Изменение календаря'),
                    ('test', 'Тестовое письмо'),
                    ('error', 'Ошибка'),
                ],
                max_length=20,
                verbose_name='Тип',
            ),
        ),
    ]
