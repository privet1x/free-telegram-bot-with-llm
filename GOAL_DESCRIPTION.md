# Product Requirements: Smart Telegram Bot with a Web Admin Panel and Dynamic LLM Behaviour

## 1. Overview

Build a Telegram bot for one private group of roughly 10–15 people, with a web
admin panel. Its key capability is dynamically configurable LLM behaviour via
NVIDIA NIM: flexible per-user rules, keyword triggers, conversation-context
analysis for disputes, and tone management.

The project should fit a low-cost stack: Vercel serverless functions, a free
database tier, and available LLM quotas.

## 2. Architecture and technology stack

- **Runtime and hosting:** Vercel Serverless Functions with webhooks, never long
  polling.
- **Language and framework:** Python with FastAPI in `/api`, optimised for
  serverless execution.
- **LLM provider:** NVIDIA NIM API (`https://integrate.api.nvidia.com/v1`).
- **Models:** `deepseek-ai/deepseek-v4-flash` for ordinary replies and
  `deepseek-ai/deepseek-v4-pro` for complex reasoning and disputes. Qwen is not
  used.
- **Database:** a serverless-safe persistent store for settings, admins, rules,
  and chat history. The selected stack is Upstash Redis.
- **Web admin:** a lightweight UI on the same Vercel domain, protected by
  Telegram OIDC Authorization Code Flow and a server-side session.

## 3. Roles and permissions

1. **Super admin / owner**
   - Has full access to the web admin panel.
   - Can designate bot admins by Telegram ID or observed `@username`.
   - Configures global rules and triggers.
2. **Assigned admins**
   - Are group members granted bot-admin access in the UI.
   - Can change the global or current-chat tone from Telegram or the UI.
   - Can invoke dispute commands and manage trigger rules.
3. **Users**
   - Ask the bot questions with a mention or a reply to the bot.
   - May be subject to administrator-configured personal rules.

## 4. Core features

### Web admin panel

- Manage users and lists. Add a user by numeric Telegram ID after membership
  verification, or by `@username` only after the bot has observed that username
  in the allowed group. The Bot API cannot safely resolve arbitrary usernames.
- Grant and revoke bot-admin rights.
- Build text rules for word roots, words, and phrases, plus per-user list-based
  policies such as a sarcastic-response list or an ignore list.
- Select tone presets: scientist, street, sarcastic robot, neutral assistant,
  and serious. Support a custom system-prompt override.

### Telegram processing

- Keep the latest 20–30 messages with author, text, and timestamp in persistent
  storage so the model can understand group context.
- Resolve disputes through `/judge`, `/dispute`, or a bot mention containing
  an English judge intent such as “judge us” or “who is right?”. The bot analyses
  the latest N messages, identifies reasoning errors, fact-checks when possible,
  and gives a fair verdict in the selected tone.
- Reply when mentioned (`@bot_name <question>`) or when a user replies to a
  message sent by this bot.
- Allow admins to change tone from chat, for example `/tone sarcastic` or
  `/set_mode serious`.

## 5. LLM prompt construction

For each response, build the effective prompt from these layers:

1. **Base layer:** the active tone from admin settings.
2. **Actor layer:** the verified caller identity, username as untrusted data,
   and computed admin status.
3. **Personal-policy layer:** administrator-authored list policies matching the
   caller.
4. **Trigger layer:** administrator-authored keyword/phrase rules matching the
   message.
5. **Dispute layer:** a dedicated template and raw recent-message transcript for
   a judge request.

## 6. Implementation request

The project must:

1. Define the Vercel-safe architecture and Redis schema for users, lists, rules,
   tone settings, and chat logs.
2. Break the work into a small number of independently testable end-to-end
   tickets.
3. Use a Vercel-compatible Python repository structure in which the web UI and
   bot API live together.
4. Implement the webhook, NVIDIA NIM client and prompt builder, history buffer,
   and admin CRUD endpoints in their assigned tickets.

The original implementation preference is Python and LangChain `ChatNVIDIA`.
The current model and non-thinking transport contract is specified in
`tickets/00-ARCHITECTURE.md`; it supersedes any historical code fragment in
`PROMPT.md`.
