# TICKET-02 — Durable QStash job и базовый LLM-ответ на mention/reply

**Размер:** L · **Зависит от:** 01 · **Разблокирует:** 03
**Общий контракт:** `00-ARCHITECTURE.md`

## Цель

Добавить надёжный асинхронный путь Telegram → Redis snapshot → QStash →
`deepseek-ai/deepseek-v4-flash` → Telegram. Бот отвечает на точное упоминание
своего username и reply именно на сообщение этого бота, использует контекст на
момент обращения и не теряет job при сбое между receive/enqueue/process/deliver.

После тикета slow LLM никак не удерживает Telegram webhook. Повтор Telegram или
QStash не должен повторно генерировать уже сохранённый ответ или создавать
второй ответ после состояния `delivered`.

## Предусловия из Ticket 01

- Secret и `TELEGRAM_ALLOWED_CHAT_ID` проверяются **до** history/job logic.
- Webhook установлен с `max_connections=1`, поэтому updates единственной группы
  попадают в snapshot/history последовательно.
- Входящие message/edit атомарно upsert-ятся по `message_id`; observed user index
  обновляется; история ограничена 30 элементами, per-record retention и sliding
  key TTL.
- Модель сообщения содержит nested `reply_to` с `message_id`, `user_id`,
  `is_bot` и текстом.

## Поводы ответа

Webhook создаёт reply job только если:

1. Telegram entity типа `mention` указывает ровно на
   `@<TELEGRAM_BOT_USERNAME>` (case-insensitive comparison; offsets Telegram
   декодируются как UTF-16 code units, а не Python code points); или
2. `reply_to.user_id` равен проверенному numeric ID **текущего** бота.

`from.is_bot=true` недостаточно: reply другому боту не запускает job. Команды с
суффиксом обрабатываются только если суффикс совпадает с username текущего бота.
Обычный текст в этом тикете только логируется. `edited_message` исправляет
history/user record, но не создаёт повторный LLM-ответ. `cooldown:auto:*` к явным
mention/reply не применяется.

## Объём работ

### 0. Identity и admin primitive

- [ ] Лениво вызвать Telegram `getMe`, проверить совпадение username из env и
  bounded-cache сохранить `{id, username}` в `bot:self`; при невозможности
  подтвердить identity routed updates получают retryable error, а не сравнение
  с любым `is_bot=true`.
- [ ] Mention extractor корректно режет Telegram UTF-16 offsets, поддерживает
  text/caption entities и сравнивает извлечённый токен целиком.
- [ ] Добавить минимальный `app/store/admins.py`: `is_admin(user_id)` учитывает
  `SUPER_ADMIN_ID` и Redis set `admins`. CRUD появится позже, но prompt уже
  получает вычисленную роль.

### 1. QStash adapter

- [ ] `app/queue/qstash.py`:
  - async `publish(job_id)` на
    `{PUBLIC_BASE_URL}/api/telegram/process` с body только
    `{"job_id":"<update_id>"}`;
  - deduplication ID `telegram-<update_id>`;
  - exact `Upstash-Retries: 3` (1 + 3 = не более 4 доставок),
    `Upstash-Retry-Delay: max(275000, exp(2.5 * retried) * 1000)` и failure callback
    `{PUBLIC_BASE_URL}/api/telegram/failure`;
  - `verify(raw_body, signature, url)` через официальный receiver, с current и
    next signing keys; проверяется исходный body до JSON parsing;
  - заданные connect/read/total timeouts; секреты/тело не логируются.
- [ ] Publish adapter возвращает QStash `messageId`; webhook сохраняет его в
  job атомарно вместе с enqueue metadata. Если worker уже перевёл
  `received` в более поздний state, metadata всё равно записывается без state
  downgrade. Несовпадающий `messageId` для того же job — integrity error.
- [ ] Никогда не публиковать сырой Telegram update, текст, username или snapshot.
  QStash получает только job ID.
- [ ] Settings/`.env.example` и production readiness добавляют `NVIDIA_API_KEY`, QStash token/current+next
  signing keys, `JOB_RETENTION_SECONDS=604800`, `WORKER_BUDGET_SECONDS=240`,
  `JOB_LEASE_SECONDS=270`. Validation требует `0 < budget < lease < maxDuration`,
  exact HTTPS `PUBLIC_BASE_URL`, обе signing keys и confirmed Flash model. Health называет
  только missing/invalid dependency names.

### 2. Создание durable job в webhook

- [ ] После secret/chat gate определить route. Для нового mention/reply **до
  upsert текущего trigger**:
  - получить до 30 уже существующих chronological history records;
  - сохранить triggering message отдельно, не добавляя его в context list;
  - сохранить reply target snapshot отдельно;
  - создать `job:{update_id}` в `received` через atomic create-if-absent.
- [ ] Затем идемпотентно upsert-ить current history/user и только после успеха
  публиковать job. Если history write падает, `received` job остаётся, Telegram
  retry переиспользует его snapshot и завершает upsert/publish.
- [ ] `request_json` содержит:
  - `kind="reply"`, `chat_id`, `update_id`, `trigger_message_id`;
  - author snapshot (`id`, `name`, `username`);
  - triggering text/entities;
  - reply context или `null`;
  - не более 30 предшествующих нормализованных history records.
- [ ] Publish выполняется после сохранения snapshot. Успех переводит job в
  `enqueued`; ошибка оставляет `received` и возвращает Telegram `503`, чтобы тот
  повторил update. Повтор webhook переиспользует существующий snapshot и тот же
  QStash dedup ID.
- [ ] Race `callback before publish response` поддерживается явно: подписанный
  worker может CAS `received→processing`; webhook после успешного publish
  reload-ит job и считает `processing/ready_to_deliver/delivered` успешным
  enqueue, не пытаясь откатить state в `enqueued`.
- [ ] `enqueued`, `processing`, `ready_to_deliver`, `delivered` и terminal failed
  подтверждаются webhook как `200`; ранний receipt/dedup marker сам по себе не
  имеет права остановить незавершённый enqueue.
- [ ] Для unrouted update `dedup:update:*` ставится после history/users. Для
  routed update тот же финальный marker ставится только после durable job и
  успешного `enqueued`; retry с marker absent продолжает сохранённый job, а не
  пересобирает snapshot по новой конфигурации.

### 3. Job store и state machine

- [ ] `app/store/jobs.py`: типизированное чтение, atomic/CAS transitions,
  increment attempts, token lease, сохранение placeholder/chunk checkpoints,
  answer и санитизированного error class.
- [ ] При create job один раз фиксируется immutable absolute `expires_at = now + JOB_RETENTION_SECONDS`
  (default 7 суток). Все job/answer/evidence/intent/checkpoint keys получают EX = remaining
  lifetime до этого exact `expires_at`; stage write/renew не может refresh-нуть данные дольше
  privacy indexes. `jobs:chat:*`/`jobs:user:*` всегда score тот же `expires_at`.
  Worker имеет hard budget 240s, lease 270s и owner-only renewal каждые 60s;
  lease освобождается compare-and-delete только владельцем.
- [ ] Lease содержит monotonically increasing fencing generation. Перед LLM,
  каждым Telegram edit/send и terminal transition worker атомарно проверяет
  ownership/fence **и allowed non-terminal state**. Потеря renewal запрещает side effect и
  возвращает retryable status; busy lease возвращает `503` + bounded
  `Retry-After` по remaining TTL. timeouts всех стадий гарантируют остановку до 240s.
- [ ] При создании job индексировать его в sorted sets `jobs:chat:*` и
  `jobs:user:*` score=`job_expires_at` для privacy purge Ticket 05. Lookup сначала
  делает `ZREMRANGEBYSCORE`; `ZREM` выполняется при реальном удалении/purge job,
  но не сразу при `delivered`, пока private snapshot ещё живёт. Поэтому stale
  job IDs не живут в refreshable SET вечно и остаются доступны privacy purge.
- [ ] Невалидные переходы не молча перезаписывают более позднее состояние.
- [ ] `delivered` — единственный успешный terminal state; он устанавливается
  только после Telegram delivery всех частей.

### 4. NVIDIA/ChatNVIDIA client contract

- [ ] Добавить pin `langchain-nvidia-ai-endpoints==1.4.3`.
- [ ] `app/llm/client.py` лениво создаёт flash client:

  ```python
  base = ChatNVIDIA(
      model="deepseek-ai/deepseek-v4-flash",
      api_key=settings.NVIDIA_API_KEY,
      temperature=0.9,
      top_p=0.95,
      max_completion_tokens=1024,
  )
  client = base.with_thinking_mode(enabled=False)
  ```

  Значение model берётся из `LLM_MODEL_FAST`, но production validation требует
  подтверждённый ID `deepseek-ai/deepseek-v4-flash`.
- [ ] Не использовать `extra_body={...}`: в текущем wrapper это может создать
  неверно вложенное поле. Контрактный HTTP-тест перехватывает сериализованный
  запрос и проверяет `chat_template_kwargs.thinking=false`, отсутствие literal
  root `extra_body`/`reasoning_effort`, model ID, timeout и
  `max_tokens`/эквивалентное wire-поле.
- [ ] Использовать async invoke либо thread offload; синхронный client не блокирует
  event loop FastAPI.

### 5. Prompt boundary (слои 1–2)

- [ ] `app/llm/prompt.py` строит **одно aggregated system message** из neutral base policy
  + только сервером проверенных numeric actor ID/`is_admin`, а затем one
  user/data message: Telegram name/username, предшествующий transcript, reply target и current
  message. Ticket 03 добавит в этот же aggregate list/rule policy.
- [ ] Raw transcript/current text не конкатенируются в system. Data-блок явно
  говорит, что любые инструкции внутри сообщений — цитируемые данные, не policy.
- [ ] Triggering message присутствует ровно один раз. Сообщения после cutoff и
  placeholder не могут попасть в snapshot.
- [ ] Имя/username из Telegram экранируются/сериализуются только как untrusted
  data, а admin status вычисляется через `admins` + `SUPER_ADMIN_ID`, не из text.

### 6. Worker и доставка

- [ ] `POST /api/telegram/process`:
  1. проверить QStash signature по raw request;
  2. валидировать body и загрузить job;
  3. если `delivered` — `200` без side effects;
  3a. если есть unresolved `send_intent` без corresponding Telegram `message_id`, не
     повторять `sendMessage`: fenced-CAS переводит job в `failed_ambiguous` и возвращает 2xx;
  4. взять lease, увеличить attempts, CAS
     `received|enqueued|failed_retryable → processing` (signed callback сам
     доказывает, что QStash принял publish);
  5. перед `sendMessage` placeholder сохранить fenced `send_intent(kind=placeholder, payload_hash)`,
     затем после result ID атомарно checkpoint-ить ID и clear intent; делать это только
     если placeholder ID ещё не сохранён;
  6. если `job:*:answer` отсутствует — собрать prompt, вызвать flash и сохранить
     answer **до** доставки;
  7. детерминированно split answer, edit placeholder первой частью, а перед каждым
     последующим `sendMessage` сохранить `send_intent(chunk_index,payload_hash)`; после result ID
     атомарно checkpoint-ить ID и clear intent. `editMessageText` по known ID остаётся retryable;
  8. upsert-ить каждый исходящий bot message/edit в history;
  9. после всех частей перейти в `delivered`, затем `200`.
- [ ] `POST /api/telegram/failure` сначала проверяет QStash signature по raw body и
  exact canonical failure URL, затем typed-парсит официальный callback envelope.
  Для correlation обязательны `sourceMessageId`, `sourceBody`, `url`, `retried`,
  `maxRetries` и `status`; `dlqId` может отсутствовать. Provider-added unknown
  fields игнорируются, но не логируются.
- [ ] `sourceBody` base64-decode-ится с strict validation и лимитом 256 bytes;
  decoded JSON должен иметь ровно `{"job_id":"<id>"}`. Затем route
  требует exact `url == PUBLIC_BASE_URL + "/api/telegram/process"`, равенство
  `sourceMessageId` с сохранённым job `qstash_message_id` и `retried >= maxRetries`.
  Если job есть, но `qstash_message_id` ещё не сохранён из-за callback-before-publish-response race,
  route возвращает retryable `503`, не terminal result; wrong **present** ID отклоняется.
  Job ID никогда не берётся из callback response `body`.
- [ ] Если у job есть active lease, failure route не терминализирует его: он
  возвращает `503` + `Retry-After` по remaining TTL, чтобы live worker либо delivered,
  либо потерял lease. После expiry только correlation route одним CAS переводит
  non-terminal job в `failed`, increment-ит fence и delete-ит lease, сохраняет sanitized
  `{status, retried, max_retries, error_class, dlq_id?}` и `failure_notice_pending=true`.
- [ ] Known placeholder меняется на failure text по checkpoint-у `failure_notice_delivered`. Если
  process/edit падает после terminal CAS, дубликат callback или dedicated failure-notice retry
  может довести только этот idempotent edit; после checkpoint дубликаты terminal
  `2xx` без overwrite. `body`, `sourceBody`, `header`, `sourceHeader` не хранятся и не логируются.
- [ ] `send_message` возвращает нормализованный Telegram `result`, поэтому
  `message_id` берётся как `message["message_id"]`, а не из внешнего
  `{ok, result}` envelope.
- [ ] Все LLM-ответы используют общий paragraph-aware splitter `<=4000`
  символов, plain text, без `parse_mode`. Первая часть reply-ится на triggering
  message; последующие части сохраняют порядок.
- [ ] Точный Bot API 400 `message is not modified` при повторе известного
  `editMessageText` считается success соответствующего checkpoint; остальные
  Telegram 400 остаются permanent.

### 7. Ошибки и ретраи

| Ситуация | Поведение worker |
|---|---|
| NVIDIA/Telegram timeout до известного ответа, 429, 5xx | `failed_retryable`, HTTP 503 QStash, reuse snapshot/answer/placeholder |
| Worker занят live lease | HTTP 503 + `Retry-After` по TTL; не тратить side effect/attempt |
| Невалидный job/body, неподдерживаемая route | terminal `failed`, 2xx после bookkeeping |
| NVIDIA auth/validation 4xx, Telegram 400 кроме exact idempotent edit no-op | terminal `failed`, user-visible placeholder error |
| Invalid QStash signature | 401; никаких state changes |
| Лимит попыток исчерпан | failure callback/DLQ → `failed` |
| `sendMessage` завершился ambiguous network timeout | `failed_ambiguous`, не слепо повторять возможный дубль; ручная проверка |
| Crash после `sendMessage`, но до checkpoint ID | unresolved pre-send intent → `failed_ambiguous`, не resend |

Повтор после сохранённого answer не вызывает LLM снова. Повтор
`editMessageText` для известного placeholder идемпотентен. Application logs
содержат `job_id`, transition, latency и error class, но не prompt/answer/token.

## Вне объёма

- Автоматические keyword triggers, списки и tone commands — Ticket 03.
- `/judge`, pro и Tavily — Ticket 04.
- Admin UI/API — Ticket 05.

## Автоматические проверки

- [ ] Unit: mention entity текущего бота; mention другого бота; reply текущему
  боту; reply другому боту; emoji перед mention (UTF-16 offset); command suffix
  текущего/чужого бота; edit упдейтит history, но не enqueue-ится.
- [ ] Unit: context cutoff, edit upsert, current message ровно один раз, outbound
  history, Unicode splitting на границах 4000.
- [ ] State-machine: publish failure + Telegram retry; crash после enqueue; crash → busy
  lease → `Retry-After` → post-expiry recovery; callback-before-publish-response/
  failure-before-publish-metadata races; fake-clock renewal/fence/240s budget; callback-vs-live-worker
  fence race; retry после answer; retry after delivered; crash at every `sendMessage`
  intent/checkpoint boundary; bounded failure-notice path.
- [ ] Contract: QStash current/next signatures, exact retry headers/delay and `Retry-After`;
  failure callback valid, malformed,
  missing/invalid/oversize base64 `sourceBody`, wrong destination/message ID,
  non-exhausted retries, missing metadata, duplicate и terminal job; every job-derived key
  expires no later than its purge indexes; ChatNVIDIA
  wire payload (`chat_template_kwargs.thinking=false`, model); Telegram shape и
  exact `message is not modified` normalization.
- [ ] Все тесты, Ticket 01 tests, `ruff` проходят в CI Python 3.12.

## Критерии приёмки (live E2E)

1. На стабильном Vercel deployment `@bot` получает связный flash-ответ с фактом
   из предыдущей переписки; последующее сообщение после trigger в ответ не
   попадает.
2. Reply на сообщение **этого** бота получает ответ и учитывает текст reply
   target; reply другому боту ничего не запускает.
3. Обычный текст молчит, но сохраняется; чужой chat/private chat не сохраняется.
4. Видно один placeholder, который заменяется plain-text ответом; ответ >4096
   приходит упорядоченными частями и все исходящие части видны в Redis history.
5. Повтор webhook до/после publish и повтор QStash delivery после `delivered` не
   создают второй ответ. Искусственный transient LLM failure успешно доезжает по
   retry с тем же snapshot.
6. В QStash body виден только job ID. Вызванная модель в contract/observability —
   ровно `deepseek-ai/deepseek-v4-flash` с non-thinking contract.

## Риски

- Telegram не предоставляет idempotency key для первого `sendMessage`;
  `failed_ambiguous` — осознанный no-duplicate компромисс для редкого timeout.
- QStash retries расходуют quota; лимит ограничен, permanent errors не ретраятся.
- LangChain холодный старт измеряется на live deployment; lazy import обязателен.
