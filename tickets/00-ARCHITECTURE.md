# 00 — Architecture and Shared Project Contract

> This is the master contract. Tickets 01–05 use its route names, Redis keys,
> environment variables, and invariants. When documents disagree, this file
> wins. The product requirements are in ../GOAL_DESCRIPTION.md.

The product is a Telegram bot for **one private group chat** of roughly 10–15
people, with a web UI, dynamic LLM behaviour, and a recent-message buffer.

## 1. Fixed decisions

| Area | Decision |
|---|---|
| Hosting | Vercel Hobby, Python 3.12, one FastAPI application |
| Telegram | Webhook; Privacy Mode disabled or the bot is a group admin. Ticket 05 requires bot group-admin status for reliable getChatMember use. |
| Access boundary | Exactly one TELEGRAM_ALLOWED_CHAT_ID. Other groups and private chats receive 200 but are not logged or processed. |
| Primary LLM | One `LLM_MODEL` for every response; deployment default is NVIDIA NIM google/gemma-4-31b-it |
| Thinking routes | Public /think, /google, and scheduled banter; all other routes are non-thinking |
| LLM wrapper | langchain-nvidia-ai-endpoints==1.4.3 and asynchronous ChatNVIDIA with a request-shape contract for Gemma thinking on/off |
| Queue | Upstash QStash. Webhook writes a Redis job/snapshot and publishes only an opaque job_id. |
| Storage | Upstash Redis REST |
| Fact search | One bounded Tavily basic search for each explicit /google request |
| Web auth | Telegram OIDC Authorization Code Flow with PKCE/state, then a short owner-only server-side session |
| Frontend | Static HTML/CSS/vanilla JavaScript in public/, no runtime build |

Critical invariants:

1. The webhook never writes final completion before a job is successfully queued.
   A Telegram retry resumes incomplete work instead of losing it.
2. QStash deduplication protects publication; Redis job state protects
   processing; delivered is written only after Telegram delivery succeeds.
3. History upserts by (chat_id, message_id); an edit replaces the original record.
   Successful outbound bot messages are history records too.
4. Register Telegram with max_connections=1. For a routed update, snapshot up
   to 30 history records **before** upserting the trigger and before publishing.
   The trigger is stored separately, so later messages cannot change a queued
   answer.
5. Raw chat, names, and user instructions never enter a system role. They are
   untrusted user/data content.
6. Send all LLM output as plain text and split it into chunks no longer than
   4,000 UTF-16 code units, leaving room below Telegram's 4,096-unit limit.
7. Direct human responses are prefixed outside the model with the immutable
   snapshot of the triggering Telegram account's current first_name.
8. The checked-in super-context cannot be replaced by Redis or an admin API.
   Dynamic tone/list/rule modifiers are explicitly subordinate to it.

~~~json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "functions": { "api/index.py": { "maxDuration": 300 } }
}
~~~

## 2. Repository structure

~~~text
/
├── api/index.py                    # FastAPI entry point; exports app
├── app/
│   ├── settings.py
│   ├── telegram/
│   │   ├── client.py               # Bot API client and plain-text splitter
│   │   ├── models.py
│   │   ├── webhook.py              # secret/chat gate, history, route, enqueue
│   │   └── processor.py            # QStash callback, job machine, LLM, delivery
│   ├── llm/
│   │   └── client.py               # One Gemma ChatNVIDIA factory
│   ├── queue/
│   │   └── qstash.py
│   ├── search/
│   │   └── tavily.py
│   ├── store/
│   │   ├── redis.py
│   │   ├── history.py
│   │   ├── users.py
│   │   ├── dedup.py
│   │   ├── jobs.py
│   │   ├── lists.py
│   │   ├── rules.py
│   │   ├── config_store.py
│   │   └── admins.py
│   ├── auth/
│   │   ├── telegram_oidc.py
│   │   └── session.py
│   └── admin/
│       └── routes.py
├── public/
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── scripts/
├── tests/
├── tickets/
├── .env.example
├── requirements.txt
└── vercel.json
~~~

## 3. Routes and Redis schema

### HTTP routes

| Method | Route | Contract | Ticket |
|---|---|---|---|
| POST | /api/telegram/webhook | Telegram secret and allowed-chat gate, history/routing/enqueue | 01→03 |
| POST | /api/telegram/process | Signed QStash callback with body containing job_id | 02 |
| POST | /api/telegram/failure | Signed QStash failure callback and terminal/DLQ bookkeeping | 02 |
| POST | /api/cron/banter | CRON_SECRET-authenticated twenty-minute scheduled banter enqueue | 08 |
| GET | /api/public/config | Safe bot username and OIDC client ID only | 05 |
| GET | /api/auth/telegram/start | Create OIDC state, nonce, PKCE, then redirect | 05 |
| GET | /api/auth/telegram/callback | Validate callback and issue session | 05 |
| POST | /api/auth/logout | Same-origin and CSRF protected logout | 05 |
| GET | /api/admin/me | Current role and CSRF token | 05 |
| CRUD | /api/admin/admins | Legacy assigned-role records; owner access only | 05→06 |
| GET/DELETE | /api/admin/users | Observed-user lookup and deletion | 05 |
| CRUD | /api/admin/lists | List metadata and membership | 05 |
| CRUD | /api/admin/rules | Text rules | 05 |
| GET/PUT/DELETE | /api/admin/tone | Fixed-preset global/chat tone override | 05→06 |
| GET/DELETE | /api/admin/logs | Allowed-chat history read or purge | 05 |

### Redis keys

| Key | Type | Meaning |
|---|---|---|
| hist:<chat_id> | list, maximum 30 | JSON history records; atomic versioned upsert/prune by message_id and ts; newest first |
| user:<user_id> | JSON string | id, username, bounded Telegram first_name in name, is_bot, last_seen_at, last_update_id; profile and alias update atomically |
| username:<normalized> | string | Globally versioned current user_id; stale owners cannot reclaim an alias |
| bot:self | bounded JSON cache | Verified getMe numeric ID, username, and Telegram first_name |
| dedup:update:<update_id> | string, 24h | Final receipt: history/users complete; routed work reached at least enqueued |
| cmd:<update_id> | JSON string, 30d | Durable tone/mode command outcome, atomically paired with configuration mutation |
| job:<update_id> | hash, absolute expires_at | State, attempts, immutable request JSON, timestamps, QStash/placeholder IDs, error |
| jobs:chat:<chat_id> | zset job_id→expires_at | Job IDs for privacy purge; expired members are pruned by score |
| jobs:user:<user_id> | zset job_id→expires_at | Job IDs that contain a user's data |
| cfg:global | JSON string | Fallback tone_preset |
| cfg:<chat_id> | JSON string | Chat tone_preset override, only for the allowed chat |
| admins | set | Numeric Telegram IDs; SUPER_ADMIN_ID is never removable |
| adminver:<user_id> | integer | Role/session version, incremented on role change |
| auth:state:<hash> | JSON string, 10m | One-time OIDC state, nonce, verifier, browser-binding hash, exact redirect URI |
| session:<jti> | JSON string, ≤8h | Server-side session record |
| member:<chat_id>:<user_id> | JSON string, ≤5m | Positive assigned-admin group-membership cache |
| auth:rate:<purpose>:<hash> | integer, 60s | Privacy-preserving per-address OIDC route rate limit |
| privacy:job:<job_id> | string, ≤job TTL | Deletion tombstone that atomically blocks late history writes |
| privacy:receipt:<index_hash> | set, ≤job retention | Retry-safe outbound message IDs retained until privacy cleanup succeeds |
| lists:index | set | List slugs |
| list:<slug>:meta | JSON string | Deterministic list metadata |
| list:<slug>:members | set | Numeric user IDs |
| rules:index | set | Rule IDs |
| rule:<id> | JSON string | Deterministic rule |
| cooldown:auto:<chat_id> | string, EX | Atomic auto-route blocker and owner token |
| scheduled:slot:<chat_id>:<slot> | string, EX | Idempotency marker for one scheduled banter slot |
| memory:epoch:<chat_id> | integer string | Mutable-memory generation boundary used by lobotomy |
| memory:gathered<user_id> | JSON string | Bounded sender-scoped gathered shard: Telegram messages, fallible observations, and image OCR/description |
| memory:gathered:tombstone:<chat_id>:<user_id> | integer string, ≤30d | Per-user privacy-deletion cutoff that rejects queued pre-deletion memory writes |
| lobotomy:members:<chat_id> | set | Super-admin-invited active members allowed to run `/lobotomy` |

## 4. Canonical data model

### History record

~~~json
{
  "message_id": 123,
  "source_update_id": 987,
  "user_id": 456,
  "username": "user",
  "name": "Telegram first_name",
  "text": "message text",
  "ts": 1780000000,
  "edit_ts": null,
  "is_edited": false,
  "is_bot": false,
  "reply_to": {
    "message_id": 120,
    "user_id": 999,
    "is_bot": true,
    "text": "quoted text"
  }
}
~~~

### Gathered-shard entry

Runtime gathered memory is a bounded JSON list in the per-user Redis key above;
it is not written to the ephemeral Vercel filesystem. Every eligible human
message is keyed by the Telegram sender ID (without chat ID because this
deployment has one configured chat) and records the current Telegram
`first_name`, source message ID, timestamp, text, provenance, and confidence.
Telegram photos and image documents additionally carry bounded media metadata.
The worker may attach a short, fallible OCR/description result generated by the
configured multimodal model. The file ID is used only for the ephemeral download
and is never stored in the gathered entry. Edits replace the sender's prior
message entry, and `/lobotomy` clears all gathered keys.

- ts comes from Telegram message.date and edit_ts from edit_date. Server time is
  only diagnostic received_at job data.
- reply_to is null when there is no reply. Bound text before serialization.
- Replace a record with the same message_id only when the version tuple
  (edit_ts or ts, is_edited, source_update_id) is not older. A delayed original
  cannot roll an edit back. Insert unknown records in Telegram order
  (ts, message_id), then retain the newest 30. Editing a long-evicted message
  cannot reinsert it ahead of newer history.
- Every write prunes records whose Telegram ts is older than retention and
  refreshes the list TTL. Reads apply the cutoff again and physically rewrite the
  list without extending its remaining TTL.
- The per-record cutoff governs usable context; it is not a per-element Redis
  TTL. Expired records are never returned and are removed on the next read/write.
  With no activity, the whole list expires after HISTORY_RETENTION_SECONDS.
- Put outbound sendMessage/editMessageText records in history only after a
  successful Bot API response gives a real message_id. Webhook-body replies such
  as /ping, /help, and tone-command replies have no real ID and are not stored.
- On each incoming message, update the observed user. Delete an old username
  alias only if it still points to that user.

### Lists, rules, and tone

A personal policy exists only through a list; there is no ambiguous
rule.kind="personal".

~~~json
{
  "slug": "aggressive",
  "title": "Sarcastic response",
  "enabled": true,
  "priority": 50,
  "applies_to": ["explicit", "auto"],
  "injected_prompt": "Use dry sarcasm without personal attacks."
}
~~~

~~~json
{
  "id": "nonsense",
  "enabled": true,
  "priority": 50,
  "scope": "all",
  "match": {"type": "substring", "value": "nonsense"},
  "instruction": "Explain calmly why the argument is not nonsense.",
  "stop_processing": false
}
~~~

Rule match types are substring, word, and phrase. Regular expressions are out of
scope. Normalize only for matching with Unicode NFKC, casefold, and whitespace
collapse; do not alter the source text sent to the LLM.

Rules sort by priority DESC, id ASC; lists sort by priority DESC, slug ASC.
Process equal-priority rules as one group in id order. If any rule in that group
has stop_processing=true, skip all lower-priority groups. A current rule scope
is auto, explicit, or all. The reserved ignore list suppresses only auto
replies; explicit mention/reply and public commands remain available. Cooldown
applies only to auto replies. Legacy judge scopes remain readable until old
records expire but are never selected by a current route.

Canonical tone slugs are neutral, serious, scientist, street, and
sarcastic_bot. The chat-command-only alias sarcastic and legacy stored
sarcastic_robot both map to sarcastic_bot. API and Redis never write aliases.

## 5. Retry-safe job state machine

Job ID is the Telegram update_id.

~~~text
received → enqueued → processing → ready_to_deliver → delivered
                ↘         ↘                 ↘
                  failed_retryable ──────────┘
                             ↘ (attempt limit or permanent error)
                               failed / failed_ambiguous
~~~

A privacy purge may transition any non-terminal state to cancelled.

| From | Allowed transitions |
|---|---|
| received | enqueued, processing, failed, cancelled |
| enqueued or failed_retryable | processing, failed, cancelled |
| processing | ready_to_deliver, failed_retryable, failed, failed_ambiguous, cancelled |
| ready_to_deliver | delivered, failed_retryable, failed, failed_ambiguous, cancelled |

Delivered, failed, failed_ambiguous, and cancelled are terminal. Reject unknown
transitions without overwriting a newer state.

1. After secret/chat gating, the webhook determines a route. For a routed update
   it snapshots up to 30 prior records, route, trigger, and reply context into
   job:<update_id> in received state; only then does it upsert current
   history/user. A retry reuses the immutable snapshot. An edited_message only
   repairs history and never makes a new LLM job.
2. Publish only {"job_id":"<update_id>"} to QStash with deduplication ID
   telegram-<update_id>, bounded retries, and a failure callback. On a successful
   response, atomically save QStash messageId and move to enqueued. On publish
   failure return 5xx to Telegram, so it retries the same snapshot and dedup ID.
   A signed worker can race ahead of the publish response and transition
   received→processing; the webhook records messageId without downgrading state.
3. Verify the QStash signature against the raw body and canonical public URL.
   Acquire a token-and-fence lease before work. The hard worker budget is 240s,
   initial lease is 270s, and only the owner renews every 60s. Check ownership,
   fence, and non-terminal state before every provider or Telegram side effect.
   A busy lease returns 503 with Retry-After equal to remaining TTL plus 1–5s
   jitter. Publish uses a minimum QStash retry delay of 275s so four deliveries
   cannot expire before lease recovery.
4. Before every non-idempotent sendMessage, write a fenced send intent with kind,
   chunk index, and payload hash. If a retry sees an intent without a message-ID
   checkpoint, set failed_ambiguous rather than send a possible duplicate.
   Create no more than one placeholder and persist its message ID. Save an LLM
   answer before Telegram delivery so retry does not generate it again.
5. Edit the placeholder with the first answer chunk, send later chunks separately,
   checkpoint each message ID, then mark delivered.
6. Provider timeout/429/5xx errors are retryable. Invalid payload, auth, most 4xx,
   and Telegram 400 are permanent, except exact “message is not modified” on a
   checkpointed intended edit. The failure callback derives job ID only from a
   bounded base64-decoded sourceBody, verifies sourceMessageId, destination URL,
   and exhausted retry counters. It never trusts a response body. It never logs
   secrets, transcript, callback body, or headers. It does not terminally fail
   a job with an active lease; after expiry, CAS marks failed, increments the
   fence, and deletes the lease.

The immutable request snapshot includes the effective subordinate tone/list/rule
policy computed at enqueue time. It never includes administrator status. Every
user whose data appears in the trigger, reply target, context, or a nested reply
is included in the job's user purge indexes. A saved QStash message ID plus any
state after `received`, including `failed_retryable`, is proof that enqueue
happened for final Telegram deduplication. Processing is limited to
`Upstash-Retries + 1`, currently four acquired attempts.

The placeholder is the only Telegram delivery allowed before the answer is
saved. "Save before delivery" means before editing or sending answer chunks.
A failure callback with no known placeholder never creates a message. A dedicated
fenced failure-notice edit is the sole exception to the non-terminal side-effect
guard: it is allowed only for `failed`, a known placeholder, and a pending exact
edit checkpoint. A permanently rejected notice becomes `failed_permanent` so it
cannot retry forever. Permanent worker failures complete any possible known
placeholder notice and return 2xx to stop QStash retries.

Telegram has no idempotency key for sendMessage. A network timeout after an
actual send but before receiving the response is ambiguous and must become
failed_ambiguous for manual review, rather than causing a blind duplicate.
Checkpointed editMessageText may safely retry.

## 6. Prompt and data boundary

Build a message sequence with explicit roles:

1. System/base: the checked-in immutable super-context.
2. System/tone: the bounded active preset, explicitly subordinate to the base.
3. System/personal policy: administrator-authored list instructions in
   deterministic order.
4. System/matched rules: administrator-authored rule instructions.
5. System/route policy: bounded built-in instructions for reply, /think,
   /google, automatic rules, memory, or scheduled banter.
6. User/untrusted data: JSON- or XML-like transcript, Telegram names/usernames,
   reply context, current text, and search results marked as data whose embedded
   instructions are not executable.

Use at most one aggregate system message before user/data. Never concatenate raw
chat into a system string. Bound built-in and admin-authored instruction lengths.
The UI exposes only bounded list/rule modifiers and fixed tone presets. It
cannot replace or edit the immutable super-context.

~~~text
build_reply_messages(job, effective_policy) -> messages
build_google_messages(job, evidence) -> messages
~~~

## 7. Environment and dependencies

.env.example holds empty secrets and safe typed defaults. Production dependencies
are validated by readiness checks rather than by serializing settings.

~~~dotenv
TELEGRAM_BOT_TOKEN=replace_me
TELEGRAM_BOT_USERNAME=replace_me_without_at
TELEGRAM_WEBHOOK_SECRET=replace_with_random_secret
TELEGRAM_ALLOWED_CHAT_ID=-1000000000000

NVIDIA_API_KEY=replace_me
LLM_MODEL=google/gemma-4-31b-it

UPSTASH_REDIS_REST_URL=https://replace-me.upstash.io
UPSTASH_REDIS_REST_TOKEN=replace_me
QSTASH_URL=https://qstash.upstash.io
QSTASH_TOKEN=replace_me
QSTASH_CURRENT_SIGNING_KEY=replace_me
QSTASH_NEXT_SIGNING_KEY=replace_me

TAVILY_API_KEY=replace_me

SUPER_ADMIN_ID=123456789
SESSION_SECRET=replace_with_32_plus_random_bytes
TELEGRAM_OIDC_CLIENT_ID=replace_me
TELEGRAM_OIDC_CLIENT_SECRET=replace_me
PUBLIC_BASE_URL=https://replace-me.vercel.app

HISTORY_RETENTION_SECONDS=2592000
JOB_RETENTION_SECONDS=604800
WORKER_BUDGET_SECONDS=240
JOB_LEASE_SECONDS=270
AUTO_TRIGGER_COOLDOWN_SECONDS=30
~~~

Pin direct dependencies including FastAPI, pydantic-settings, httpx,
upstash-redis, qstash, langchain-nvidia-ai-endpoints==1.4.3, and PyJWT[crypto].

The single Gemma factory calls with_thinking_mode(enabled=False) for ordinary
work and enabled=True only for /think and /google. In the pinned wrapper and
current NVIDIA model profile this maps to
chat_template_kwargs.enable_thinking=false/true. Contract tests capture both
outgoing request shapes. Do not send a literal root extra_body or an unverified
root reasoning_effort.

## 8. Privacy, operations, and quality

- Publish a group notice before production use. It must say that the bot holds up
  to 30 recent messages, excludes and removes records beyond the configured
  cutoff at the next access, lets the entire buffer expire after the same idle
  period, retains observed profiles/list membership until deletion, and retains
  durable model-job snapshots for seven days. It must identify the owner contact and
  purge path.
- Selected context goes to NVIDIA. Tavily is called only for `/google` and
  receives the bounded query the participant explicitly supplied in that
  command. It never receives recent group history automatically. QStash
  receives only job_id.
- The UI lets a super-admin purge history/jobs and delete an observed profile.
  It cancels non-terminal jobs, removes their snapshots/answers, and then clears
  history. Workers recheck cancellation before provider calls and delivery. A
  call already sent to an external provider cannot be recalled.
- Never log secrets or complete prompts/transcripts. Logs contain IDs, state,
  latency, and sanitised error classes only.
- CI runs Ruff and pytest on Python 3.12. Unit tests use fake Redis/HTTP;
  contract tests cover QStash signatures/state transitions, ChatNVIDIA payload,
  and Telegram splitting. Every ticket also requires a real stable-deployment
  E2E test.

## 9. Ticket order

~~~text
01 closed-chat ingestion, history, users
  → 02 durable QStash jobs and original replies
    → 03 deterministic rules, lists, tone, and auto routing
      → 04 historical judge workflow
        → 05 OIDC admin API/UI and privacy controls
          → 06 immutable super-context, Gemma, and public commands
~~~

The plan intentionally has few tickets. Each ends in automated checks and a live
Telegram/Vercel/Upstash end-to-end check.

## 10. Verified external contracts (2026-07-17)

- NVIDIA hosted model page: [Gemma 4 31B IT](https://build.nvidia.com/google/gemma-4-31b-it).
- LangChain: [ChatNVIDIA.with_thinking_mode](https://reference.langchain.com/python/langchain-nvidia-ai-endpoints/chat_models/ChatNVIDIA/with_thinking_mode). The wrapper version remains pinned by a request-shape contract test.
- Telegram: [Privacy Mode](https://core.telegram.org/bots/features#privacy-mode) and [OIDC Authorization Code + PKCE](https://core.telegram.org/bots/telegram-login).
- Vercel: [Function limits](https://vercel.com/docs/functions/limitations). Hobby Python functions currently have a 300-second maximum.
- Upstash: [QStash deduplication](https://upstash.com/docs/qstash/features/deduplication), [retries](https://upstash.com/docs/qstash/features/retry), and [callback payload](https://upstash.com/docs/qstash/features/callbacks).
- Tavily: [API credits](https://docs.tavily.com/documentation/api-credits). Basic search uses one credit and the free tier currently provides 1,000 per month.

These contracts are time-sensitive. Recheck the linked primary documentation
before implementing the corresponding future ticket.
