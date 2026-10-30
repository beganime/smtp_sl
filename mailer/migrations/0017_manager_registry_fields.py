from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mailer', '0016_alter_mosaicmonitorevent_event_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='mailbox',
            name='canonical_sl_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text='Заполняется автоматически при создании ящика из карточки клиента.',
                max_length=32,
                verbose_name='SL-ID в ManagerSL',
            ),
        ),
        migrations.AddField(
            model_name='inboundmessage',
            name='manager_sync_pending',
            field=models.BooleanField(
                db_index=True,
                default=False,
                verbose_name='Ожидает синхронизации с ManagerSL',
            ),
        ),
        migrations.AddField(
            model_name='inboundmessage',
            name='manager_synced_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name='Синхронизировано с ManagerSL',
            ),
        ),
        migrations.AddField(
            model_name='inboundmessage',
            name='manager_sync_error',
            field=models.TextField(blank=True, verbose_name='Ошибка синхронизации с ManagerSL'),
        ),
    ]
