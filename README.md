# Умный Telegram-бот с веб-админкой и LLM-поведением

Telegram-бот для закрытого группового чата с динамическим поведением на базе LLM
(NVIDIA NIM), гибкими правилами, реакцией на триггеры, разрешением споров и
веб-админкой. Разворачивается на Vercel (serverless, webhook).

План работ и архитектура — в [`tickets/`](tickets/README.md).
Текущий статус: **код и автоматические проверки TICKET-01 готовы**; live
Vercel/Telegram/Upstash acceptance требует реальных credentials/deploy и ещё не
отмечен как пройденный в репозитории.

## Стек

- **Python 3.12 + FastAPI** одной функцией на **Vercel Hobby** (webhook, не polling).
- **Upstash Redis** (REST) — история, дедупликация и каталог уже замеченных
  пользователей. На проде обязателен Upstash, локально/в тестах доступен
  in-memory adapter. История ограничена 30 сообщениями; записи старше 30 дней
  отсекаются, а сам список также имеет sliding TTL
  (`HISTORY_RETENTION_SECONDS`).
- **NVIDIA NIM** через LangChain `ChatNVIDIA` (с тикета 02).
- **Upstash QStash** — декуплинг медленных LLM-вызовов (с тикета 02).

## Структура

```
api/index.py          # Vercel entrypoint: ре-экспорт FastAPI `app`
app/
  settings.py         # конфиг из окружения (.env локально)
  server.py           # сборка FastAPI + /api/health
  store/              # redis.py, history.py, dedup.py, users.py
  telegram/           # models.py, client.py, webhook.py
scripts/set_webhook.py# регистрация/диагностика вебхука
tests/                # pytest-набор
```

## Локальная разработка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# заполнить переменные Ticket 01, перечисленные ниже

# запустить локально (health): http://127.0.0.1:8000/api/health
uvicorn app.server:app --reload

# прогнать тесты
pytest
```

Без credentials Upstash приложение использует in-memory хранилище — только для
локального запуска и тестов. **На Vercel отсутствие постоянного Redis или ID
разрешённой группы считается ошибкой готовности**: health/webhook вернут `503`,
а не будут молча терять состояние.

Для проверки именно Vercel routing локально можно использовать:

```bash
vercel dev
```

## Переменные окружения

Для TICKET-01 заполни:

- `TELEGRAM_BOT_TOKEN` и `TELEGRAM_BOT_USERNAME`;
- `TELEGRAM_WEBHOOK_SECRET` (1–256 символов: буквы, цифры, `_`, `-`);
- `TELEGRAM_ALLOWED_CHAT_ID` — numeric ID единственной закрытой группы;
- `PUBLIC_BASE_URL`;
- `UPSTASH_REDIS_REST_URL` и `UPSTASH_REDIS_REST_TOKEN` на Vercel.

Пустые значения будущих тикетов можно оставить пустыми; не-секретные defaults
DeepSeek V4 Flash/Pro и QStash уже заполнены в шаблоне.

> ⚠️ Реальные секреты не коммитим (`.env` в `.gitignore`). Ключ NVIDIA, ранее
> лежавший в `.env.example`, перенесён в локальный `.env` — рекомендуется
> перевыпустить его на build.nvidia.com.

## Деплой на Vercel

1. Создать бота у **@BotFather** и получить token/username.
2. В @BotFather выполнить `/setprivacy` → выбрать бота → **Disable**. Если бот
   уже был в группе, удалить и добавить его снова. Альтернатива — сделать бота
   администратором группы.
3. Добавить бота в закрытую группу, написать обычное сообщение и до установки
   webhook узнать ID без вывода содержимого сообщений:
   ```bash
   python scripts/discover_chat_id.py
   ```
4. Заполнить `.env`, создать Upstash Redis и выполнить проверки:
   ```bash
   python scripts/check_redis.py
   python scripts/check_telegram.py
   ```
5. Импортировать репозиторий в Vercel и задать те же Env Vars, включая
   `PUBLIC_BASE_URL=https://<project>.vercel.app`.
6. Задеплоить и проверить readiness:
   ```bash
   vercel deploy --prod
   curl -f https://<project>.vercel.app/api/health
   ```
7. Зарегистрировать webhook:
   ```bash
   python scripts/set_webhook.py set     # регистрация
   python scripts/set_webhook.py info    # проверка (getWebhookInfo)
   ```
   Скрипт задаёт `max_connections=1`: для маленькой единственной группы это
   сохраняет порядок ingestion/context без проблем с пропускной способностью.
   Pending updates по умолчанию сохраняются; destructive `--drop-pending`
   используется только при осознанном сбросе очереди.
8. В группе написать **обычную фразу** и `/ping`. Фраза должна появиться в
   `hist:<chat_id>` в Upstash, `/ping` должен вернуть `pong`. Команда
   `/ping@OtherBot` должна быть проигнорирована.

## API (TICKET-01)

| Метод | Путь | Назначение |
|---|---|---|
| GET  | `/api/health` | health-check + текущий бэкенд хранилища |
| POST | `/api/telegram/webhook` | приём апдейтов (проверка секрета, дедуп, лог, `/ping`, `/help`) |
