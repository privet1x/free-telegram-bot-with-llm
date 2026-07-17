# TICKET-01 — Надёжный каркас, Telegram webhook и каноническая история чата

**Размер:** L · **Зависит от:** — · **Разблокирует:** 02
**Контракт имён/ключей/env:** см. `00-ARCHITECTURE.md`

**Статус:** локальная реализация + automated suite готовы; live acceptance не
считается пройденным без реального Vercel/Telegram/Upstash smoke.

## Цель

Поднять фундамент, который реально деплоится на Vercel Hobby, принимает апдейты
Telegram только от **одной разрешённой закрытой группы**, надёжно сохраняет
последние 30 сообщений и наблюдаемых пользователей в Upstash Redis и синхронно
отвечает на `/ping` и `/help`. Повторная доставка или временный сбой Redis не
должны ни дублировать историю, ни навсегда терять апдейт.

LLM и QStash в этом тикете ещё не реализуются.

## Зафиксированные решения

- Vercel FastAPI preset: одна функция, `api/index.py`, ASGI-переменная `app`,
  Python 3.12 и корневой `requirements.txt`.
- MVP обслуживает один чат: `TELEGRAM_ALLOWED_CHAT_ID`. На Vercel отсутствие
  allowlist или постоянного Redis — ошибка готовности, а не молчаливый fallback
  на память процесса.
- Telegram Privacy Mode необходимо отключить через @BotFather (или сделать бота
  администратором группы). После `/setprivacy -> Disable` уже добавленного бота нужно
  удалить из группы и добавить снова. Без этого полная история невозможна.
- `dedup:update:{update_id}` означает **успешно сохранённый входящий апдейт**, а
  не «начали обработку». TTL — 24 часа.
- Порядок приёма: проверить готовность/secret/chat → быстро проверить завершённый
  dedup → выполнить идемпотентные записи user/history → атомарно отметить dedup →
  только победитель гонки формирует служебный ответ.
- История делает upsert по `(chat_id, message_id)`: `edited_message` заменяет
  исходную запись и не занимает второе место в буфере. Операция upsert+trim
  должна быть атомарной на Redis и эквивалентной в MemoryKV.
- Помимо лимита 30 записей, atomic write/read отсекает records старше
  `HISTORY_RETENTION_SECONDS` по Telegram `ts`, а ключ имеет такой же sliding TTL
  (по умолчанию 30 дней). Read физически удаляет expired records, не только
  скрывает их из ответа. Админка позже добавит явную очистку.
- Время берётся из Telegram `message.date`; для правки сохраняется `edit_date`.
  Сохраняется также минимальный контекст reply.
- На входящем сообщении обновляется наблюдаемый каталог пользователей. Добавление
  по `@username` в будущей админке поддерживается только для уже наблюдаемых
  ботом пользователей; Telegram ID всегда остаётся каноническим идентификатором.
- Синхронный Upstash SDK не должен блокировать event loop: webhook оформляется
  как sync FastAPI handler (threadpool) либо использует эквивалентный безопасный
  адаптер.

## Объём работ

- [x] Структура репозитория и пакет `app/` по `00-ARCHITECTURE.md §2`.
- [x] `requirements.txt`, `requirements-dev.txt`, `.python-version`,
      `vercel.json`; воспроизводимые совместимые версии зависимостей.
- [x] `.env.example`:
      - секреты пустые;
      - не-секретные defaults (`LLM_MODEL_*`, `QSTASH_URL`) заполнены;
      - пустые значения не ломают типизированные будущие настройки;
      - присутствует `TELEGRAM_ALLOWED_CHAT_ID`.
- [x] `app/settings.py` — типизированный `Settings`; пустые значения шаблона
      игнорируются, production readiness валидируется отдельно.
- [x] `app/store/redis.py` — ленивый Upstash REST-клиент и эквивалентный
      thread-safe MemoryKV для тестов; `ping`, обычные KV-операции и атомарный
      history-upsert.
- [x] `app/store/history.py`:
      - `upsert(chat_id, record)` — добавить новое либо заменить edit;
      - всегда `trim` до 30;
      - prune records старше retention и обновлять sliding TTL;
      - `recent(chat_id, n)` — newest-first;
      - повреждённая единичная запись не валит весь буфер.
- [x] `app/store/users.py`:
      - `observe(user)` обновляет `user:{id}` и `username:{normalized}`;
      - удаляет старый username-index при переименовании;
      - delayed update с меньшим `(last_seen_at,last_update_id)` не откатывает
        более новый профиль;
      - `resolve_username` возвращает только наблюдаемого пользователя.
- [x] `app/store/dedup.py` — `already_seen` + финальный `mark_seen` после
      успешных идемпотентных записей, TTL 24 часа.
- [x] `app/telegram/models.py` — безопасный разбор `message`/`edited_message`,
      Telegram timestamps, caption/caption_entities, reply metadata; команда с
      `@suffix` считается нашей только при совпадении с
      `TELEGRAM_BOT_USERNAME`.
- [x] `app/telegram/client.py` — базовый Bot API клиент; обычный вызов возвращает
      нормализованный `result` и проверяет Telegram `ok`; webhook-reply остаётся
      для мгновенных `/ping`/`/help`.
- [x] `app/telegram/webhook.py`:
      1. проверить production readiness;
      2. constant-time проверить secret (иначе 403);
      3. распарсить update; непригодный update → 200 без работы;
      4. чужой `chat_id` → 200 `ignored`, без Redis-данных и ответа;
      5. завершённый update → 200 `dedup`;
      6. идемпотентно записать user + history;
      7. отметить dedup; при concurrent race отвечает только победитель;
      8. `/ping`/`/help` без suffix или с suffix этого бота → webhook reply;
      9. остальное → быстрый 200.
- [x] `api/index.py` + `/api/health`: health реально проверяет Redis `PING` и
      обязательную production-конфигурацию; при неготовности возвращает 503 без
      раскрытия секретов.
- [x] `scripts/set_webhook.py` — `set/info/delete`, разрешённые update-типы,
      `max_connections=1` для последовательного ingestion, проверка формата
      secret/HTTPS и ненулевой exit code при HTTP/API ошибке; pending updates не
      удаляются без явного destructive `--drop-pending`.
- [x] `scripts/check_telegram.py` — проверка token, username, privacy mode и
      доступности `TELEGRAM_ALLOWED_CHAT_ID`; `scripts/check_redis.py` — реальный
      round-trip Redis, включая production Lua EVAL history upsert/edit/prune и
      observed profile/username-alias transition.
- [x] README — локальный запуск, `vercel dev`/`vercel deploy`, env, Privacy Mode,
      allowlist, smoke-check scripts и регистрация webhook.
- [x] Автотесты MemoryKV/webhook и контрактные тесты Upstash-adapter через mock;
      CI запускает `pytest` и lint.

## Каноническая запись истории

```json
{
  "message_id": 123,
  "source_update_id": 987,
  "user_id": 42,
  "username": "alice",
  "name": "Alice",
  "text": "сообщение",
  "ts": 1784200000,
  "edit_ts": null,
  "is_edited": false,
  "is_bot": false,
  "reply_to": {
    "message_id": 120,
    "user_id": 7,
    "is_bot": true,
    "text": "цитируемый текст"
  }
}
```

`source_update_id` — Telegram update, который принёс эту версию записи; он
разрешает безопасно упорядочить запоздалый original/update после edit.
`reply_to` может быть `null`. Ticket 02 дополнительно будет сохранять финальные
исходящие ответы бота после успешного Bot API вызова, когда известен настоящий
`message_id` Telegram.

Основной и reply-текст ограничиваются 4096 Unicode characters до
сериализации. Новая запись без integer `message_id`/`ts` отклоняется;
повреждённый JSON или legacy record с невалидным `ts` никогда не
возвращается и физически удаляется read-prune.

## Критерии приёмки

1. `pytest` и lint проходят; есть failure-path тест: ошибка history после начала
   обработки → повтор того же update успешно сохраняется, а не подавляется.
2. `Settings(_env_file=".env.example")` создаётся без ValidationError; модели
   DeepSeek и QStash URL получают ожидаемые defaults.
3. На Vercel без Redis либо без `TELEGRAM_ALLOWED_CHAT_ID` health/webhook сообщают
   503; с валидной конфигурацией `GET /api/health` → `ok:true`, реальный Redis
   отвечает на `PING`.
4. `scripts/check_telegram.py` подтверждает: token/username верны, разрешённый
   chat — `group`/`supergroup`, а обычные сообщения видны (Privacy Mode выключен
   либо bot действительно group admin); mismatch даёт non-zero exit.
5. Webhook без правильного secret → 403. Апдейт другого чата → 200 ignored и не
   создаёт history/user/dedup ключей.
6. Обычное сообщение разрешённой группы создаёт user-index и одну запись history;
   после 35 сообщений остаются ровно последние 30.
7. Правка существующего сообщения меняет его текст/`edit_ts`, но размер истории
   не растёт и старый текст исчезает. Edit в `/ping`/`/help` не запускает command
   response.
8. `/ping`, `/ping@our_bot` → `pong`; `/ping@OtherBot` не получает ответа.
9. Повтор одного `update_id` не дублирует историю и не даёт второй ответ.
10. Искусственный сбой между user/history/dedup можно безопасно повторить; после
    восстановления данные полны и единственны.
11. Live smoke: `vercel deploy`, `setWebhook`, обычная фраза в группе видна в
    Upstash, `/ping` отвечает, `getWebhookInfo` не показывает ошибку доставки.

## Вне объёма

- QStash, NVIDIA NIM и LLM-ответы — Ticket 02.
- Правила, триггеры и тон — Ticket 03.
- `/judge` — Ticket 04.
- Админка — Ticket 05.

## Риски

- Webhook-body reply нельзя сделать строго exactly-once при обрыве соединения
  после server-side commit; для `/ping`/`/help` это допустимый MVP-компромисс.
  Все важные LLM side effects с Ticket 02 используют durable job state.
- Никогда не добавлять историю в публичный health/debug endpoint.
- Реальный ключ NVIDIA, однажды помещённый в шаблон, рекомендуется перевыпустить,
  даже если текущий tracked `.env.example` уже очищен.
