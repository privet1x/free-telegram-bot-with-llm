# Smart Telegram Bot with an LLM Admin Panel

A Telegram bot for one private group chat. It will provide configurable LLM
behaviour through NVIDIA NIM, keyword and per-user rules, dispute resolution,
and a web admin panel. The application is designed for Vercel's serverless
webhook model.

The implementation plan and the shared architecture contract are in
[`tickets/`](tickets/README.md).

Current status: **Tickets 01–04 are implemented and pass local automated
checks.** Ticket 02 adds durable mention/reply jobs, signed QStash processing,
Ticket 03 adds deterministic policies, automatic routing, and tone control, and
Ticket 04 adds admin-only judge/deep workflows with grounded evidence support.
and DeepSeek V4 Flash replies. Live Vercel/Telegram/Upstash/NVIDIA acceptance
remains pending an authorized deployment check.

## Stack

- **Python 3.12 + FastAPI**, deployed as one **Vercel Hobby** function. The bot
  uses webhooks, not polling.
- **Upstash Redis (REST)** for history, deduplication, observed users, verified
  bot identity, and durable private job snapshots. Upstash is mandatory in
  production; the in-memory adapter exists only for local development and
  tests. History holds at most 30 messages and uses both a 30-day per-record
  cutoff and a sliding `HISTORY_RETENTION_SECONDS` list TTL.
- **NVIDIA NIM** via LangChain `ChatNVIDIA` for
  `deepseek-ai/deepseek-v4-flash` non-thinking replies.
- **Upstash QStash** to decouple slow LLM work from Telegram webhooks. QStash
  receives only an opaque job ID; snapshots remain in Redis.

## Repository layout

```text
api/index.py           # Vercel entry point: re-exports FastAPI `app`
app/
  settings.py          # configuration from environment variables (.env locally)
  server.py            # FastAPI assembly and /api/health
  llm/                 # DeepSeek V4 Flash client and prompt boundary
  queue/               # QStash publishing and signature verification
  store/               # Redis, history, users, admins, durable jobs
  telegram/            # webhook ingestion, routing, identity, worker delivery
  request_body.py      # capped streaming reads for public routes
scripts/set_webhook.py # webhook registration and diagnostics
tests/                 # pytest suite
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# Fill in the Ticket 02 variables listed below.

# Start locally; health endpoint: http://127.0.0.1:8000/api/health
uvicorn app.server:app --reload

# Run checks
pytest
ruff check api app scripts tests
```

Without Upstash credentials, the application uses in-memory storage for local
development and tests only. **On Vercel, missing persistent Redis or a configured
allowed chat ID is a readiness failure**: health and webhook routes return `503`
instead of silently losing state.

To verify Vercel routing locally, run:

```bash
vercel dev
```

## Environment variables

For Ticket 02, configure:

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`;
- `TELEGRAM_WEBHOOK_SECRET` (1–256 letters, numbers, `_`, or `-`);
- `TELEGRAM_ALLOWED_CHAT_ID`, the numeric ID of the one private group;
- `PUBLIC_BASE_URL`, an HTTPS deployment origin without a path, query, or
  fragment;
- `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`;
- `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, and
  `QSTASH_NEXT_SIGNING_KEY`;
- `NVIDIA_API_KEY`.

Keep the checked-in defaults for `LLM_MODEL_FAST=deepseek-ai/deepseek-v4-flash`,
`JOB_RETENTION_SECONDS=604800`, `WORKER_BUDGET_SECONDS=240`, and
`JOB_LEASE_SECONDS=270`. The worker budget must remain shorter than the lease,
which must remain shorter than Vercel's 300-second function duration.

> Do not commit real secrets (`.env` is ignored). If an NVIDIA key was ever
> committed to an earlier version of `.env.example`, rotate it in NVIDIA NIM.

## Deploying to Vercel

1. Create a bot with **@BotFather** and obtain its token and username.
2. In @BotFather, use `/setprivacy`, select the bot, and choose **Disable**. If
   the bot was already in the group, remove and add it again. Making the bot a
   group administrator is an alternative.
3. Add the bot to the private group and send an ordinary message. Before setting
   a webhook, discover the chat ID without printing message content:

   ```bash
   python scripts/discover_chat_id.py
   ```

4. Fill in `.env`, create an Upstash Redis database, and run:

   ```bash
    python scripts/check_redis.py
    python scripts/check_qstash.py
    python scripts/check_telegram.py
   ```

5. Import the repository into Vercel and configure the same environment
   variables, including `PUBLIC_BASE_URL=https://<project>.vercel.app`.
6. Deploy and check readiness:

   ```bash
   vercel deploy --prod
   curl -f https://<project>.vercel.app/api/health
   ```

7. Register and inspect the webhook:

   ```bash
   python scripts/set_webhook.py set
   python scripts/set_webhook.py info
   ```

   The script sets `max_connections=1`. That is appropriate for the one small
   group and preserves ingestion/context order. Pending updates are retained by
   default; use destructive `--drop-pending` only for an intentional reset.

8. Send an ordinary message and `/ping` in the group. The message should appear
   in `hist:<chat_id>` in Upstash and `/ping` should return `pong`.
   `/ping@OtherBot` must be ignored. Then mention the bot exactly or reply to a
   message it sent. The webhook creates a Redis job snapshot, QStash calls the
   signed processor, and one `Thinking…` placeholder is edited into a Flash
   response. Do not test this flow against a production group until deployment
   and provider costs are authorized.

## Ticket 02 API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Health check and selected storage backend |
| POST | `/api/telegram/webhook` | Receive updates, preserve history, and enqueue exact mentions/replies |
| POST | `/api/telegram/process` | Signed QStash worker: lease, Flash response, retry-safe Telegram delivery |
| POST | `/api/telegram/failure` | Signed exhausted-QStash callback and checkpointed failure notice |
