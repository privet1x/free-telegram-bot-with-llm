# TICKET-02 — Durable QStash Jobs and Basic Flash Replies for Mentions and Replies

**Size:** L · **Depends on:** 01 · **Unblocks:** 03
**Shared contract:** 00-ARCHITECTURE.md

## Goal

Add a reliable asynchronous path:

~~~text
Telegram → Redis snapshot → QStash → DeepSeek V4 Flash → Telegram
~~~

The bot replies to an exact mention of its username and to a reply to a message
sent by this bot. It uses context captured at trigger time and must not lose a
job across receive, enqueue, process, or delivery failures.

Slow LLM work must never hold a Telegram webhook open. A Telegram or QStash retry
must not regenerate a saved answer or create another answer after delivered.

## Preconditions supplied by Ticket 01

- Secret and TELEGRAM_ALLOWED_CHAT_ID are checked before history or job logic.
- The webhook is registered with max_connections=1, so updates for the one group
  arrive in context/history order.
- Incoming messages and edits upsert atomically by message_id. The user directory
  is updated. History is limited to 30 records and applies its record cutoff and
  sliding list TTL.
- The message model stores nested reply metadata: message_id, user_id, is_bot,
  and bounded text.

## Reply triggers

Create a reply job only if one of the following is true:

1. A Telegram mention entity resolves exactly to
   @<TELEGRAM_BOT_USERNAME>, case-insensitively. Telegram entity offsets are
   UTF-16 code units, not Python code-point offsets.
2. reply_to.user_id equals the verified numeric ID of the current bot.

from.is_bot=true alone is insufficient: a reply to another bot must not trigger
this bot. Commands with a suffix count only when the suffix names this bot.
Ordinary text is logged but does not get an LLM reply in this ticket.
edited_message repairs history and users only; it never creates an LLM job.
The future auto-route cooldown does not apply to explicit mention/reply jobs.

## Scope

### 0. Bot identity and admin primitive

- [x] Lazily call getMe, verify the username against environment configuration,
  and cache verified numeric ID and username in bot:self. If identity cannot be
  verified, routed work returns retryable failure instead of treating any bot
  message as this bot.
- [x] Implement a mention extractor with correct UTF-16 offsets for text and
  caption entities and exact full-token comparison.
- [x] Add app/store/admins.py. is_admin(user_id) checks SUPER_ADMIN_ID and the
  Redis admins set. Ticket 05 adds CRUD, but prompt construction already uses the
  computed role.

### 1. QStash adapter

- [x] Implement app/queue/qstash.py with async publish(job_id) to
  {PUBLIC_BASE_URL}/api/telegram/process and body only:

  ~~~json
  {"job_id":"<update_id>"}
  ~~~

- [x] Set deduplication ID telegram-<update_id>, Upstash-Retries: 3 (at most four
  deliveries), Upstash-Retry-Delay:
  max(275000, exp(2.5 * retried) * 1000), and failure callback
  {PUBLIC_BASE_URL}/api/telegram/failure.
- [x] Verify raw QStash body and signature against the canonical destination URL
  with the official receiver and both current and next signing keys. Verify
  before JSON parsing. Use bounded connect/read/total timeouts; never log
  secrets or body contents.
- [x] Publish returns QStash messageId. Save it atomically with enqueue metadata.
  If a callback already advanced the job, record metadata without downgrading
  state. A different messageId for the same job is an integrity error.
- [x] Never put a raw Telegram update, text, username, transcript, or snapshot in
  QStash. It receives a job ID only.
- [x] Add NVIDIA_API_KEY, QStash token, both signing keys,
  JOB_RETENTION_SECONDS=604800, WORKER_BUDGET_SECONDS=240, and
  JOB_LEASE_SECONDS=270 to settings/readiness. Validate
  0 < worker budget < lease < Vercel maxDuration, exact HTTPS public URL, both
  signing keys, and the expected Flash model. Health reports only dependency
  names, never values.

### 2. Durable job creation in the webhook

- [x] After secret/chat gating, determine the route. For a new mention/reply,
  before upserting the trigger:
  - obtain up to 30 existing history records in chronological order;
  - store the triggering message separately, not in the context list;
  - store a reply-target snapshot or null;
  - atomically create job:<update_id> in received state if absent.
- [x] Then idempotently upsert current history/user and publish only after that
  succeeds. If history write fails, received remains and a Telegram retry
  completes history and publication using the original snapshot.
- [x] request_json stores kind="reply", chat_id, update_id,
  trigger_message_id, author ID/name/username snapshot, trigger text/entities,
  reply context or null, and no more than 30 preceding normalized records.
- [x] Snapshot the trusted effective policy at creation, including the numeric
  actor ID and server-computed administrator role. A later role or configuration
  change must not alter queued work.
- [x] Successful publication changes state to enqueued. Publish failure leaves
  received and returns Telegram 503. A retry reuses the same snapshot and QStash
  deduplication ID.
- [x] Support callback-before-publish-response: a signed worker may change
  received to processing; the webhook records messageId and treats
  processing/ready_to_deliver/delivered as proof of enqueue rather than
  reverting state.
- [x] A final update dedup marker may acknowledge enqueued, processing,
  ready_to_deliver, delivered, or terminal failure. It must not suppress an
  incomplete enqueue merely because an early receipt exists.
- [x] Unrouted updates write dedup:update after history/users. Routed updates use
  the same final marker only after durable job logic reaches a safe point.

### 3. Job retention and indexes

- [x] Every job-derived key, answer/evidence/intents/checkpoints, and job index
  member expires no later than immutable job.expires_at. Do not refresh it on
  retry.
- [x] Maintain jobs:chat:<chat_id> and jobs:user:<user_id> sorted sets with score
  equal to expires_at for privacy purge. Prune expired members before reads.
- [x] Index every user whose data appears in the trigger, reply target, context,
  or a nested reply, not only the triggering author.
- [x] A job snapshot is immutable after creation except explicit checkpoints,
  state, attempts, error class, and cancellation. Redis configuration changes
  never alter a queued job's policy or context.

### 4. Flash client and prompt boundary

- [x] Add a lazy Flash factory with DeepSeek V4 Flash and the pinned wrapper:

  ~~~python
  base = ChatNVIDIA(
      model="deepseek-ai/deepseek-v4-flash",
      api_key=settings.NVIDIA_API_KEY,
      temperature=0.4,
      max_completion_tokens=2048,
  )
  client = base.with_thinking_mode(enabled=False)
  ~~~

- [x] Contract-test the outgoing payload: model ID,
  chat_template_kwargs.thinking=false, timeout, and absence of literal root
  extra_body/reasoning_effort. The current historical source sketch is not a
  substitute for this request-shape test.
- [x] Build one aggregate system message from trusted base policy and
  server-computed numeric actor ID/is_admin. Put names, usernames, transcript,
  reply target, and trigger text only in one untrusted user/data message.
- [x] Escape/serialize Telegram data. A user cannot gain admin role or change
  system instructions by writing it in chat.

### 5. Signed processor and delivery

- [x] POST /api/telegram/process verifies the QStash signature over raw request
  body before parsing and derives job_id only from body.
- [x] Atomically acquire a token-and-fencing lease. A duplicate callback with an
  active lease returns 503 and Retry-After equal to lease TTL plus jitter. A
  delivered or cancelled job returns 200 without side effects.
- [x] Enforce a 240-second hard work budget within a renewable 270-second lease.
  Renew only as the owner every 60 seconds. Check lease token, fence, and
  non-terminal job state before every NIM, Tavily, or Telegram side effect.
- [x] On first processing, create one placeholder through sendMessage. Before
  every non-idempotent sendMessage, write a fenced intent with payload hash and
  chunk index. Save message IDs after success.
- [x] Save the generated answer before any Telegram delivery. Split plain text
  into chunks up to 4,000 UTF-16 code units. Edit the placeholder with the
  first chunk, then send later chunks. Upsert successful outbound Bot API
  messages into history.
- [x] The placeholder is the sole pre-answer delivery; save the answer before
  any answer edit or answer chunk send. Process at most four acquired attempts
  (`Upstash-Retries + 1`).
- [x] If a retry finds a send intent with no message-ID checkpoint, mark
  failed_ambiguous and do not send a possible duplicate. Retry safe
  editMessageText only with a known intended edit checkpoint; exact Telegram
  “message is not modified” is success only for that checkpoint.
- [x] Classify provider timeout/429/5xx as retryable. Classify invalid payload,
  auth, and most 4xx as permanent. Save sanitized error class only.

### 6. Failure callback

- [x] POST /api/telegram/failure first verifies signature over raw body. Derive
  source job ID only from bounded base64-decoded sourceBody; require exact
  sourceMessageId, exact process URL, and exhausted retry counters matching the
  job. Ignore untrusted callback response body.
- [x] If the callback arrives before publish metadata, return retryable 503 while
  the job can still be running or publication can still complete. If an active
  lease exists, return 503 plus Retry-After.
- [x] After lease expiry, CAS the job to failed, increment fence, delete lease,
  and make every side-effect guard require both current fence and non-terminal
  state.
- [x] Failure notices have their own checkpoint:
  failure_notice_pending/failure_notice_delivered. Duplicated callbacks may
  complete only a known placeholder edit; they never create another message.
- [x] When no placeholder is known, record that no notice is possible and never
  call sendMessage. A dedicated fenced edit for a pending notice on `failed` is
  the only terminal-state side-effect exception; a permanent edit rejection is
  checkpointed as failure_notice_failed_permanent.
- [x] Do not log response bodies, callback headers, secrets, or transcript.

## Error and limit policy

| Condition | Outcome |
|---|---|
| NVIDIA or Telegram timeout/429/5xx before a known result | failed_retryable, HTTP 503 to QStash, reuse saved snapshot/answer/placeholder |
| QStash bad signature | HTTP 401, no state change |
| QStash body invalid/oversized or wrong metadata | HTTP 400/401, no state change |
| Telegram network timeout after a send intent | failed_ambiguous, no resend |
| Telegram exact intended edit says message is not modified | checkpointed success |
| Invalid model response or permanent provider error | failed |
| Cancelled/delivered job | HTTP 200, no side effect |

## Out of scope

- Automatic keyword routing, lists, and tone commands: Ticket 03.
- /judge, /dispute, /deep, Pro, and Tavily: Ticket 04.
- Web CRUD/UI and OIDC: Ticket 05.

## Automated checks

- [x] Mention entity for this bot, another bot, captions, emoji before mention,
  and UTF-16 offsets.
- [x] Reply to this bot versus reply to another bot; command suffix handling;
  edit updates history but never queues.
- [x] Snapshot excludes trigger and later messages; context remains stable across
  Telegram retry and configuration changes.
- [x] QStash publish body privacy, deduplication ID, retry headers/delay, both
  signature keys, and canonical URL.
- [x] State transitions, publication failure/retry, callback-before-publish race,
  failure-before-metadata race, active-lease callback race, lease renewal/fence,
  and 240-second budget.
- [x] Crash at every send-intent/checkpoint boundary, answer reuse, delivery
  splitting, outbound history, and no duplicate sends.
- [x] Failure callback malformed/missing/invalid/oversize sourceBody, wrong
  destination/message ID, non-exhausted retries, duplicate/terminal job, and
  expiry of every job-derived key.
- [x] ChatNVIDIA request-shape contract and Telegram exact edit normalization.
- [x] Ticket 01 regression tests and Ruff remain green.
- [ ] Python 3.12 CI remains green (not locally available; pending CI).

## Live E2E acceptance

1. On a stable Vercel deployment, an @bot mention receives a coherent Flash reply
   grounded in a fact from preceding conversation; a later message is absent.
2. A reply to this bot considers reply-target text; a reply to another bot does
   not run work.
3. Ordinary text remains silent but is stored; another group or private chat is
   not stored.
4. One placeholder is replaced by plain-text output. An answer above 4,096
   characters arrives in order and all outbound chunks appear in history.
5. Telegram retries and QStash retries after delivered never create another
   answer. A transient LLM failure recovers from the same snapshot.
6. QStash body contains only job_id, and observability/contract tests show the
   exact Flash model and non-thinking payload.

## Risks

- Telegram lacks a sendMessage idempotency key. failed_ambiguous is the deliberate
  no-duplicate trade-off for a rare ambiguous network timeout.
- QStash retries consume quota, so retry count is bounded and permanent errors
  are not retried.
- Lazy import of LangChain is required; verify cold-start latency on live Vercel.
