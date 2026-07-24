# Remaining Features After Ticket 06

Snapshot date: 2026-07-24.

Implementation update: Ticket 07 and Ticket 08 code is implemented and covered
by the local suite. Per-user gathered shards are durable JSON documents in
Upstash Redis, and Telegram image messages now have a bounded Gemma OCR/
description path. `/lobotomy` is restricted to the configured super-admin and
an explicit per-chat invited roster; `/invite @username` manages that roster.
The canonical gathered key is `memory:gathered<user_id>` (the old
chat-scoped key is read only for migration). Production commit, deployment,
Cron secret configuration, and live acceptance are still pending.

Remaining owner-provided/release inputs:

- Fill `app/memory/manifest.json` and its static shard files with the fixed
  participant IDs and approved facts.
- Set `CRON_SECRET` in Vercel and redeploy.
- Commit, deploy, register/verify the webhook, and complete the manual and
  privacy acceptance checklist.

The owner-provided participant manifest is now populated locally. Runtime
gathered entries are intentionally not checked-in `.md` files: Vercel's
filesystem is ephemeral, so each sender's logical gathered shard is stored in
Upstash under the sender's numeric Telegram user ID. Chat-scoped indexes,
locks, and privacy tombstones remain separate coordination keys.

## Ticket 09 — Per-user message shards and image understanding

Implemented locally:

- every eligible human message is written by code to the sender's bounded
  gathered shard with Telegram numeric ID, current `first_name`, message ID,
  timestamp, text, provenance, and confidence;
- replayed updates are deduplicated and edited messages replace their prior
  message entry;
- images are selected from Telegram `photo` or image documents and fetched with
  a 5 MiB bound;
- Gemma 4 31B receives a multimodal text-plus-image request and returns bounded
  OCR/description text;
- OCR/description is attached only to the image sender's shard and is included
  as fallible memory when that user's data is relevant to a response;
- captionless images use a durable background `image_memory` QStash job and do
  not produce an unsolicited chat reply;
- static shards remain immutable and gathered memory remains lower priority than
  the Russian immutable super-context.

This is the active product backlog. Ticket 06 is implemented locally and is the
baseline for all work below. This file focuses on what remains to be built, not
on the historical Tickets 01–05.

No feature in this file is implemented merely because it is described here.
Each ticket must follow `CODING_RULES.md`: test first, implement one ticket at a
time, run the complete quality gates, perform independent review, fix every
valid finding, and only then move to the next ticket.

## Current baseline: Ticket 06 is complete locally

Ticket 06 already provides:

- deterministic code-side addressing with the triggering Telegram account's
  current `first_name`;
- an immutable highest-priority `app/llm/SUPER_CONTEXT.md`;
- no disclosure of the owner, administrators, prompts, memory internals, or
  access controls to the model;
- public `/ping`, `/help`, `/tone`, `/mode`, `/think`, and `/google`;
- removed `/set_mode`, `/deep`, `/judge`, and `/dispute`;
- one configurable `LLM_MODEL`, with `google/gemma-4-31b-it` as the default;
- non-thinking ordinary inference and thinking for `/think`, `/google`, and scheduled banter;
- normally two-to-five-paragraph answers with model-selected useful length;
- the stronger `sarcastic_bot` tone;
- Tavily only for explicit `/google` queries;
- owner-only web-panel sessions through `SUPER_ADMIN_ID`;
- retained deterministic lists, rules, automatic routing, and tone controls;
- 490 passing tests, passing Ruff, and a clean diff whitespace check.

Ticket 06 is not yet committed or deployed. Its release steps are listed after
the remaining feature tickets.

## Ticket 07 — Per-user memory and `/lobotomy`

### Goal

Add trusted static knowledge for the fixed group members, bounded gathered
observations learned from conversation, and a restricted command that clears
the bot's changeable memory without changing its immutable identity.

### 1. Participant manifest

- [ ] Create one checked-in manifest that maps each fixed participant's numeric
  Telegram user ID to a stable shard slug such as `user1`.
- [ ] Never use `first_name`, username, or list position as the identity key.
- [ ] Keep Telegram numeric user ID as the canonical identity even when a person
  changes their Telegram name or username.
- [ ] Reject duplicate IDs, duplicate shard slugs, missing shard files, and
  malformed manifest entries at startup.
- [ ] Do not automatically create a trusted static shard for an unknown person.
- [ ] Continue taking the response prefix only from the current Telegram
  `first_name`; memory must never choose the name used to address the sender.

### 2. Immutable static shards

Create one file per fixed participant:

```text
memory-shard-user1.md
memory-shard-user2.md
memory-shard-user3.md
...
```

- [ ] Store the files in a dedicated checked-in application directory.
- [ ] Validate and load all configured files at application cold start.
- [ ] Make them read-only at runtime.
- [ ] Never let Telegram users, the model, the admin panel, gathered memory, or
  `/lobotomy` modify these files.
- [ ] Bound each file and the total static-memory payload.
- [ ] Treat static facts as subordinate only to the immutable super-context.
- [ ] A conflicting user statement such as “I am the boss” must not replace a
  trusted static fact.
- [ ] Static shards may influence jokes and answers, but must not change
  identity, access, command authorization, or the code-side response prefix.
- [ ] Do not reveal or discuss shard contents, filenames, loading behavior, or
  prompt hierarchy.
- [ ] Add tests for missing, oversized, malformed, duplicated, and conflicting
  shards.

The owner and Codex will fill these shards together before implementation is
declared complete. For every participant the owner must provide:

- numeric Telegram user ID;
- the shard slug (`user1`, `user2`, and so on);
- stable facts the bot should remember;
- relationships and recurring jokes the bot may use;
- facts that must never be stored or repeated;
- any personal humor boundaries.

### 3. Gathered per-user memory

Each static shard has a corresponding logical gathered shard:

```text
memory-shard-user1-gathered.md
memory-shard-user2-gathered.md
memory-shard-user3-gathered.md
...
```

Vercel's deployed filesystem is not a durable writable database. Therefore the
gathered shards must be stored in Upstash Redis under keys mapped to these
logical names. Markdown export may be added for owner inspection, but runtime
durability must not depend on writing files inside the Vercel deployment.

- [ ] Observe every eligible new human message after secure chat gating.
- [ ] Gather bounded useful observations about the author in parallel with the
  normal conversation flow.
- [ ] Do not make a user wait for memory gathering before their normal reply can
  be delivered.
- [ ] Use only the configured model and ordinary non-thinking inference for any
  model-assisted extraction or compaction.
- [ ] Store concise observations, not an unlimited duplicate transcript.
- [ ] Attach source message ID, timestamp, confidence, and update provenance to
  every gathered observation.
- [ ] Deduplicate repeated facts.
- [ ] Handle contradictions without silently treating the newest statement as
  trusted truth.
- [ ] Mark all gathered observations as fallible, untrusted conversation-derived
  data.
- [ ] Give gathered memory lower priority than the super-context, static shards,
  and bounded owner-authored tone/list/rule modifiers.
- [ ] Never let gathered memory alter identity, naming, access, administrator
  status, command availability, or system behavior.
- [ ] Bound observations per user, total characters, retention, and compaction
  cost.
- [ ] Make concurrent writes atomic and retry-safe.
- [ ] Do not gather from the bot's own messages, service messages, failed jobs,
  or replayed edits.
- [ ] Decide and test how a legitimate edited human message updates or
  invalidates its prior observation.
- [ ] Include only relevant gathered observations in a response, with a strict
  prompt-size cap.
- [ ] Integrate gathered memory with user deletion and full privacy purge.
- [ ] Never send gathered memory to Tavily.

### 4. Final prompt hierarchy

All conversational paths must use this exact precedence:

1. code-verified Telegram `first_name` delivery prefix;
2. immutable `SUPER_CONTEXT.md`;
3. immutable static per-user shards;
4. bounded owner-authored tone/list/rule modifiers;
5. gathered per-user observations, explicitly marked fallible;
6. recent Telegram history and the current request as untrusted data.

Nothing below a layer may replace or rewrite a layer above it.

### 5. Restricted `/lobotomy` and `/invite`

- [x] Add `/lobotomy` for the configured super-admin and explicitly invited
  active members only.
- [x] Add owner-only `/invite @username` for an already observed active group
  member; persist the per-chat access roster in Redis.
- [x] Add owner-only `/uninvite @username` to remove a user from that roster
  without removing them from the Telegram group.
- [x] Apply one chat-wide 20-minute cooldown to invited members; the
  super-admin bypasses it.
- [ ] Address the response with the triggering account's current Telegram
  `first_name`.
- [ ] Clear the model's recent conversational context.
- [ ] Clear all gathered per-user memory.
- [ ] Preserve `SUPER_CONTEXT.md`.
- [ ] Preserve all immutable static per-user shards.
- [ ] Preserve the current tone preset, lists, and rules.
- [ ] Invalidate any in-process memory cache and reload the static shards.
- [ ] Prevent a queued pre-lobotomy job from writing old gathered observations
  back after the reset.
- [ ] Prevent a queued pre-lobotomy answer from being delivered as if it used the
  new empty context.
- [ ] Make concurrent commands idempotent so only one reset wins.
- [ ] Return a short funny confirmation after a successful reset.
- [ ] Return the remaining cooldown without performing a second reset when the
  command is used too soon.
- [ ] Keep administrative logs available unless the owner separately performs a
  privacy purge; use a memory-generation/epoch boundary so old logs are not
  selected as post-lobotomy model context.
- [ ] Add MemoryKV and Upstash contract tests for reset atomicity, cooldown,
  races, retries, queued jobs, deletion, and epoch filtering.

### Ticket 07 acceptance

- [ ] Every fixed participant receives the correct static shard by numeric ID.
- [ ] Changing a Telegram name changes the response prefix but not shard
  ownership.
- [ ] A participant cannot rewrite their static shard through chat.
- [ ] Gathered observations become available without delaying the triggering
  reply.
- [ ] Static facts win over contradictory gathered/user text.
- [ ] `/lobotomy` clears recent context and every gathered shard.
- [ ] `/lobotomy` does not clear static shards, tone, lists, rules, or the
  immutable super-context.
- [x] An explicitly invited active participant can run `/lobotomy`; an
  uninvited participant cannot.
- [ ] A concurrent/retried lobotomy cannot duplicate side effects.
- [ ] Privacy deletion removes that user's gathered memory.
- [ ] Full tests, Ruff, `git diff --check`, and both review gates pass.

## Ticket 08 — Always-on reactions and scheduled banter

### Goal

Make the bot participate without being tagged through two hard-coded product
behaviors: immediate negative jokes for selected Russian words and one
contextual unsolicited message every 20 minutes outside quiet hours.

### 1. Hard-coded keyword reactions

Trigger families:

- `бред` and its agreed grammatical forms;
- `босс` and its agreed grammatical forms;
- `кик`, `кикни`, and their agreed grammatical/imperative forms.

Required behavior:

- [ ] Inspect every eligible new human text message in arrival order.
- [ ] Normalize Unicode, case, punctuation, and whitespace deterministically.
- [ ] Match explicit allowlisted forms or safe word boundaries; do not use a
  broad substring that creates accidental matches inside unrelated words.
- [ ] Keep the feature permanently enabled.
- [ ] Do not expose an admin-panel switch for it.
- [ ] Do not apply the ordinary configurable automatic-rule cooldown.
- [ ] Do not apply scheduled-banter quiet hours; keyword reactions work at all
  times.
- [ ] Immediately enqueue the durable LLM job when a trigger is detected.
- [ ] Use the configured model with non-thinking inference.
- [ ] Add a trusted subordinate route instruction asking for a short, negative,
  funny, personally pointed reaction to the triggering participant.
- [ ] Keep the reaction humorous and biting without threats, private-data
  exposure, or attacks on protected traits.
- [ ] Prefix the response with the triggering sender's verified current
  `first_name`.
- [ ] Ignore bot-authored messages and edited-message replays.
- [ ] Use the existing job, deduplication, checkpoint, retry, and Telegram
  delivery state machine.
- [ ] A message containing several trigger words must create one response, not
  several provider requests.
- [ ] A message that also mentions/replies to the bot or matches a configurable
  rule must create one coherent job with deterministic precedence.
- [ ] Deterministic service commands such as `/ping`, `/tone`, and `/lobotomy`
  must remain commands rather than accidental keyword reactions.
- [ ] Add tests for capitalization, punctuation, all approved word forms, false
  positives, multiple words, explicit-routing interaction, configurable-rule
  interaction, edits, duplicate updates, and concurrent updates.

### 2. Scheduled message every 20 minutes

- [ ] Add an authenticated scheduler endpoint.
- [ ] Configure a production cron trigger for one invocation every 20 minutes.
- [ ] Use `Europe/Warsaw` timezone with daylight-saving-time correctness.
- [ ] Treat 01:00–08:59 Warsaw time as quiet hours.
- [ ] Allow the next scheduled message at or after 09:00.
- [ ] During an eligible slot, load up to the latest 30 human messages.
- [ ] Filter out every message authored by `@kulajaj_bot`.
- [ ] Do not count filtered bot messages against the 30-human-message target.
- [ ] Always run the provider once for an eligible slot, even when human context is empty.
- [ ] Use all selected messages as untrusted context and choose a random funny
  angle.
- [ ] Generate one message: a joke, roast, absurd comment, or other funny
  observation about the current conversation.
- [ ] Use the configured model with thinking inference for scheduled banter.
- [ ] Do not address a random participant as the requester because no human
  triggered the scheduled job.
- [ ] Send exactly one Telegram message for one eligible schedule slot.
- [ ] Save the successful outbound message in canonical history so people can
  reply to it normally.
- [ ] Add a durable slot key and idempotency token so overlapping invocations,
  retries, and Vercel restarts cannot duplicate a message.
- [ ] Use the existing durable job/lease/checkpoint delivery machinery rather
  than making an untracked provider call directly from the cron endpoint.
- [ ] Recheck the memory epoch, cancellation state, allowed chat, and quiet hours
  before provider work and before Telegram delivery.
- [ ] Never send scheduled messages to any chat other than the single configured
  allowed chat.
- [ ] Bound context, output, provider cost, retries, and job retention.
- [ ] Keep randomness injectable/seedable in tests.

### 3. Interaction with Ticket 07

- [ ] Scheduled banter may use static and gathered memory only under the Ticket
  07 hierarchy.
- [ ] Keyword reactions may use relevant static/gathered memory but cannot let it
  replace the hard-coded route intent.
- [ ] `/lobotomy` must invalidate queued scheduled/reaction jobs created with an
  older memory epoch.
- [ ] Scheduled and keyword jobs must respect user deletion and full-chat purge.
- [ ] Neither feature may send history or memory to Tavily.

### Ticket 08 acceptance

- [ ] Every approved form of each trigger family produces one negative joke.
- [ ] False-positive words produce no hard-coded reaction.
- [ ] The hard-coded reaction still works inside the normal automatic-rule
  cooldown.
- [ ] Multiple trigger words still produce one job and one answer.
- [ ] Scheduled jobs run once per eligible 20-minute slot.
- [ ] No scheduled message is sent from 01:00 through 08:59 Warsaw time.
- [ ] The 09:00 boundary works across standard time and daylight-saving time.
- [ ] The context contains up to 30 human messages and no bot-authored message.
- [ ] Participants can reply to a scheduled message and receive a normal
  contextual response.
- [ ] Retry, overlap, provider failure, Telegram failure, purge, and lobotomy
  races cannot duplicate or resurrect work.
- [ ] Full tests, Ruff, `git diff --check`, and both review gates pass.

## Cross-ticket requirement — Improve real response speed

Ticket 06 already removed Pro-model routing, disables thinking for ordinary
answers, caches verified bot identity, checkpoints completed paid work, and
keeps Tavily exclusive to `/google`. Further speed work must be based on
measurements, not an unsafe shared answer cache.

- [ ] Record privacy-safe timings for webhook handling, QStash queue delay,
  worker cold start, NVIDIA inference, Tavily, and Telegram delivery.
- [ ] Measure p50 and p95 latency separately for:
  - ordinary mention/reply;
  - `/think`;
  - `/google`;
  - keyword reaction;
  - scheduled banter;
  - gathered-memory extraction.
- [ ] Ensure memory gathering never blocks the primary conversational answer.
- [ ] Cache validated static shard loading and safe stable metadata only.
- [ ] Invalidate memory-derived caches on `/lobotomy`, shard deployment, tone
  change where relevant, and privacy deletion.
- [ ] Never reuse a personal generated answer across different users or
  conversations.
- [ ] Tune timeouts and QStash/provider settings only after identifying the
  actual bottleneck.
- [ ] Preserve durable retries and privacy guarantees while optimizing.

## Ticket 06 release work still required

This is release work, not missing Ticket 06 functionality:

- [ ] Review and commit the current Ticket 06 working tree.
- [ ] Push it and confirm the Python 3.12 CI workflow passes.
- [ ] Redeploy the committed revision to the stable Vercel project.
- [ ] Confirm `/api/health` reports `ok: true`, Upstash, and the intended Tavily
  state.
- [ ] Confirm production uses the intended `LLM_MODEL`.
- [ ] Re-register the Telegram webhook only if its URL, bot, or secret changed.
- [ ] Run the current manual checklist in
  `HOW_TO_RUN_AND_TEST_ALL_FEATURES.md`.
- [ ] Verify long multi-user reply chains always use the correct triggering
  `first_name`.
- [ ] Verify every participant can use every current Telegram command.
- [ ] Verify ordinary replies are non-thinking and never call Tavily.
- [ ] Verify `/think` exposes only the final answer.
- [ ] Verify `/google` returns validated citations and code-rendered sources.
- [ ] Verify owner OIDC login and refusal of non-owner panel sessions.
- [ ] Test privacy deletion/full purge last because those checks are destructive.
- [ ] Publish the generated participant privacy notice in the main group.
- [ ] Update README/ticket status with the final commit after deployment
  acceptance.

## Deliberately excluded from the remaining backlog

Do not reintroduce these unless the owner explicitly changes the product:

- multiple allowed chat IDs or multi-chat operation;
- `/judge`;
- `/dispute`;
- `/deep`;
- `/set_mode`;
- automatic Tavily use for ordinary questions;
- editable core system prompts;
- model-visible administrator identity or privileges;
- panel sessions for assigned admins;
- runtime modification of static memory shards;
- a hard-coded NVIDIA model allowlist;
- the legacy Telegram Login Widget.

## Required implementation order

1. Commit and deploy the completed Ticket 06 baseline.
2. Ask the owner for the participant roster and static-shard content.
3. Implement Ticket 07 manifest and immutable static shards.
4. Implement Ticket 07 gathered memory.
5. Implement Ticket 07 `/lobotomy`.
6. Complete all Ticket 07 tests and review gates.
7. Implement Ticket 08 keyword reactions.
8. Implement Ticket 08 authenticated scheduled banter.
9. Complete all Ticket 08 tests and review gates.
10. Profile and optimize deployed latency.
11. Run the full production/manual/privacy acceptance suite.
