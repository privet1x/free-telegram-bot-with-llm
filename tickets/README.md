# Тикеты — Telegram-бот с LLM и Web Admin

План намеренно состоит из небольшого числа крупных E2E-тикетов. Общие
инварианты, схема Redis, state machine, prompt roles, env и privacy contract
находятся в [00-ARCHITECTURE.md](00-ARCHITECTURE.md).

## Порядок

| # | Тикет | Результат | Зависит |
|---|---|---|---|
| 00 | [Архитектура](00-ARCHITECTURE.md) | Единый технический/данный контракт | — |
| 01 | [Closed-chat ingestion](01-skeleton-webhook-history.md) | Vercel webhook, allowed chat, atomic history/edit upsert, observed users | — |
| 02 | [Durable QStash + Flash](02-qstash-llm-reply.md) | Snapshot/job state machine, mention/reply через `deepseek-ai/deepseek-v4-flash` | 01 |
| 03 | [Rules/lists/tone](03-rules-triggers-tone.md) | Детерминированные policies и unmentioned auto routing | 02 |
| 04 | [Judge + grounded facts](04-dispute-resolution-judge.md) | Admin-only verdict через `deepseek-ai/deepseek-v4-pro` + Tavily citations | 03 |
| 05 | [Web Admin](05-admin-panel.md) | Telegram OIDC, secure session, CRUD/UI и privacy purge | 04 |

```text
01 → 02 → 03 → 04 → 05
```

## Неподвижные решения

- Ровно один закрытый `TELEGRAM_ALLOWED_CHAT_ID`; чужие чаты не логируются.
- Основные ответы: NVIDIA NIM `deepseek-ai/deepseek-v4-flash`.
- `/judge` и явный admin-only `/deep`: NVIDIA NIM `deepseek-ai/deepseek-v4-pro`
  с текущим проверенным hosted-NIM non-thinking payload.
- Grounded fact-check: до 3 обезличенных Tavily basic searches; реальные sources
  прикладываются к verdict, а при недоступности поиска facts честно помечаются
  непроверенными.
- QStash получает только job ID. Private history/context snapshot остаётся в
  Redis; retries проходят через `received → enqueued → processing → delivered`
  и terminal failure states.
- История — максимум 30 записей, atomic upsert edits/outbound messages, sliding
  retention и per-record cutoff 30 дней; queued job использует snapshot на
  момент trigger, а privacy purge отменяет indexed jobs и удаляет их private
  snapshots.
- Web Admin использует современный Telegram OIDC Authorization Code + PKCE/state,
  не legacy Login Widget.
- Vercel worker `maxDuration` — 300 секунд; все Telegram LLM outputs plain text и
  делятся общим splitter до 4000 символов.

## Definition of done каждого тикета

1. Реализованы все contracts/ошибки/retention/security из архитектуры.
2. `ruff` и весь `pytest` suite зелёные в CI на Python 3.12; добавлены unit и
   внешние contract tests нового звена.
3. Пройдены live E2E criteria тикета на стабильном Vercel deployment с реальными
   Telegram, Upstash и применимыми provider credentials.
4. В репозитории нет секретов, а logs не содержат private transcript/prompts.
5. README/env/deployment instructions обновлены вместе с поведением; privacy
   notice опубликован до production acceptance.
