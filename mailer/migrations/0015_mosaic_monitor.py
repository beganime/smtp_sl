from django.db import migrations, models
import mailer.models


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0014_emailaianalysis_suggested_reply'),
    ]

    operations = [
        migrations.CreateModel(
            name='MosaicMonitorConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True, verbose_name='Мониторинг включён')),
                ('calendar_urls', models.TextField(default=mailer.models.default_mosaic_calendar_urls, help_text='По одной ссылке в строке.', verbose_name='Страницы календарей')),
                ('check_interval_minutes', models.PositiveSmallIntegerField(default=5, verbose_name='Интервал проверки, минут')),
                ('months_ahead', models.PositiveSmallIntegerField(default=3, help_text='Проверяется текущий месяц и указанное число следующих месяцев.', verbose_name='Месяцев вперёд')),
                ('sender_email', models.EmailField(default='akylpro2023@gmail.com', max_length=254, verbose_name='Почта отправителя')),
                ('sender_password_encrypted', models.TextField(blank=True, editable=False, verbose_name='Зашифрованный пароль приложения')),
                ('recipients', models.TextField(default='begenchyagmurow2008@gmail.com', help_text='Email через запятую или по одному в строке.', verbose_name='Получатели')),
                ('subject_prefix', models.CharField(default='[Mosaic Visa]', max_length=120, verbose_name='Префикс темы')),
                ('last_snapshot', models.JSONField(blank=True, default=dict, editable=False, verbose_name='Последний снимок')),
                ('last_checked_at', models.DateTimeField(blank=True, null=True, verbose_name='Последняя проверка')),
                ('last_changed_at', models.DateTimeField(blank=True, null=True, verbose_name='Последнее изменение')),
                ('last_notification_at', models.DateTimeField(blank=True, null=True, verbose_name='Последнее уведомление')),
                ('last_error', models.TextField(blank=True, verbose_name='Последняя ошибка')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Настройки обновлены')),
            ],
            options={
                'verbose_name': 'Монитор Mosaic Visa',
                'verbose_name_plural': 'Монитор Mosaic Visa',
            },
        ),
        migrations.CreateModel(
            name='MosaicMonitorEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('baseline', 'Базовый снимок'), ('change', 'Изменение календаря'), ('test', 'Тестовое письмо'), ('error', 'Ошибка')], max_length=20, verbose_name='Тип')),
                ('summary', models.CharField(max_length=500, verbose_name='Описание')),
                ('details', models.TextField(blank=True, verbose_name='Подробности')),
                ('notification_sent', models.BooleanField(default=False, verbose_name='Письмо отправлено')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
            ],
            options={
                'verbose_name': 'Событие монитора Mosaic Visa',
                'verbose_name_plural': 'События монитора Mosaic Visa',
                'ordering': ('-created_at',),
            },
        ),
    ]
