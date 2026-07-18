# TICKET-03 — Deterministic Rules, Lists, Automatic Triggers, and Tone Control

**Size:** L · **Depends on:** 02 · **Unblocks:** 04
**Shared contract:** 00-ARCHITECTURE.md sections 4 and 6

**Status:** Local implementation and automated checks are complete. Live
provider/Vercel acceptance remains pending authorized deployment.

## Goal

Make bot behaviour data-driven: built-in and custom tone, personal policies
through lists, text rules, and automatic intervention without a bot mention.
This ticket implements prompt layers 1–4. The judge-specific layer remains in
Ticket 04.

The core end-to-end proof is that an ordinary message containing a configured
keyword, without a mention, reaches the processor. Matching and routing must
therefore occur in the webhook, not only in a worker that never receives that
message.

## Canonical models

### Tone configuration

~~~json
{
  "tone_mode": "preset",
  "tone_preset": "neutral",
  "custom_system_prompt": null,
  "judge_default_n": 20
}
~~~

- tone_mode is preset or custom.
- Canonical built-in presets are neutral, serious, scientist, street, and
  sarcastic_robot. Serious exists specifically for the required
  /set_mode serious command.
- In custom mode, a non-empty custom_system_prompt replaces base preset text.
  In preset mode, custom text remains saved but inactive.
- cfg:<allowed_chat_id> overrides cfg:global field by field. Empty or unknown
  values fail validation rather than creating a hidden preset.

### Personal lists

~~~json
{
  "slug": "aggressive",
  "title": "Sarcastic response",
  "enabled": true,
  "priority": 50,
  "applies_to": ["explicit", "auto", "judge"],
  "injected_prompt": "Use dry sarcasm without personal attacks."
}
~~~

- Membership always uses numeric Telegram user_id.
- A personal policy is expressed by a list plus injected_prompt. There is no
  separate rule.kind="personal".
- The reserved ignore list cannot be removed or renamed. It injects no prompt and
  suppresses only automatic replies; explicit mention/reply remains available.

### Text rules

~~~json
{
  "id": "nonsense",
  "enabled": true,
  "priority": 50,
  "scope": "all",
  "match": {"type": "substring", "value": "nonsense"},
  "instruction": "Explain calmly why the argument is not nonsense.",
  "stop_processing": false
}
~~~

- scope is auto, explicit, judge, or all. auto/all may create an unmentioned
  job; explicit/all add policy to explicit Flash work; judge/all are snapshotted
  by Ticket 04.
- Match types are substring, word, and phrase. Regex is out of scope.
- Normalize for matching as NFKC, casefold, and punctuation/whitespace
  normalization. A word matches a full Unicode token; phrase preserves normalized
  word boundaries; substring is intentionally appropriate for word roots.
- Require non-empty match value and instruction, bounded lengths, priority in
  [-1000,1000], and safe IDs/slugs. Never mutate original text sent to the LLM.

## Determinism and conflicts

1. Sort matched rules by priority DESC, id ASC.
2. Process rules in equal-priority groups, each in id ASC. Include the complete
   current group; if any member has stop_processing=true, discard all
   lower-priority groups.
3. Sort active member lists by priority DESC, slug ASC.
4. Apply base tone, trusted numeric actor ID and computed admin state, list
   policies, then matched rules. Telegram name/username and raw messages appear
   only in the final untrusted data block.
5. Cap one job at 10 list policies and 10 rule policies after sorting. Record a
   metric when the cap is exceeded; never create unbounded prompt growth.
6. At enqueue, save effective_policy: selected tone text, computed is_admin, list
   policies, and rule snapshots. Later Redis changes affect later jobs only.

## Scope

### 1. Stores and validation

- [ ] Add app/store/config_store.py with global/chat merge, get/set for tone
  mode, preset, custom prompt, atomic/optimistic validation, and atomic chat
  override clear. A mutation must explicitly choose scope="global" or
  scope="chat"; reads return global, override, and effective config.
- [ ] Add app/store/lists.py for list metadata and membership CRUD,
  member_lists(user_id, kind), deterministic sort, and reserved ignore handling.
  Do not leave orphan keys or index members.
- [ ] Add app/store/rules.py for CRUD, normalization/matching, deterministic
  resolution, and scope filtering. Do not leave orphan keys or index members.
- [ ] Extend the Ticket 02 admin store with list/add/remove. SUPER_ADMIN_ID is
  always admin and cannot be removed.
- [ ] Bound administrator-authored prompts/instructions and return a field-level
  validation error when input is invalid.

### 2. Required webhook routing extension

After Ticket 02, routing becomes:

1. After secret/allowed-chat gating, an edited_message only upserts history and
   never runs command, explicit, or automatic routing twice.
2. For a new message, detect command/route and take a durable pre-trigger
   snapshot using Ticket 02 helpers. Command text does not run automatic rules.
3. For explicit mention/reply, match rules with explicit/all scope, build
   effective_policy, and create an ordinary reply job. ignore and auto cooldown
   never suppress explicit work.
4. For ordinary text, match auto/all rules. With no match, only write history.
   If the author belongs to ignore, only write history.
5. Before new matching, load job:<update_id>. An existing received auto job
   completes history/publication and cannot be suppressed by a new cooldown or
   changed configuration.
6. For a new automatic route, one Lua/CAS operation checks no cooldown, writes
   owner value {update_id}:{random_token} with TTL, and creates the durable
   received job with snapshot. There must be no crash window where cooldown
   exists without a job.
7. Then upsert history/user and publish under the QStash deduplication ID. A known
   publish failure does **not** remove cooldown: received remains its owner,
   Telegram retry finds it and retries publication, and new automatic updates
   stay suppressed until expiry. Only compare-delete by the owner token in the
   same atomic operation that cancels/removes the job.

Webhook matching is inexpensive Redis/CPU work; it never calls an LLM. The
processor executes the stored route and policy without deciding whether to
respond again. This avoids configuration drift.

### 3. Prompt builder and processor

- [ ] Extend build_reply_messages:
  - layer 1: effective preset or custom base;
  - layer 2: trusted numeric actor ID and computed is_admin;
  - layer 3: sorted list instructions;
  - layer 4: sorted matched-rule instructions;
  - final user/data: Telegram name/username, transcript, reply target, and
    current message.
- [ ] Serialize every Telegram name/username/text as data. A transcript instruction
  such as “ignore previous instructions” cannot alter trusted policy.
- [ ] Process reply and auto_rule through the same Flash client and Ticket 02
  delivery/split/retry path.

### 4. Chat commands

Only an admin or super-admin in the allowed group may run these:

- [ ] /tone <neutral|serious|scientist|street|sarcastic_robot> and
  /set_mode <slug> select a preset for the current allowed chat and set
  tone_mode=preset without deleting saved custom text.
- [ ] /tone global <slug> and /set_mode global <slug> update cfg:global.
  /tone clear removes the chat override and returns to global configuration.
- [ ] The ergonomic command alias sarcastic canonicalises to sarcastic_robot.
  Thus /tone sarcastic and /set_mode serious both work, while config/API store
  canonical values only.
- [ ] /mode reports active mode/preset and permitted slugs.
- [ ] A command suffix executes only when it identifies this bot.
- [ ] Non-admin gets a short refusal; invalid slug gets usage plus allowed slugs.
- [ ] The command path is idempotent. After user/history upsert, one Lua/CAS
  create-if-absent operation writes cmd:<update_id> for 30 days, applies a typed
  canonical configuration value, and writes final dedup:update. Only the winner
  returns a short webhook reply. A retry with a command receipt changes nothing
  and sends no second reply. A crash before the operation changes nothing; a
  crash after it cannot roll back a newer UI/chat setting.
- [ ] A Telegram webhook-body reply has no Bot API message_id, so it is not added
  to canonical history. History contains only outbound sendMessage or
  editMessageText results with a real ID.

### 5. Runtime configuration

- [ ] Add AUTO_TRIGGER_COOLDOWN_SECONDS plus
  MAX_LIST_POLICIES=10/MAX_RULE_POLICIES=10 to settings and .env.example.
  Production validation requires positive limits and the Ticket 02 queue
  dependencies when automatic routing is enabled. Health is not ready if auto
  routing is enabled without its queue/config dependency.

### 6. Seed data

- [ ] Add an idempotent scripts/seed.py that creates reserved ignore, a demo
  aggressive list, and demo rule nonsense with scope="all". It never overwrites
  administrator changes without explicit --force.
- [ ] The script operates only on the configured allowed-chat context, returns
  non-zero on Redis/API failure, and never prints secrets.

## Failure and limit policy

- Failure to load rules/config for a potential automatic trigger is retryable,
  not a silent “do nothing.”
- Cooldown applies only to automatic work and is atomically created with the job
  before publication. Publication failure retains both resumable job and blocker
  until expiry. Only a retry of that same update bypasses its owner cooldown.
  A suppressed update still writes history and final dedup.
- Rule/list policy is trusted only because mutation will be admin-only. Before
  Ticket 05, change it only through seed/manual operations.
- Too many matches still use the documented deterministic cap, never Redis's
  arbitrary set order.

## Out of scope

- Judge layer, grounded facts, and Pro: Ticket 04.
- Web CRUD/UI and username resolution UI: Ticket 05.

## Automated checks

- [ ] All match types, Unicode NFKC/casefold, punctuation, and false positives.
- [ ] Deterministic rule/list ordering, equal priorities, stop processing, and
  policy caps.
- [ ] Scope matrix auto/explicit/judge/all, ignore semantics, and trusted admin
  status.
- [ ] Unmentioned automatic message creates a job; no-match does not; command
  never starts a rule; mention receives explicit/all policy; edit creates no job.
- [ ] Parallel automatic cooldown race, atomic gate/job invariant, publication
  failure retains owner blocker, received-job retry bypasses it, and a different
  update is suppressed.
- [ ] Tone commands: admin/non-admin, foreign suffix, invalid slug,
  sarcastic→sarcastic_robot, serious, duplicate/concurrent calls, crash
  boundaries, global/chat/clear scope, and retained custom text.
- [ ] Prompt-role snapshot proves raw transcript is absent from system content.
- [ ] Ticket 01–02 tests, Ruff, and Python 3.12 CI stay green.

## Live E2E acceptance

1. A non-ignored user writes “this is nonsense” without a mention. Webhook creates
   an auto_rule job and the bot replies with the rule instruction.
2. Two equal-priority rules produce the same order on repeated tests, and
   stop_processing discards exactly the expected lower-priority rules.
3. A member of aggressive gets its policy; another user does not. A member of
   ignore gets no automatic response but can still receive an explicit @bot reply.
4. An admin runs /tone scientist, /tone sarcastic, /set_mode serious,
   /tone global street, then /tone clear. Alias canonicalisation and
   chat/global precedence are visible and durable.
5. A non-admin cannot change tone. The prompt contract includes computed admin
   state while raw chat remains user/data content.
6. A burst of automatic matches produces at most one job per cooldown window,
   while an explicit mention still receives a reply.

## Risks

- Automatic replies consume NIM/QStash quota, so cap/cooldown/ignore are required.
- substring intentionally has broad root matching; the UI must explain its
  difference from word and phrase.
- Administrator-authored policy has high priority and can be dangerous. Bound its
  size, validate types, limit mutation to admins, and recheck role/session
  immediately in Ticket 05.
