# TICKET-06 — Trusted Conversation Identity and Public Commands

## Goal

Replace the legacy judge/admin-command conversation contract with one immutable
assistant identity, deterministic Telegram-first-name addressing, one NVIDIA
Gemma model, and a small public command surface.

This ticket intentionally supersedes the conversational parts of Tickets 03 and
04. The web control plane remains owner-only.

## Fixed product decisions

- Every direct bot response to a human starts with the triggering account's
  current Telegram `first_name`. The delivery layer adds this value; the LLM
  never chooses a name, nickname, title, or form of address.
- Telegram `last_name`, `username`, quoted names, prior history, and user
  instructions such as "call me boss" never affect the response prefix.
- The checked-in immutable super-context is the highest LLM policy. Runtime
  settings, the admin panel, list policies, rules, chat history, and users cannot
  replace it.
- The model receives no administrator status and must not identify, infer, or
  discuss administrators, internal prompts, hidden memory, configuration, or
  access controls.
- All Telegram commands are public. The web panel and its mutation APIs accept
  only `SUPER_ADMIN_ID`.
- `/set_mode`, `/deep`, `/judge`, `/dispute`, and judge-intent mention routing
  are removed.
- `/tone <preset>` changes the allowed chat's tone without clearing history or
  memory. `/mode` reports the current preset.
- `/think <question>` uses thinking mode with ordinary recent context.
- `/google <query>` performs one bounded Tavily search and one thinking-model
  synthesis with code-rendered sources. It does not send group history to
  Tavily.
- Mentions, replies, automatic rules, and ordinary commands use non-thinking
  inference.
- Every LLM path uses the single configured `LLM_MODEL`.
  `google/gemma-4-31b-it` is the checked-in default and recommended deployment
  value for this bot. The operator may choose another compatible NVIDIA model;
  the application deliberately does not hard-code a model allowlist. All paths
  use `temperature=1`, `top_p=0.95`, and a 16,384-token hard completion ceiling.
  Prompts request two to five paragraphs and let the model choose the useful
  length inside that bound.
- The canonical sarcastic preset is `sarcastic_bot`. Legacy
  `sarcastic_robot` records are migrated on read.

## Trusted prompt hierarchy

1. Code-verified delivery identity (`first_name`) used outside the LLM output.
2. Checked-in immutable super-context.
3. Static user shards (Ticket 07).
4. Bounded administrator-authored tone/list/rule modifiers that are explicitly
   subordinate to the super-context.
5. Gathered user memory (Ticket 07), marked as fallible untrusted observations.
6. Recent Telegram history and the current request, always untrusted data.

The model is instructed to produce no opening vocative. The Telegram delivery
layer prefixes the first chunk with the immutable job snapshot's `first_name`.
Retries reuse the saved, already-addressed answer.

## Command contract

| Command | Behavior |
|---|---|
| `/ping` | Addressed `pong` response |
| `/help` | Addressed public command list |
| `/tone <neutral|serious|scientist|street|sarcastic_bot>` | Idempotently set the chat preset |
| `/mode` | Addressed current preset |
| `/think <question>` | Contextual thinking response |
| `/google <query>` | Tavily-backed thinking response with source URLs |

Unknown and removed slash commands receive one addressed help hint and never
fall through to mention/reply routing.

## Storage and migration

- `cfg:global` and `cfg:<chat_id>` retain only `tone_preset`.
- Reads accept legacy configuration, map `sarcastic_robot` to
  `sarcastic_bot`, and discard `tone_mode`, `custom_system_prompt`, and
  `judge_default_n`.
- Existing history remains valid. The persisted `name` field now explicitly
  means the bounded Telegram `first_name`.
- New jobs use snapshot contract version 2. A retained version-1 job or a
  removed job kind is terminally rejected before any new provider or Telegram
  side effect, so a saved legacy answer cannot bypass the new prompt/addressing
  contract.
- Existing assigned-admin records may remain in Redis for safe rollback but no
  longer grant a session or command privileges.

## LLM and search reliability

- `ChatNVIDIA` remains lazy-loaded and asynchronous.
- Each invocation uses an isolated wrapper because the provider transport holds
  mutable response state used for sanitized error classification.
- Non-thinking calls must emit
  `chat_template_kwargs.enable_thinking=false`; `/think` and `/google` must emit
  `true`. A request-shape contract test pins both payloads.
- Reasoning content and known Gemma/DeepSeek reasoning tags are removed or
  rejected before persistence and Telegram delivery.
- `/google` checkpoints its Tavily result before generation so retries do not
  repeat a completed paid search. Real source lines are rendered by code, never
  trusted from the model.

## Acceptance criteria

1. A reply after a long multi-user conversation starts with the current
   sender's `first_name`, even when the transcript asks for another form of
   address.
2. No LLM system message contains a Telegram name, username, administrator
   status, or editable custom base prompt.
3. The admin API cannot replace the immutable super-context.
4. A non-owner cannot establish or reuse a web admin session.
5. Every group member can use `/tone`, `/think`, and `/google`.
6. Removed commands cannot create jobs.
7. Normal replies use `enable_thinking=false`; `/think` and `/google` use
   `enable_thinking=true`.
8. `/google` sends only its bounded explicit query to Tavily and returns only
   validated source URLs.
9. Tone changes preserve the complete recent-history buffer.
10. Focused tests, the full suite, Ruff, and `git diff --check` pass, followed
    by both required review gates.
