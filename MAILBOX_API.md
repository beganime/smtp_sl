# API добавления почтовых ящиков

Endpoint:

```text
POST https://tmmail.ru/api/v1/mailboxes/
```

Авторизация:

```text
Authorization: Bearer YOUR_API_TOKEN
```

Все созданные ящики автоматически прикрепляются к аккаунту `tmmail.ru`.
Пароль почтового ящика хранится в SMTP_SL в зашифрованном виде и никогда не возвращается в ответе API.

## Пример запроса

```bash
curl -X POST "https://tmmail.ru/api/v1/mailboxes/" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "student@sanly.tm",
    "password": "Mailbox_0710",
    "provider": "sanly",
    "display_name": "Student Name",
    "is_active": true
  }'
```

`provider` можно не передавать для Gmail, Яндекс, Mail.ru/List.ru и Sanly.tm — он будет определён по домену.

Допустимые значения `provider`:

- `gmail`
- `yandex`
- `mailru`
- `sanly`
- `other`

Для `provider: "other"` дополнительно нужны `imap_host` и `smtp_host`. Необязательные поля: `imap_port`, `smtp_port`, `imap_use_ssl`, `smtp_use_ssl`.

## Успешный ответ — HTTP 201

```json
{
  "status": "created",
  "mailbox": {
    "id": 501,
    "email": "student@sanly.tm",
    "display_name": "Student Name",
    "provider": "sanly",
    "is_active": true,
    "account": "tmmail.ru"
  },
  "sync": {
    "queued": true,
    "run_id": 42
  }
}
```

## Ящик уже существует — HTTP 409

```json
{
  "status": "exists",
  "error": "Mailbox already exists.",
  "mailbox": {
    "id": 501,
    "email": "student@sanly.tm",
    "provider": "sanly",
    "is_active": true
  }
}
```

Другие коды:

- `400` — некорректный JSON или поля;
- `401` — отсутствует или неверный Bearer-токен;
- `403` — браузерный запрос пришёл не с `https://mail.tmmail.ru`;
- `405` — метод отличается от POST;
- `503` — аккаунт `tmmail.ru` ещё не настроен.

## JavaScript для mail.tmmail.ru

API-токен безопаснее хранить на серверной стороне `mail.tmmail.ru`, а не в браузерном JavaScript.

```javascript
const response = await fetch("https://tmmail.ru/api/v1/mailboxes/", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.TMMAIL_API_TOKEN}`,
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
    email: "student@gmail.com",
    password: "application-password",
    provider: "gmail",
    display_name: "Student Name",
    is_active: true
  })
});

const result = await response.json();
if (!response.ok) {
  throw new Error(result.error || "Mailbox API request failed");
}
```
