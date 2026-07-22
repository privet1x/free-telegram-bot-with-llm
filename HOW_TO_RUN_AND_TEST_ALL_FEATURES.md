# How to Run and Manually Test Every Feature

This is an operator and manual acceptance-test guide for the Telegram group bot in this repository. Follow it in order for the first deployment. The later sections can be reused as a release checklist.

The bot is intentionally scoped to one Telegram group. It records that group's conversation history, answers explicit requests with the fast model, can automatically react through rules, and gives administrators deeper dispute analysis with the smart model and optional web research. The web admin panel manages administrators, user policies, rules, tone, logs, and privacy deletion.

> **Privacy and cost warning:** use a private test group first. Tell every participant that the bot stores messages and may send selected content to NVIDIA-hosted models and, during fact-checking, deidentified search queries to Tavily. Do not test with private or sensitive conversations.

> **Configuration assumption for this project:** the existing local `.env` is already populated and must be preserved. The only unset entry is the commented `PUBLIC_BASE_URL=https://<project>.vercel.app` placeholder, because the real origin is not known until Vercel assigns it. Do not copy `.env.example` over `.env`, regenerate secrets, create replacement provider credentials, or commit `.env`.

## 1. What you need

You already have the required credentials. Make sure you can sign in to their existing accounts and projects:

- Telegram and [BotFather](https://t.me/BotFather)
- [Vercel](https://vercel.com/)
- [Upstash Redis](https://upstash.com/docs/redis/overall/getstarted)
- [Upstash QStash](https://upstash.com/docs/qstash/overall/getstarted)
- [NVIDIA API Catalog](https://build.nvidia.com/)
- [Tavily](https://docs.tavily.com/documentation/quickstart) for web-assisted judge results

On your computer, install:

- Git
- Python 3.12
- A Vercel-supported Git provider account, if deploying through the Vercel dashboard

The project already contains the Vercel entry point, `vercel.json`, pinned dependencies, and `.python-version`. Do not set a custom build command or output directory in Vercel.

## 2. Verify and prepare the existing Telegram bot

### 2.1 Verify the bot identity

1. Open a private chat with `@BotFather`.
2. Select the bot already represented by `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME` in `.env`.
3. Confirm its username matches `TELEGRAM_BOT_USERNAME` without the leading `@`.
4. Do not run `/newbot`, revoke the token, or replace either existing value.

Never commit the token or paste it into an issue, chat, screenshot, or log.

### 2.2 Disable privacy mode

The bot needs ordinary group messages for conversation history and automatic rules.

1. In BotFather, run `/setprivacy`.
2. Select your bot.
3. Select **Disable**.
4. If the bot was already in the group, remove it and add it again so Telegram applies the change cleanly.

Telegram's [privacy-mode documentation](https://core.telegram.org/bots/features#privacy-mode) explains which group messages a bot can receive.

### 2.3 Create the test group and add the bot

1. Create a private Telegram group specifically for testing.
2. Add the bot to it.
3. Promote it to administrator. It does not need broad moderation rights, but administrator status makes group-member checks reliable for admin-panel authorization.
4. Add at least one second human tester. Judge testing requires messages from at least two human authors.

The application accepts only `TELEGRAM_ALLOWED_CHAT_ID`; events from every other group are ignored and not stored.

### 2.4 Confirm the existing group and super-admin IDs

`TELEGRAM_ALLOWED_CHAT_ID` and `SUPER_ADMIN_ID` are already set in `.env`; do not rediscover or change them during the normal deployment. Run this after adding the bot to the group:

```bash
python scripts/check_telegram.py
```

The configured chat must be the private group you intend to test. The configured super administrator must be your numeric Telegram user ID.

Only if the existing group ID is proven wrong, temporarily delete an active webhook before using polling:

```bash
python scripts/set_webhook.py delete
```

Send a fresh ordinary message in the test group, then run:

```bash
python scripts/discover_chat_id.py
```

Compare the reported negative group ID with `TELEGRAM_ALLOWED_CHAT_ID`. Correct the value only if they do not match.

Only if `SUPER_ADMIN_ID` is also proven wrong, send another fresh message and use this one-off read-only command:

```bash
python - <<'PY'
import httpx
from app.settings import settings

response = httpx.get(
    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates",
    timeout=30,
).json()
for update in response.get("result", []):
    message = update.get("message") or update.get("edited_message") or {}
    sender = message.get("from") or {}
    if sender.get("id"):
        print(f"id={sender['id']} username=@{sender.get('username', '')}")
PY
```

Identify your own username in the output and correct `SUPER_ADMIN_ID` only if necessary. Avoid sharing the output because it can contain other participants' IDs. Restore the webhook later in Section 8.

## 3. Verify the existing external services

No new service credentials are needed. The following checks explain what each already-populated group of variables is for. Do not rotate or replace working values during deployment.

### 3.1 Upstash Redis

1. Open the existing Redis database in the Upstash console.
2. Confirm it is active and its REST URL/token correspond to `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`.
3. Do not create a second database unless the existing connection check fails and you intentionally want a migration.

Redis is the durable store for messages, users, configuration, jobs, and deduplication. A production deployment must not use the process-local memory fallback because serverless instances are disposable.

### 3.2 Upstash QStash

1. Open the existing QStash project in the Upstash console.
2. Confirm `QSTASH_TOKEN`, `QSTASH_CURRENT_SIGNING_KEY`, and `QSTASH_NEXT_SIGNING_KEY` still belong to it.
3. Keep the existing `QSTASH_URL`; the checked-in default is `https://qstash.upstash.io`.

QStash moves slow LLM work out of the Telegram webhook. Its console's **Logs** view is the first place to look when a Telegram message remains at `Thinking…`.

### 3.3 NVIDIA models

1. Sign in to the existing account at [build.nvidia.com](https://build.nvidia.com/).
2. Confirm the existing `NVIDIA_API_KEY` can access the model IDs configured in `LLM_MODEL_FAST` and `LLM_MODEL_SMART`.
3. The checked-in values (`deepseek-ai/deepseek-v4-flash` and `deepseek-ai/deepseek-v4-pro`) are defaults. You may choose other NVIDIA-supported model IDs; keep both variables non-empty and verify provider access before testing.

The Flash model handles ordinary explicit and automatic replies. The Pro model handles `/deep` and judge workflows.

### 3.4 Tavily

1. Open the existing Tavily project.
2. Confirm the already-configured `TAVILY_API_KEY` is active.

Tavily is optional for application startup, but required to test web-assisted fact checking. Without it, judge still runs in logic-only mode and explicitly warns that web verification was unavailable.

## 4. Run locally and run the automated tests

Create a clean virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

The application automatically loads the existing `.env`. Leave the `PUBLIC_BASE_URL` placeholder commented during local setup; local tests and the local health endpoint do not require the production origin. Never commit `.env`.

Run the complete local quality gate:

```bash
pytest
ruff check .
```

Run the web application:

```bash
uvicorn app.server:app --reload
```

In another terminal:

```bash
curl http://127.0.0.1:8000/api/health
```

Open `http://127.0.0.1:8000/` to inspect the admin-panel shell. Telegram cannot deliver a production webhook to this local HTTP URL, and Telegram OIDC uses exact HTTPS origins and secure cookies, so perform full login and bot testing on the Vercel deployment.

When Upstash variables are absent locally, the application uses an in-memory store. That is useful for unit development only: all data disappears on restart.

Keep `ALLOW_UNFILTERED_LOCAL_CHATS=false`. Do not enable unfiltered chat ingestion on a public deployment.

## 5. Make the first Vercel deployment

The easiest flow is through the dashboard:

1. Push the repository to a private Git repository.
2. In Vercel, select **Add New → Project** and import it.
3. Keep the repository root as the project root.
4. Do not override framework, build command, output directory, or install command. Vercel detects the FastAPI entry point at `api/index.py`.
5. In the import screen's environment-variable area, add the values already present in `.env` to the **Production** environment. Skip only the commented/unset `PUBLIC_BASE_URL` placeholder. Vercel does not automatically read your local `.env`.
6. Choose a stable project name and deploy.
7. Record the assigned production origin, for example `https://your-project.vercel.app`.
8. In the local `.env`, uncomment/replace the placeholder with the exact assigned origin:

   ```dotenv
   PUBLIC_BASE_URL=https://your-project.vercel.app
   ```

   Use HTTPS and only the origin: no callback path, query, fragment, or trailing path.

The bootstrap deployment can report a configuration error because `PUBLIC_BASE_URL` was not known at build/deployment time. This is expected. Do not register the Telegram webhook yet.

You may instead deploy with the CLI after authenticating:

```bash
vercel
vercel --prod
```

The Vercel CLI does not make the application's local `.env` become project-level Production configuration automatically. Confirm the existing values in **Project → Settings → Environment Variables** regardless of which deployment method you use.

The deployment uses Vercel's [Python runtime](https://vercel.com/docs/functions/runtimes/python). The repository requests Python 3.12 and a 300-second function duration.

Use one stable production origin for Telegram, QStash, and admin login. Preview URLs should not receive the production webhook. If Deployment Protection is enabled, ensure Telegram and QStash can publicly reach the production API routes.

## 6. Configure Telegram Web Login for the admin panel

Telegram now exposes web login through BotFather's bot settings. The official [Telegram Login/OIDC guide](https://core.telegram.org/bots/telegram-login) is the authority if BotFather wording changes.

The OIDC client ID and secret are already present in `.env`; preserve them. The Web Login control is in the BotFather **mini app**, not necessarily in the ordinary BotFather chat menu. Open the official mini app from [@BotFather](https://t.me/botfather?startapp), then update the existing Web Login registration to use the new production URL. The separate **Privacy Policy** field is only the public link to your bot's privacy notice; it does not create OIDC login credentials.

1. Open the [@BotFather mini app](https://t.me/botfather?startapp) in Telegram and select the existing bot.
2. Open **Bot Settings → Web Login**. If the page opens as **Login Widget**, tap **Switch to OpenID Connect Login**; the legacy widget is not the flow used by this application.
3. Register or confirm the production origin, for example:

   ```text
   https://your-project.vercel.app
   ```

4. In **Trusted origins**, add the same origin without any path:

   ```text
   https://your-project.vercel.app
   ```

5. In **Redirect URIs**, add the exact callback URI:

   ```text
   https://your-project.vercel.app/api/auth/telegram/callback
   ```

6. Leave **Native login** disabled/default; this server uses the browser redirect flow.
7. Confirm the displayed client ID belongs to the already-configured `TELEGRAM_OIDC_CLIENT_ID`. Do not paste either client secret into chat or documentation.
8. Keep the signing algorithm at its default `RS256`. This application validates RS256 tokens.

The origin and callback must use the same exact scheme and host as `PUBLIC_BASE_URL`. If you later switch to a custom domain, update BotFather, `PUBLIC_BASE_URL`, and the Vercel variables together.

## 7. Set the one missing value and finalize Vercel configuration

At this point, the local `.env` should have every variable populated, including the newly known `PUBLIC_BASE_URL`. Do not regenerate `TELEGRAM_WEBHOOK_SECRET`, `SESSION_SECRET`, or any provider key.

1. Open **Vercel Project → Settings → Environment Variables**.
2. Add `PUBLIC_BASE_URL` to **Production** with the exact origin recorded in Section 5.
3. Audit the remaining rows against the existing local `.env`. Copy values exactly; do not create alternatives.
4. Make sure `ALLOW_UNFILTERED_LOCAL_CHATS` remains `false`.
5. Save the settings and redeploy Production so the deployment receives `PUBLIC_BASE_URL`.

The following tables are an audit inventory, not instructions to generate new values.

### Required for the bot

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Keep existing `.env` value |
| `TELEGRAM_BOT_USERNAME` | Keep existing `.env` value, without `@` |
| `TELEGRAM_WEBHOOK_SECRET` | Keep existing `.env` secret |
| `TELEGRAM_ALLOWED_CHAT_ID` | Keep existing negative numeric group ID |
| `ALLOW_UNFILTERED_LOCAL_CHATS` | `false` |
| `NVIDIA_API_KEY` | Keep existing `.env` value |
| `LLM_MODEL_FAST` | Existing NVIDIA-supported fast model ID |
| `LLM_MODEL_SMART` | Existing NVIDIA-supported smart model ID |
| `UPSTASH_REDIS_REST_URL` | Keep existing `.env` value |
| `UPSTASH_REDIS_REST_TOKEN` | Keep existing `.env` value |
| `QSTASH_URL` | Keep existing `.env` value |
| `QSTASH_TOKEN` | Keep existing `.env` value |
| `QSTASH_CURRENT_SIGNING_KEY` | Keep existing `.env` value |
| `QSTASH_NEXT_SIGNING_KEY` | Keep existing `.env` value |
| `PUBLIC_BASE_URL` | **New value:** exact Vercel production origin |

### Required for the admin panel

| Variable | Value |
|---|---|
| `SUPER_ADMIN_ID` | Keep existing `.env` value |
| `SESSION_SECRET` | Keep existing `.env` secret |
| `TELEGRAM_OIDC_CLIENT_ID` | Keep existing `.env` value |
| `TELEGRAM_OIDC_CLIENT_SECRET` | Keep existing `.env` secret |

### Required for complete judge testing

| Variable | Value |
|---|---|
| `TAVILY_API_KEY` | Keep existing `.env` value |
| `FACT_CHECK_MAX_QUERIES` | Keep existing value; initial default is `3` |

Keep the existing retention, worker, lease, cooldown, and policy-limit values. The table shows the expected initial values for comparison. In particular, the worker budget must remain below the function duration and the job lease must remain safely above the worker budget.

| Variable | Initial value | Meaning |
|---|---:|---|
| `HISTORY_RETENTION_SECONDS` | `2592000` | Retain the bounded history for up to 30 days |
| `JOB_RETENTION_SECONDS` | `604800` | Retain private job snapshots for up to 7 days |
| `WORKER_BUDGET_SECONDS` | `240` | Maximum application work budget per job |
| `JOB_LEASE_SECONDS` | `270` | Worker ownership lease; safely above the worker budget |
| `AUTO_TRIGGER_COOLDOWN_SECONDS` | `30` | Per-chat automatic-reply cooldown |
| `MAX_LIST_POLICIES` | `10` | Maximum applied personal-list policies |
| `MAX_RULE_POLICIES` | `10` | Maximum applied matching rules |

Vercel environment changes do not modify an already-created deployment, which is why the Production redeploy in step 5 is required. The local `.env` and Vercel settings remain separate copies; future intentional changes must be made in both places.

## 8. Validate dependencies and enable the webhook

Activate the local virtual environment, then run:

```bash
python scripts/check_redis.py
python scripts/check_qstash.py
python scripts/check_telegram.py
```

Expected results:

- Redis completes a real temporary write/read/Lua/delete round trip.
- QStash authenticates and both local signing keys match the provider's current key set.
- Telegram confirms the exact bot username, allowed group, membership, and privacy coverage.

Check deployed health:

```bash
curl https://your-project.vercel.app/api/health
```

For the complete setup, expect HTTP 200, `"ok": true`, an Upstash-backed store, and Tavily reported as enabled. A 503 response intentionally lists missing or invalid configuration field names without exposing their values.

Initialize the protected `ignore` list before running the complete manual suite:

```bash
python scripts/seed.py
```

This also creates the demonstration `aggressive` list and `nonsense` rule. Delete those two demos in the admin panel before systematic testing if you want a clean baseline; leave the reserved `ignore` list in place. The panel deliberately cannot create, rename, or delete that protected list.

Only after health and the scripts pass, register the webhook:

```bash
python scripts/set_webhook.py set
python scripts/set_webhook.py info
```

The registered URL must end with `/api/telegram/webhook`, use HTTPS, and show no recent delivery error. The helper configures the secret header, a single connection, and only the update types the application accepts.

Do not use `--drop-pending` unless you intentionally want Telegram to discard undelivered messages.

## 9. Understand all user-visible bot behavior

### Normal chat behavior

- An ordinary message is stored but gets no reply unless an enabled automatic rule matches.
- An exact `@your_bot_username` mention asks the Flash model to answer.
- Replying to a message authored by the bot also asks the Flash model to answer.
- The bot first sends `Thinking…`, then edits that placeholder with the result.
- Long answers are split into Telegram-safe chunks.
- Editing a message updates stored history but never starts a second bot job.
- Commands explicitly addressed to another bot, such as `/ping@OtherBot`, are ignored.

### Commands

| Command | Who can use it | Behavior |
|---|---|---|
| `/ping` | Anyone in the allowed group | Returns `pong` |
| `/help` | Anyone | Shows the basic in-chat usage help |
| `/mode` | Administrator | Shows effective tone/mode and available presets |
| `/tone <preset>` | Administrator | Sets this group's tone preset |
| `/tone global <preset>` | Administrator | Sets the global tone preset |
| `/tone clear` | Administrator | Clears the group override and falls back to global |
| `/set_mode <preset>` | Administrator | Alias for tone selection |
| `/judge [N]` | Administrator | Analyzes the latest `N` eligible messages as a dispute |
| `/dispute [N]` | Administrator | Alias for `/judge` |
| `/deep <question>` | Administrator | Uses the Pro model for a deeper contextual answer |

Available tone presets are `neutral`, `serious`, `scientist`, `street`, and `sarcastic_robot`. The short alias `sarcastic` maps to `sarcastic_robot`.

`/judge` defaults to the configured judge window. A supplied ASCII number is clamped to 5–30. It needs at least three meaningful prior messages from at least two human authors. A direct bot mention containing “judge us” or “who is right” also enters the judge workflow. Service commands are excluded from the transcript.

Judge extracts checkable claims, performs up to the configured number of Tavily searches, and appends sources with `[Sx]` citations when web evidence is available. If Tavily is missing or a claim cannot be researched safely, it continues in logic-only mode and says so.

### Lists and rules

- A **list** is a reusable trusted instruction attached to selected users. It can apply to explicit replies, automatic replies, judge, or a combination of these scopes.
- At most the configured number of enabled list policies applies to one request. Higher-priority policies are injected first; equal priority is ordered by slug.
- The reserved `ignore` list suppresses automatic reactions for its members. It does not block their exact mentions, replies, or commands.
- A **rule** matches text by substring, word, or phrase. Its scope can be `explicit`, `auto`, `judge`, or `all`.
- Rules in `auto` or `all` scope can make the bot answer an ordinary unmentioned message.
- Automatic replies observe the configured per-chat cooldown, 30 seconds by default. Explicit requests still work during cooldown.
- Higher-priority rules are applied first. A stopping rule prevents lower-priority groups from being applied.
- Judge rules match the selected conversation transcript, not the `/judge` command text itself.

## 10. Configure the admin panel

Open the production origin in a normal browser and select **Log in with Telegram**. The account whose ID equals `SUPER_ADMIN_ID` becomes the super administrator. Sessions last at most eight hours.

### Admins

Only the super administrator can add or revoke other administrators.

1. Open **Admins**.
2. Add a user by exact numeric ID, or by username after the bot has observed at least one message from that user.
3. The candidate must still be an active member of the configured group.
4. Ask the new administrator to log in with their own Telegram account.

Assigned administrators can manage normal configuration but cannot grant administrators or use super-admin-only privacy deletion. Revocation invalidates their active admin session.

### Lists

1. Open **Lists** and create a meaningful slug, title, injected instruction, priority, and scopes.
2. Add users as members by numeric ID or observed username.
3. Enable the list only when ready.
4. Use the previewed membership/configuration to verify it before chat testing.

The reserved `ignore` list cannot be renamed or deleted.

### Rules

1. Open **Rules**.
2. Choose a stable rule ID; it cannot be changed later.
3. Choose match type: substring, whole word, or phrase.
4. Select scope and priority.
5. Enter the trusted instruction that will accompany a matching model request.
6. Enable the rule and optionally enable its stop behavior.

Use distinctive nonsense test words so normal conversation cannot trigger paid requests.

### Tone

The **Tone** page controls global defaults and the allowed group's override. It also shows the effective result. Presets control response style; custom mode requires a non-empty custom prompt. Judge window size must remain between 5 and 30. Clear the group override to inherit global settings again.

### Logs

The **Logs** page shows recent bot activity and status. Use **Refresh** after a chat test. Message text is rendered as text, not executable HTML.

### Privacy

The **Privacy** page provides:

- A participant notice to copy and publish in the group before real use.
- Super-admin-only deletion for one observed user, optionally including messages and jobs.
- A full chat purge protected by an exact confirmation phrase.

Deletion cannot recall content already sent to model/search providers. A deleted user who speaks again is observed again. Full purge is irreversible, so test it last.

## 11. Manual acceptance test: run in this order

Record pass/fail and screenshots in a separate test note. Watch Vercel function logs and QStash delivery logs during the first pass, but do not paste secrets or private message bodies into bug reports.

### A. Deployment smoke test

- [ ] `GET /api/health` returns HTTP 200 and `ok: true`.
- [ ] Health reports the durable Upstash store.
- [ ] Health reports Tavily enabled for the full test configuration.
- [ ] `scripts/set_webhook.py info` shows the correct production URL and no recent error.
- [ ] `/ping` returns `pong` in the allowed group.
- [ ] `/help` returns usage text.
- [ ] A plain message without a matching auto rule produces no response.
- [ ] An exact `@botusername What is 2 + 2?` mention creates `Thinking…` and then an answer.
- [ ] Reply to that bot answer with another question; the bot answers again.
- [ ] Edit an ordinary earlier message; history updates but no duplicate reply appears.
- [ ] Send `/ping@OtherBot`; this bot does not respond.
- [ ] Refresh admin **Logs** and confirm the recent actions appear.

### B. Authorization and tone

- [ ] As the super administrator, `/mode` reports the effective mode.
- [ ] Run `/tone serious`, mention the bot, and verify a more formal response.
- [ ] Run `/tone scientist`, mention the bot, and verify the style changes.
- [ ] Run `/tone sarcastic`; `/mode` should report `sarcastic_robot`.
- [ ] Run `/set_mode serious`; confirm it behaves like `/tone serious`.
- [ ] Run `/tone global street`, then `/tone clear`; the group should inherit `street`.
- [ ] Restore the desired default, normally `/tone neutral`.
- [ ] As a non-admin tester, try `/mode` and `/tone serious`; both are refused without changing configuration.
- [ ] Repeat one tone change from the admin panel and verify the effective value in `/mode`.
- [ ] In the panel, set the current chat to `custom` with `Begin each answer with CUSTOM-TEST and use one concise paragraph.` Mention the bot and verify the custom style, then return to the intended preset.
- [ ] Change **Judge context messages** to 5, save, and confirm the Effective panel shows 5. Run `/judge` without a number after preparing sufficient context, then restore the default value of 20.

### C. Assigned-admin lifecycle

- [ ] Have the second tester send a message so their username is observed.
- [ ] Look them up once by exact `@username` and once by numeric ID; both return the same profile.
- [ ] Look up an unobserved username and confirm the panel explains that the person must message first or be entered by numeric ID.
- [ ] Add them in **Admins** while they are an active group member.
- [ ] They can log into the panel with Telegram.
- [ ] They can view and manage ordinary configuration.
- [ ] They cannot add administrators or use super-admin privacy deletion.
- [ ] Revoke them from **Admins**.
- [ ] Their existing panel session loses access; they cannot log in again as an admin.
- [ ] Log out of the super-admin session and confirm protected panel data disappears, then log in again for the remaining tests.

### D. Personal list policies

Create a temporary list named `manual_test`:

- Scope: `explicit`
- Priority: a distinctive test priority
- Instruction: `Begin your answer with LIST-TEST.`
- Member: the second tester

Then verify:

- [ ] The member explicitly mentions the bot and the answer begins with `LIST-TEST`.
- [ ] A non-member asks the same question and does not receive that marker.
- [ ] Disable the list and confirm the member no longer receives its behavior.
- [ ] Re-enable it, rename it to `manual_test_renamed`, and confirm its membership and behavior remain attached to the renamed list.
- [ ] Delete the renamed temporary list.

Test the reserved `ignore` list after creating the auto rule in the next section:

- [ ] Add the tester to `ignore`.
- [ ] Their matching ordinary message does not auto-trigger.
- [ ] Their exact bot mention still receives an explicit answer.
- [ ] Remove them from `ignore` and confirm auto behavior returns after cooldown.

### E. Rules and cooldown

Create an enabled rule named `manual_auto`:

- Match type: `word`
- Pattern: `bananatest`
- Scope: `auto`
- Instruction: `Begin your answer with AUTO-TEST.`

Verify:

- [ ] An ordinary message containing the whole word `bananatest` triggers a reply beginning with `AUTO-TEST`.
- [ ] A second matching ordinary message within 30 seconds is suppressed by cooldown.
- [ ] An exact mention during cooldown still gets an explicit response.
- [ ] A larger word such as `bananatestx` does not match the whole-word rule.
- [ ] After cooldown, the original word triggers again.

Create a second rule named `manual_explicit`:

- Match type: `word`
- Pattern: `violetest`
- Scope: `explicit`
- Instruction: `Begin your answer with EXPLICIT-TEST.`

Verify:

- [ ] Plain `violetest` does not trigger the bot.
- [ ] An exact mention containing `violetest` gets the explicit marker.
- [ ] A higher-priority stopping rule prevents a lower-priority matching rule from contributing.
- [ ] Disabled rules do nothing.

Test the remaining match types and scopes with temporary, distinctive records:

| Test | Temporary configuration | Expected result |
|---|---|---|
| Substring | Match `grape`, scope `explicit`, marker `SUBSTRING-TEST` | A mention containing `grapetest` matches |
| Phrase | Match `red blue`, scope `explicit`, marker `PHRASE-TEST` | A mention containing `red, blue` matches after punctuation normalization; reversed words do not |
| All | Match `allscopetest`, scope `all`, marker `ALL-SCOPE-TEST` | It applies to an ordinary auto trigger, an explicit mention, and judge when the selected transcript contains the match |
| Judge | Match `orbitest`, scope `judge`, marker `JUDGE-RULE-TEST` | It does not trigger an ordinary reply but influences judge when `orbitest` is in the selected transcript |

- [ ] Create two explicit rules for the same word at different priorities. Put `stop_processing` on the higher-priority rule; verify the lower-priority instruction is excluded.
- [ ] Edit a rule and confirm its ID cannot be changed.
- [ ] Delete all temporary rules after the judge tests to prevent accidental requests and cost.

### F. Judge and deep analysis

Have two humans exchange at least three meaningful messages. Include one safe, publicly checkable disagreement, for example the opening year of a landmark. Do not use personal claims.

- [ ] As an administrator, run `/judge 5`.
- [ ] `Thinking…` is replaced by a Pro-model dispute analysis.
- [ ] The response identifies the participants without leaking internal numeric IDs.
- [ ] Checkable claims have `[Sx]` citations and a source list when Tavily evidence exists.
- [ ] The conclusion distinguishes sourced facts from reasoning or uncertainty.
- [ ] Run `/dispute 5` and confirm equivalent routing.
- [ ] Mention the bot with “judge us” and confirm judge routing.
- [ ] A non-admin `/judge 5` is refused before expensive model work starts.
- [ ] Run `/deep Explain the strongest argument on each side`; confirm a contextual Pro-model answer.
- [ ] Run judge when fewer than the required meaningful/two-author messages exist after the final purge; it should explain that context is insufficient.

Optional degraded-mode test: remove `TAVILY_API_KEY` from a disposable deployment and redeploy. Health should report Tavily disabled, while judge should complete with a clear logic-only warning and no fabricated web citations. Restore the key and redeploy afterward.

### G. Persistence, formatting, and safety

- [ ] Redeploy the application and confirm users, configuration, and logs remain in the admin panel.
- [ ] Ask for a deliberately long answer and confirm it arrives in readable chunks.
- [ ] Send emoji and non-Latin text; verify it remains readable in chat and logs.
- [ ] Send the literal text `<img src=x onerror=alert(1)>`; the admin log shows it as text and no browser alert executes.
- [ ] Confirm another Telegram group gets no response from this single-chat deployment.
- [ ] Confirm an edited matching message never creates an automatic job.

### H. Privacy deletion — destructive, do last

1. Copy the privacy notice from the panel and publish it in the test group.
2. Choose a test participant, not the super administrator.
3. Delete their profile without purging messages. Confirm their observed profile, list memberships, and any admin grant disappear, while retained chat messages remain.
4. Have them speak again; verify they are observed again.
5. Delete them with message/job purge enabled. Confirm authored messages and stored reply quotations are scrubbed and their queued work is removed.
6. Enter the exact full-purge confirmation phrase shown by the panel.
7. Confirm logs/history and indexed jobs for the group are empty.
8. Immediately run `/judge 5`; it should report insufficient history.

Do not perform the full purge against a group whose retained history you need.

## 12. Reserved list and optional demo seed data

The following command creates the reserved `ignore` list plus demonstration `aggressive` and `nonsense` policies:

```bash
python scripts/seed.py
```

It is idempotent. `--force` overwrites demo records, so use that option only intentionally. The application can run without seeding, but the protected `ignore` list will not be available for membership management. For a clean real setup, seed once, retain `ignore`, and delete the two demo records in the panel. Create all other policies manually.

## 13. Troubleshooting

### Health returns 503

Read the named configuration fields in the JSON response, correct them in Vercel Production variables, and redeploy. The response intentionally does not echo secret values. Re-run all three check scripts.

### Bot sees commands but not ordinary messages

Privacy mode is probably still enabled. Disable it in BotFather, remove and re-add the bot, and keep it as group administrator. Run `scripts/check_telegram.py` again.

### Bot responds nowhere

Check, in order:

1. `scripts/set_webhook.py info` for the URL, pending count, and last Telegram error.
2. Vercel deployment and function logs.
3. The exact `TELEGRAM_ALLOWED_CHAT_ID` and bot username.
4. That the production endpoint is public and not blocked by deployment protection.
5. That environment changes were followed by a redeploy.

### `Thinking…` never changes

Check QStash Logs for delivery status, then compare the QStash token and signing keys with `scripts/check_qstash.py`. Check Vercel worker logs, NVIDIA access to the configured model IDs, the API key, and the configured function/worker/lease timings.

### Admin login fails or loops

Confirm all of these match exactly:

- Browser origin
- `PUBLIC_BASE_URL`
- BotFather registered origin
- BotFather callback ending in `/api/auth/telegram/callback`
- Vercel OIDC client ID and secret

Keep OIDC at RS256. Also verify that the Telegram account is the configured super administrator or an assigned administrator still in the group. The bot should be group administrator for reliable membership checks.

### Username cannot be found in the panel

Ask that user to send a message in the allowed group, refresh, and try the exact username again. Numeric ID lookup works without relying on a current username.

### An auto rule does not fire

Check that it is enabled, has `auto` or `all` scope, uses the intended match type, and matches the exact text. Then check the sender's `ignore` membership and wait past the cooldown. Editing an existing message is intentionally non-triggering.

### Judge has no citations

Confirm Tavily is enabled in health and its key is valid. Some claims are subjective, private, unsafe to search, or unsupported; those should not receive invented citations. Review Vercel logs for provider timeout information.

### A setup script says polling conflicts with a webhook

Delete the webhook temporarily:

```bash
python scripts/set_webhook.py delete
```

Run the discovery step, then restore it with `set` and verify with `info`.

## 14. Cleanup and taking the bot offline

After manual testing:

- Delete temporary rules and lists.
- Remove test members from `ignore`.
- Clear test chat tone overrides and restore the intended global preset.
- Revoke temporary administrators.
- Use privacy deletion/full purge only if the test history should be erased.
- Rotate any secret that appeared in a terminal recording, screenshot, or shared message, then update Vercel and redeploy.

To stop Telegram delivery without deleting data:

```bash
python scripts/set_webhook.py delete
```

To resume later:

```bash
python scripts/set_webhook.py set
python scripts/set_webhook.py info
```

## 15. Release acceptance summary

A release is ready for real participants only when all of these are true:

- Automated tests and Ruff pass.
- Production health returns 200 with durable Upstash storage.
- Redis, QStash, and Telegram check scripts pass.
- Webhook info has the correct stable URL and no delivery error.
- Mention, reply, automatic rule, `/deep`, and `/judge` each complete successfully.
- Admin login, assignment, revocation, lists, rules, tone, logs, and privacy controls were manually checked.
- The bot ignores non-allowed groups.
- The participant privacy notice has been published.
- Temporary test rules, policies, users, and admin grants have been cleaned up.

At that point, add real participants to the one configured group, explain the mention/reply and admin-only commands, and monitor Vercel and QStash logs during the first live session.
