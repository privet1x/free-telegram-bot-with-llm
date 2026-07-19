# Tickets — Telegram Bot with an LLM Admin Panel

This intentionally small plan uses a few large end-to-end tickets. The shared
invariants, Redis schema, state machine, prompt roles, environment variables,
and privacy contract are in [00-ARCHITECTURE.md](00-ARCHITECTURE.md).

## Delivery order

| # | Ticket | Outcome | Depends on |
|---|---|---|---|
| 00 | [Architecture](00-ARCHITECTURE.md) | One technical and data contract | — |
| 01 | [Closed-chat ingestion](01-skeleton-webhook-history.md) | Vercel webhook, allowed-chat gate, atomic history/edit upsert, observed users | — |
| 02 | [Durable QStash + Flash](02-qstash-llm-reply.md) | Snapshot/job state machine and mention/reply via `deepseek-ai/deepseek-v4-flash` | 01 |
| 03 | [Rules, lists, and tone](03-rules-triggers-tone.md) | Deterministic policies and unmentioned automatic routing | 02 |
| 04 | [Judge and grounded facts](04-dispute-resolution-judge.md) | Admin-only verdict through `deepseek-ai/deepseek-v4-pro` and Tavily citations | 03 |
| 05 | [Web admin](05-admin-panel.md) | Telegram OIDC, secure session, CRUD/UI, and privacy purge | 04 |

```text
01 → 02 → 03 → 04 → 05
```

## Fixed decisions

- The bot serves exactly one private `TELEGRAM_ALLOWED_CHAT_ID`; other chats are
  acknowledged but never persisted.
- Ordinary replies use NVIDIA NIM `deepseek-ai/deepseek-v4-flash`.
- `/judge` and explicit admin-only `/deep` use
  `deepseek-ai/deepseek-v4-pro` with the verified hosted-NIM non-thinking
  payload.
- Grounded fact checks use at most three de-identified Tavily basic searches.
  Real sources are attached to a verdict; when search is unavailable, facts are
  explicitly marked unverified.
- QStash receives only a job ID. Private history and the context snapshot stay
  in Redis. Retries follow a durable state machine with terminal failure states.
- History contains at most 30 records, supports atomic edits and outbound
  messages, uses a sliding retention TTL plus a per-record cutoff, and is
  snapshotted at trigger time. Privacy purge cancels indexed jobs and deletes
  their private snapshots.
- The web admin uses Telegram OIDC Authorization Code Flow with PKCE/state, not
  the legacy Login Widget.
- Vercel workers have `maxDuration=300`; LLM output is plain text and is split
  into chunks no longer than 4,000 UTF-16 code units.

## Completion definition

A ticket is complete only when:

1. Its scope and automated checks are implemented.
2. Ticket 01–05 regression tests, lint, and CI are green.
3. Its live E2E criteria pass on a stable Vercel deployment with real
   Telegram/Upstash/provider integration.
4. Secrets and raw chat data do not appear in logs or diagnostics.
5. README, environment template, deployment instructions, and the privacy notice
   are updated with the feature.

Tickets 01–05 have completed implementation, automated checks, and required
local review gates. Ticket 05 shipped in commit `b26ced7`. All live
Vercel/Telegram/Upstash/QStash/NVIDIA/Tavily/OIDC acceptance checks remain
pending authorized deployment and provider interaction.
