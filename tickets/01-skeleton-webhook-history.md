# TICKET-01 — Reliable Foundation, Telegram Webhook, and Canonical Chat History

**Size:** L · **Depends on:** — · **Unblocks:** 02
**Shared names, keys, and environment contract:** see 00-ARCHITECTURE.md

**Status:** Local implementation and automated checks are complete. Live acceptance
is not complete until it passes a real Vercel, Telegram, and Upstash smoke test.

## Goal

Deliver a Vercel-ready foundation that accepts Telegram updates only from one
allowed private group, reliably stores the latest 30 messages and observed users
in Upstash Redis, and replies synchronously to /ping and /help. A repeated
delivery or a temporary Redis failure must not duplicate history or permanently
lose an update.

LLM and QStash work are deliberately out of scope for this ticket.

## Fixed decisions

- Use the Vercel FastAPI preset: one api/index.py entry point exporting app,
  Python 3.12, and root requirements files.
- The MVP serves exactly one TELEGRAM_ALLOWED_CHAT_ID. On Vercel, missing an
  allowlist or persistent Redis is a readiness failure, never an in-process
  memory fallback.
- Disable Telegram Privacy Mode in BotFather, or make the bot a group admin.
  After disabling privacy for a bot already in the group, remove and re-add it.
  Without message visibility, complete history is impossible.
- dedup:update:<update_id> means “the incoming update was successfully
  persisted,” not “processing started.” Its TTL is 24 hours.
- Ingestion order is: readiness/secret/chat gate; fast final-dedup lookup;
  idempotent user/history writes; atomic final dedup mark; only the race winner
  returns a service-command reply.
- History is upserted by (chat_id, message_id). An edited_message replaces its
  original record instead of using another buffer position. Redis upsert and trim
  are atomic and MemoryKV has equivalent semantics.
- In addition to the 30-record limit, reads and writes remove records older than
  HISTORY_RETENTION_SECONDS using Telegram ts. The list key uses the same
  sliding TTL, defaulting to 30 days. Reads physically prune expired records.
- Use Telegram message.date and edited_message.edit_date, never server time, as
  the canonical history timestamps. Store minimal reply context too.
- The observed-user directory is updated for each incoming message. A future
  admin UI can resolve an @username only after the bot has observed it; numeric
  Telegram ID remains canonical.
- The synchronous Upstash SDK runs in FastAPI's thread pool and must not block the
  async event loop.

## Completed scope

- [x] Repository structure and app package follow 00-ARCHITECTURE.md.
- [x] requirements.txt, requirements-dev.txt, .python-version, and vercel.json
  use reproducible compatible versions.
- [x] .env.example has blank secrets, safe non-secret defaults, a valid typed
  template, and TELEGRAM_ALLOWED_CHAT_ID.
- [x] app/settings.py provides typed Settings, ignores blank template values, and
  validates production readiness separately.
- [x] app/store/redis.py provides lazy Upstash Redis REST and thread-safe
  MemoryKV adapters, ping, standard KV operations, and atomic history upsert.
- [x] app/store/history.py provides upsert, a 30-record trim, retention pruning
  and sliding TTL, newest-first reads, and corrupt-record tolerance.
- [x] app/store/users.py atomically maintains user:<id> and
  username:<normalized>, removes old aliases safely, prevents a stale retry from
  rolling back a newer profile, and resolves observed users only.
- [x] app/store/dedup.py provides already_seen and final mark_seen with a
  24-hour TTL.
- [x] app/telegram/models.py safely parses message and edited_message, Telegram
  timestamps, captions and entities, reply metadata, and command suffixes.
- [x] app/telegram/client.py validates Bot API results and supports direct
  webhook replies for /ping and /help without leaking token-bearing URLs.
- [x] app/telegram/webhook.py applies production readiness, a constant-time
  secret check, chat gating, idempotent user/history writes, final deduplication,
  and the service commands.
- [x] api/index.py and /api/health check Redis PING and required production
  configuration without revealing secret values.
- [x] scripts/set_webhook.py configures set/info/delete, message and
  edited_message updates, max_connections=1, safe secret/HTTPS validation, and
  explicit-only destructive pending-update drops.
- [x] scripts/check_telegram.py verifies token, username, Privacy Mode, and
  allowed-group access. scripts/check_redis.py performs a real Upstash
  round-trip including production Lua history upsert/edit/prune and
  observed-profile/username-alias transitions.
- [x] README includes local setup, Vercel deployment, environment, Privacy Mode,
  allowed-chat, smoke scripts, and webhook registration instructions.
- [x] MemoryKV/webhook tests, mocked Upstash adapter contracts, lint, and CI are
  included.

## Canonical history record

~~~json
{
  "message_id": 123,
  "source_update_id": 987,
  "user_id": 42,
  "username": "alice",
  "name": "Alice",
  "text": "message text",
  "ts": 1784200000,
  "edit_ts": null,
  "is_edited": false,
  "is_bot": false,
  "reply_to": {
    "message_id": 120,
    "user_id": 7,
    "is_bot": true,
    "text": "quoted message text"
  }
}
~~~

source_update_id is the Telegram update that produced this record version. It
orders a delayed original update safely after an edit. reply_to may be null.
Ticket 02 will also store final outbound bot messages only after a successful
Bot API call yields a real Telegram message_id.

Main and reply text are limited to 4,096 Unicode characters before
serialization. A new record without integer message_id and ts is rejected.
Corrupt JSON or legacy records with an invalid ts never reach a reader and are
physically removed by read-prune.

## Automated acceptance

1. pytest and lint pass, including a failure-path test in which history fails
   before final dedup and a retry completes safely.
2. Settings loaded from .env.example do not raise ValidationError; DeepSeek model
   and QStash URL defaults are correct.
3. On Vercel, missing Redis or TELEGRAM_ALLOWED_CHAT_ID makes health and webhook
   return 503. With valid configuration, /api/health reports ready and real Redis
   responds to PING.
4. check_telegram.py verifies token/username, group or supergroup access, and
   ordinary-message visibility through disabled Privacy Mode or bot-admin status.
5. A bad webhook secret returns 403. An update from another chat returns ignored
   and creates no history, user, or dedup keys.
6. An ordinary allowed-group message produces one user index and one history
   record; 35 messages leave exactly the newest 30.
7. Editing a message replaces its text and edit_ts without increasing history
   size. An edit that becomes /ping or /help never triggers a command reply.
8. /ping and /ping@our_bot return pong; /ping@OtherBot is ignored.
9. A repeated update_id neither duplicates history nor sends a second response.
10. A synthetic failure between user/history/dedup can be retried until data is
    complete and singular.

## Live acceptance still required

Deploy to Vercel, register the webhook, send an ordinary group message, verify it
in Upstash, run /ping, and confirm getWebhookInfo has no delivery errors.

## Out of scope

- QStash, NVIDIA NIM, and LLM replies: Ticket 02.
- Rules, automatic triggers, and tone: Ticket 03.
- /judge and /deep: Ticket 04.
- Web admin panel: Ticket 05.

## Risks

- A webhook-body reply cannot be made strictly exactly-once if the connection
  fails after server-side commit. That is an accepted MVP compromise for /ping
  and /help. Ticket 02 uses durable jobs for meaningful LLM side effects.
- Never expose history in public health or debug endpoints.
- Rotate any NVIDIA key that was ever committed to an older environment template.
