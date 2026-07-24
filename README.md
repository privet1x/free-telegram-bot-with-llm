# Smart Telegram Bot with an LLM Admin Panel

A Telegram bot for one private group chat. It provides NVIDIA NIM conversation,
deterministic rules and per-user modifiers, explicit web search, and an
owner-only web panel. The application is designed for Vercel's serverless
webhook model.

The implementation plan and the shared architecture contract are in
[`tickets/`](tickets/README.md).

Current status: **Tickets 01–08 are implemented locally; deployment acceptance
and participant shard population remain pending.** The bot includes durable
mention/reply/keyword/scheduled jobs, immutable prompt policy, static and
gathered-memory infrastructure, restricted `/lobotomy` with `/invite` and
`/uninvite` roster commands,
Telegram-first-name
addressing, owner-only Telegram OIDC, CRUD controls, and privacy deletion. Live
Vercel/Telegram/Upstash/NVIDIA/Tavily/OIDC acceptance remains pending an
authorized deployment check.

## Stack

- **Python 3.12 + FastAPI**, deployed as one **Vercel Hobby** function. The bot
  uses webhooks, not polling.
- **Upstash Redis (REST)** for history, deduplication, observed users, verified
  bot identity, and durable model-job snapshots. Upstash is mandatory in
  production; the in-memory adapter exists only for local development and
  tests. History holds at most 30 messages and uses both a 30-day per-record
  cutoff and a sliding `HISTORY_RETENTION_SECONDS` list TTL.
- **NVIDIA NIM** via LangChain `ChatNVIDIA` for one configured model. The
  checked-in `LLM_MODEL` default is `google/gemma-4-31b-it`; the application
  deliberately does not enforce a model allowlist.
- **Upstash QStash** to decouple slow LLM work from Telegram webhooks. QStash
  receives only an opaque job ID; snapshots remain in Redis.
- **Telegram OIDC + server-side sessions** protect a same-origin vanilla
  JavaScript panel that only `SUPER_ADMIN_ID` can open.

## Repository layout

```text
api/index.py           # Vercel entry point: re-exports FastAPI `app`
  app/
  settings.py          # configuration from environment variables (.env locally)
  server.py            # FastAPI assembly and /api/health
  llm/                 # immutable super-context and one ChatNVIDIA client
  queue/               # QStash publishing and signature verification
  store/               # Redis, history, users, admins, durable jobs
  telegram/            # webhook ingestion, routing, identity, worker delivery
  memory/              # immutable participant shards and gathered memory
  metrics.py           # bounded privacy-safe latency samples
  auth/                 # Telegram OIDC, group membership, revocable sessions
  admin/                # typed same-origin admin and privacy API
  request_body.py      # capped streaming reads for public routes
public/                 # static admin panel, no build step
scripts/set_webhook.py # webhook registration and diagnostics
tests/                 # pytest suite
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# Fill in the production variables listed below.

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

Configure:

- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`;
- `TELEGRAM_WEBHOOK_SECRET` (1–256 letters, numbers, `_`, or `-`);
- `TELEGRAM_ALLOWED_CHAT_ID`, the numeric ID of the one private group;
- `PUBLIC_BASE_URL`, an HTTPS deployment origin without a path, query, or
  fragment;
- `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`;
- `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, and
  `QSTASH_NEXT_SIGNING_KEY`;
- `NVIDIA_API_KEY`;
- `TAVILY_API_KEY` for the explicit `/google` command;
- `CRON_SECRET` for the authenticated `/api/cron/banter` Vercel Cron route;
- `SUPER_ADMIN_ID`, the immutable positive Telegram ID of the owner;
- `SESSION_SECRET`, at least 32 random bytes;
- `TELEGRAM_OIDC_CLIENT_ID` and `TELEGRAM_OIDC_CLIENT_SECRET` from BotFather
  Web Login configuration.

Set `LLM_MODEL=google/gemma-4-31b-it` for this deployment. There is one model
setting for every generated path; the code checks that it is non-empty but does
not restrict you to a compiled allowlist.
`JOB_RETENTION_SECONDS=604800`, `WORKER_BUDGET_SECONDS=240`, and
`JOB_LEASE_SECONDS=270`. The worker budget must remain shorter than the lease,
which must remain shorter than Vercel's 300-second function duration.

The checked-in `app/memory/manifest.json` maps fixed Telegram numeric user IDs to
static `memory-shard-<slug>.md` files. Populate that manifest and the corresponding
files with owner-authored facts before enabling trusted participant memory. Unknown
Telegram users never receive a trusted static shard automatically. Gathered
observations are bounded, fallible Redis data and are cleared by `/lobotomy`.

Vercel Cron invokes `/api/cron/banter` every 20 minutes. It is quiet from 01:00
through 08:59 in `Europe/Warsaw`, and uses only the latest human-authored context.

> Do not commit real secrets (`.env` is ignored). If an NVIDIA key was ever
> committed to an earlier version of `.env.example`, rotate it in NVIDIA NIM.

## Deploying to Vercel

1. Create a bot with **@BotFather** and obtain its token and username.
2. In @BotFather, use `/setprivacy`, select the bot, and choose **Disable**. If
   the bot was already in the group, remove and add it again.
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

5. In BotFather → Bot Settings → Web Login, register the stable Vercel origin
   and exact callback
   `https://<project>.vercel.app/api/auth/telegram/callback`. Configure the
   resulting OIDC client values, `SUPER_ADMIN_ID`, and a random session secret.
6. Import the repository into Vercel and configure the same environment
   variables, including `PUBLIC_BASE_URL=https://<project>.vercel.app`.
7. Deploy and check readiness:

   ```bash
   vercel deploy --prod
   curl -f https://<project>.vercel.app/api/health
   ```

8. Register and inspect the webhook:

   ```bash
   python scripts/set_webhook.py set
   python scripts/set_webhook.py info
   ```

   The script sets `max_connections=1`. That is appropriate for the one small
   group and preserves ingestion/context order. Pending updates are retained by
   default; use destructive `--drop-pending` only for an intentional reset.

9. Send an ordinary message and `/ping` in the group. The message should appear
   in `hist:<chat_id>` in Upstash and `/ping` should return
   `<your first_name>, pong`.
   `/ping@OtherBot` must be ignored. Then mention the bot exactly or reply to a
   message it sent. The webhook creates a Redis job snapshot, QStash calls the
   signed processor, and one addressed `Thinking…` placeholder is edited into a
   Gemma
   response. Do not test this flow against a production group until deployment
   and provider costs are authorized.
10. Open the stable deployment, complete Telegram login as `SUPER_ADMIN_ID`,
    and test session revocation and the privacy controls before announcing
    production use. Other Telegram users cannot establish a panel session.

## Telegram commands

Every command is available to every member of the allowed group:

- `/ping` and `/help` return immediate addressed responses.
- `/tone <neutral|serious|scientist|street|sarcastic_bot>` changes the chat
  preset without clearing history. `/mode` shows the active preset.
- `/think <question>` uses up to 30 preceding messages and enables private model
  thinking; only the final answer is delivered.
- `/google <query>` sends that bounded explicit query to Tavily, then uses
  thinking mode to answer with validated source URLs. It does not send recent
  group history to Tavily.
- Scheduled banter every 20 minutes also uses thinking mode and returns only the
  final joke; it runs even when the current human context is empty.

`/set_mode`, `/deep`, `/judge`, and `/dispute` are removed. Mention the bot or
reply to one of its messages for an ordinary non-thinking response.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Health, selected storage backend, and optional dependency state |
| POST | `/api/telegram/webhook` | Receive updates, preserve history, and enqueue exact mentions/replies |
| POST | `/api/telegram/process` | Signed QStash worker: lease, model response, retry-safe Telegram delivery |
| POST | `/api/telegram/failure` | Signed exhausted-QStash callback and checkpointed failure notice |
| GET | `/api/public/config` | Safe bot username and OIDC client ID |
| GET | `/api/auth/telegram/start` | Start Telegram OIDC with state, nonce, and PKCE |
| GET | `/api/auth/telegram/callback` | Validate Telegram identity and issue a server session |
| POST | `/api/auth/logout` | Same-origin, CSRF-protected session revocation |
| GET | `/api/admin/me` | Current role, retention settings, and CSRF token |
| GET/DELETE | `/api/admin/users` | Exact observed-user lookup and privacy deletion |
| CRUD | `/api/admin/lists` | Personal policies and numeric membership |
| CRUD | `/api/admin/rules` | Deterministic text rules |
| GET/PUT/DELETE | `/api/admin/tone` | Global/chat fixed-preset tone |
| GET/DELETE | `/api/admin/logs` | Bounded allowed-chat history and confirmed full purge |

## Participant privacy notice

Publish the notice generated on the panel's **Privacy** page before production
use. The panel inserts the exact configured retention values, including seconds
that do not divide evenly into days. Enter a working Telegram `@username` or
another monitored owner contact first; copying remains disabled while that field
is empty.

Keep the generated operational facts intact: expired history is excluded and
removed on the next access, observed profiles and list membership remain until
owner deletion, selected context is processed by NVIDIA NIM, `/google` sends
only its explicit bounded query to Tavily, QStash receives only an opaque job
ID, and data already sent to an external provider cannot be recalled.
