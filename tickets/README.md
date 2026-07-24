# Tickets — Telegram Bot with an LLM Admin Panel

This intentionally small plan uses a few large end-to-end tickets. The shared
invariants, Redis schema, state machine, prompt roles, environment variables,
and privacy contract are in [00-ARCHITECTURE.md](00-ARCHITECTURE.md).

## Delivery order

| # | Ticket | Outcome | Depends on |
|---|---|---|---|
| 00 | [Architecture](00-ARCHITECTURE.md) | One technical and data contract | — |
| 01 | [Closed-chat ingestion](01-skeleton-webhook-history.md) | Vercel webhook, allowed-chat gate, atomic history/edit upsert, observed users | — |
| 02 | [Durable QStash + Flash](02-qstash-llm-reply.md) | Historical snapshot/job state-machine delivery | 01 |
| 03 | [Rules, lists, and tone](03-rules-triggers-tone.md) | Deterministic policies and unmentioned automatic routing | 02 |
| 04 | [Judge and grounded facts](04-dispute-resolution-judge.md) | Historical judge workflow, superseded by Ticket 06 | 03 |
| 05 | [Web admin](05-admin-panel.md) | Telegram OIDC, secure session, CRUD/UI, and privacy purge | 04 |
| 06 | [Trusted conversation and public commands](06-trusted-conversation-and-public-commands.md) | Deterministic first-name addressing, immutable super-context, separate fast text and vision models, `/think`, and `/google` | 05 |
| 07 | Per-user memory and `/lobotomy` | Immutable participant shards, bounded gathered observations, epoch-safe mutable-memory reset | 06 |
| 08 | Always-on reactions and scheduled banter | Hard-coded Russian keyword reactions and authenticated twenty-minute scheduled messages | 07 |

```text
01 → 02 → 03 → 04 → 05 → 06 → 07 → 08
```

## Fixed decisions

- The bot serves exactly one private `TELEGRAM_ALLOWED_CHAT_ID`; other chats are
  acknowledged but never persisted.
- Text replies use `LLM_MODEL` and image/OCR uses `LLM_MODEL_VISION`. Defaults are
  `deepseek-ai/deepseek-v4-flash` and `google/gemma-4-31b-it`. Ordinary replies
  are non-thinking; public `/think`, `/google`, and scheduled banter enable model
  thinking.
- `/google` sends only the bounded query that the participant explicitly placed
  in that command to one Tavily basic search. Real source URLs are rendered by
  code; unavailable search is disclosed.
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
2. All earlier-ticket regression tests, lint, and CI are green.
3. Its live E2E criteria pass on a stable Vercel deployment with real
   Telegram/Upstash/provider integration.
4. Secrets and raw chat data do not appear in logs or diagnostics.
5. README, environment template, deployment instructions, and the privacy notice
   are updated with the feature.

Tickets 01–08 have local implementation and automated checks. The participant
manifest is intentionally empty until the owner supplies the fixed Telegram IDs
and shard content. All live
Vercel/Telegram/Upstash/QStash/NVIDIA/Tavily/OIDC acceptance checks remain
pending authorized deployment and provider interaction.

Ticket 06 supersedes the conversational command/model/prompt portions of
Tickets 03 and 04. Those tickets remain as historical delivery records.
