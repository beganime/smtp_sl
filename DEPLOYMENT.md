# SMTP_SL deployment

Рабочая SQLite, вложения и временные файлы находятся в persistent volume
`smtp_data`, а не в Git. Пока общий диск не выбран, это каноническое локальное
хранилище SMTP_SL. Почтовые ящики Mailu хранятся отдельно в `/opt/mailu/mail`.

```bash
copy deploy/.env.example .env
docker compose up --build -d
```

Публичная регистрация менеджеров отключена. Ящики добавляются через защищённый
`POST /api/v1/mailboxes/`, пароли сохраняются зашифрованно. Для нового ящика
TMMail используется квота 250 МБ.

После подтверждения анкеты ManagerSL вызывает защищённый
`POST /api/v1/tmmail/provision/`. SMTP_SL создаёт ящик в Mailu и сразу ставит
его на сбор писем. Повторный запрос с тем же адресом безопасно возвращает уже
созданный ящик. Для этого настраиваются `TMMAIL_PROVISION_API_TOKEN`,
`MAILU_API_TOKEN` и `MAILBOX_IMPORT_API_TOKEN`.
