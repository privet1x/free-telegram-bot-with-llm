# United Deep Code Review — Consolidated from three independent reviews

Date: 2026-07-19

This file is the deduplicated union of three independent code reviews of the
same implementation (tickets 01–05, commits `fb3eb0e`…`9b20d38`):

| Source report | Model | Findings |
|---|---|---|
| `CODEX_CODE_REVIEW_DONE.md` | Codex | CR-01 … CR-33 (33) |
| `FABLE_CODE_REVIEW_DONE.md` | Claude Fable 5 | F1 … F21 + L1 … L14 (35) |
| `SOL_CODE_REVIEW_DONE.md` | SOL | SOL-01 … SOL-35 (35) |

**Merge method.** All 103 original findings were mapped by root cause. Findings
describing the same defect (even when scoped or titled differently) were merged
into one unified finding; findings that bundled several root causes (e.g.
F17 ≈ CR-15+16+17+18) were split so each unified finding has exactly one root
cause. Every original ID appears exactly once in the traceability matrix at the
end — nothing was dropped.

**Result: 47 unique findings — 3 Critical, 17 High, 18 Medium, 9 Low.**

Each finding lists:
- **Found by** — which of the three reviews identified it (consensus 3/3, 2/3, or 1/3).
- **Sources** — the original finding IDs it merges.
- **Severity** — the consolidated rating; where the reviews disagreed, the
  individual votes are shown and the majority (or the best-argued position)
  was taken.
- **[reproduced]** — at least one review demonstrated the defect empirically,
  not just by inspection.

---

## Critical findings

### U-01 — Telegram OIDC login can never succeed: state validation always fails [reproduced ×3]

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-02, F1, SOL-01

`create_state()` serializes the one-time login transaction as a colon-joined
string `{handle_hash}:{verifier}:{redirect_uri}:{nonce}`
(`app/auth/telegram_oidc.py:26-30`); `consume_state()` unpacks it with
`raw.split(":", 3)` and compares element 2 to the full redirect URI
(`app/auth/telegram_oidc.py:45-50`). Every real redirect URI starts with
`https://`, which contains a colon, so element 2 is the literal string
`"https"` and validation fails on every attempt:

```
raw.split(":", 3) → ['HASH', 'VERIFIER', 'https', '//host/api/auth/telegram/callback:NONCE']
```

All three reviews reproduced this independently. Even if the comparison were
fixed, the `verifier`/`nonce` positions are also wrong, so PKCE and nonce
validation would still break. **No administrator can complete the advertised
OIDC login flow.** No test exercises `consume_state` with a realistic
`https://` URL, which is why the suite stays green.

### U-02 — OIDC token exchange uses the wrong Telegram client-authentication method

**Found by:** SOL only (1/3) · **Sources:** SOL-02

Even after U-01 is fixed, `app/admin/routes.py:90-94` posts `client_secret` as
a form field and sends no HTTP Basic Authorization header. Telegram's OIDC
instructions require Basic Authorization with the client ID and secret (their
example form contains `client_id` but not `client_secret`). The token request
is therefore not the documented Telegram exchange and should be expected to
fail — a second, independent end-to-end login blocker.

### U-03 — Ticket 05 privacy/deletion scope and admin UI are largely absent

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-11, CR-12, F2 (part), SOL-03

The ticket itself admits "Partial local implementation only", but if the five
tickets are treated as implemented this is a release blocker:

- The router ends at read-only `GET /api/admin/logs`
  (`app/admin/routes.py:294-314`). There is no `DELETE /api/admin/users/{id}`
  and no `DELETE /api/admin/logs` — no observed-profile deletion, history
  purge, cancellation of indexed jobs, snapshot/answer erasure, quoted-reply
  scrubbing, membership removal, or admin-session invalidation on deletion.
- The `cancelled` job state and the `jobs:chat:*`/`jobs:user:*` purge indexes
  are maintained (`app/store/jobs.py:312-314`) but nothing ever uses them.
- Observed profiles and list memberships have no ordinary expiry
  (`app/store/users.py:42-64`, `app/store/lists.py:119-130`), making deletion
  a necessary privacy control, not optional polish.
- The frontend (`public/index.html`, `public/app.js` — 7 lines) is a read-only
  JSON viewer plus Logout, not the required users/lists/rules/tone/logs/privacy
  management UI. Observed-username assignment, unknown-username 422 UX, list
  rename, destructive confirmations, mutation feedback, 401/403 UX, retention
  display, and the participant privacy notice are all absent (no notice text
  exists anywhere in the repo, including README).

---

## High findings

### U-04 — Assigned-admin group membership is never verified or rechecked

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-10, F2 (part), SOL-04

`POST /api/admin/admins` converts and stores the supplied ID directly
(`app/admin/routes.py:137-147`); there is no `getChatMember` call anywhere in
the tree. Session issuance and validation check only Redis role/super-admin
status (`app/auth/session.py:24-55`) — no allowed-group membership check at
assignment or login, and no five-minute session recheck. A super-admin can
grant panel access to an account outside the private group, and a member who
leaves or is kicked keeps access until manually removed — violating the access
boundary and membership-revocation contract.

### U-05 — `scripts/seed.py` cannot run normally, is not idempotent, and is not directly runnable

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-03, F3, SOL-17
**Severity votes:** Codex High · Fable Critical · SOL Medium → **High**

Three stacked defects in the required ticket-03 provisioning script:

- Direct `python scripts/seed.py --help` fails immediately with
  `ModuleNotFoundError: app` — unlike `scripts/set_webhook.py`, it never adds
  the project root to `sys.path` (`scripts/seed.py:1-9`). [reproduced by SOL]
- When run via a working module path, a plain (no `--force`) run always exits
  non-zero: it creates the reserved `ignore` list with `force=args.force`
  (default `False`), and `lists.create()` unconditionally raises
  `"ignore is reserved"` without force (`app/store/lists.py:88-93`).
- A second non-force run fails on `"list already exists"` for the demo
  entities, so the script is not idempotent; with `--force` it overwrites
  administrator changes — the opposite of the ticket's "idempotent, never
  overwrites without explicit --force" requirement.

### U-06 — Production admin authentication does not fail closed on invalid configuration

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-28, F9, SOL-05

`production_config_errors()` correctly flags a short `SESSION_SECRET`,
missing `SUPER_ADMIN_ID`, or missing OIDC values
(`app/settings.py:140-157`), but only `/api/health` and the Telegram webhook
consult it. The auth/admin routers are always mounted
(`app/server.py:37-45`) and `require_session()` only requires a *non-empty*
secret (`app/auth/session.py:24-44`), so a production deployment with an
8-byte `SESSION_SECRET` still signs and accepts sessions while health reports
the configuration invalid. Ticket 05 requires production to refuse to operate
in this state; returning 503 from health does not stop the sensitive routes.

### U-07 — Judge Pro stages can repeat paid calls after an ambiguous takeover

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-05, F8 (part), SOL-06

`prepare_intent()` deliberately returns `ambiguous`/`conflict`/
`ownership_lost` for unsafe states (`app/store/job_backend.py:425-476`; Lua at
`app/store/job_backend_upstash.py:309-347`), but the claims and verdict stages
inspect only whether a checkpoint exists and ignore the returned status
(`app/telegram/processor.py:450-474`, 524-546). If a Pro call succeeds and the
worker dies before checkpointing, the next fence sees the old uncheckpointed
intent as ambiguous — and current code enters the ordinary `else` branch and
calls Pro again. This violates the explicit "retry never repeats a successful
Pro/search call" contract and double-spends provider quota.

### U-08 — Evidence stage: Tavily calls without ownership guard; batch-only checkpoint repeats searches

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-06, F8 (part), SOL-07

In `_judge_answer()` (`app/telegram/processor.py:476-523`):

- The evidence branch never calls `_require_owned()` before the search loop or
  before each `tavily_search()` call, and it falls through even when
  `prepare_intent()` reported `ownership_lost`. An expired, superseded, or
  cancelled worker can still disclose queries to and incur cost at an external
  provider — violating the "check ownership before every provider side effect"
  invariant (the claims and verdict stages do guard their Pro calls).
- Evidence is checkpointed only after the entire search batch. A crash or
  budget timeout mid-batch repeats all already-successful searches on the next
  attempt.

### U-09 — Tavily query de-identification does not enforce the private-chat boundary

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-07, F12, SOL-08

`sanitize_query()` rejects only `@`, URL prefixes, and runs of ≥4 digits
(`app/search/tavily.py:18,34-44`). It does not compare queries to the
transcript (verbatim private quotes pass), does not catch shorter/split
numeric IDs, and `validate_claims()` first calls it with no participant terms
at all (`app/search/judge.py:13-28`). The later `forbidden_terms` list is
built only from preceding-context names/usernames
(`app/telegram/processor.py:500-511`) — the trigger author's identity is not
included. SOL reproduced a three-digit participant ID and a complete raw chat
sentence passing unchanged. Claims whose queries fail sanitization are
silently dropped instead of being recorded as `unverified_private`, so the
verdict cannot disclose what was skipped. Prompt injection or an ordinary
model mistake can send private group content to Tavily despite the stated
code-enforced guarantee.

### U-10 — `/deep <question>` is incorrectly gated and snapshotted as a dispute [reproduced]

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-08, F4, SOL-10

`command == "deep"` enters the shared judge branch
(`app/telegram/webhook.py:259-303`), so `/deep` requires at least three
meaningful prior records from two human authors. In a new or quiet chat an
admin's `/deep explain X` receives "Not enough context to analyze this
dispute." (SOL reproduced with `/deep What is 2+2?` in an empty chat — no job
created). The snapshot also uses judge-filtered `meaningful[-N:]` context with
`N = judge_default_n` instead of the ordinary up-to-30-record reply context,
and bare `/deep` with no question is accepted. Ticket 04 defines `/deep` as an
explicit Pro route with ordinary context and no dispute semantics — the
feature is not usable as specified.

### U-11 — Judge sufficiency is checked against the wrong record window [reproduced]

**Found by:** SOL only (1/3) · **Sources:** SOL-11

The webhook validates the three-record/two-author threshold over all loaded
meaningful records (up to 30) and only afterwards applies the requested N when
building the snapshot (`app/telegram/webhook.py:271-301`). SOL reproduced:
six records pass the two-author check because the oldest record has a second
author, while the newest five are all one person — `/judge 5` then queues a
five-record transcript containing only one author. The sufficiency check must
describe the transcript actually sent to the model.

### U-12 — Judge evidence loses its claim association; partial verification is not disclosed

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-09, F15 (part), SOL-09
**Severity votes:** Codex High · Fable Medium · SOL High → **High**

The search checkpoint flattens sources and discards their `claim_id`
(`app/telegram/processor.py:507-523`), and `build_judge_messages()` receives
transcript and evidence but not the extracted claims
(`app/llm/prompts.py:285-295`). Neither the model nor code can determine which
source supports or refutes which claim, so per-claim "insufficient evidence"
statements cannot be grounded. The required "verification unavailable"
disclosure is added only when the evidence list is *entirely* empty
(`app/telegram/processor.py:547-555`) — partially verified disputes get no
disclosure for the claims that were private, failed, or unsupported.

### U-13 — Tone API: partial updates destroy configuration; chat `judge_default_n` cannot be set

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-04, F5, SOL-18 (part)

`PUT /api/admin/tone` forwards raw JSON into `config_store.set_tone(**payload)`
(`app/admin/routes.py:270-281`) whose keyword defaults are
`tone_mode="preset", tone_preset="neutral"`
(`app/store/config_store.py:100-136`). Consequences, all reachable from the
intended API:

- `{"scope":"global","judge_default_n":25}` silently resets the global tone to
  neutral/preset, discarding an active custom or serious configuration.
- For `scope="chat"`, a supplied `judge_default_n` is overwritten with the
  current effective value and never persisted — the documented chat-level
  judge default cannot be set at all (SOL reproduced: supplying 9 retained the
  prior value).
- Writing a chat preset stores only `{tone_mode, tone_preset}` and replaces
  the whole `cfg:<chat_id>` value, dropping a previously saved chat custom
  prompt instead of keeping it saved-but-inactive as ticket 03 requires.

### U-14 — Production (Lua) and test (MemoryKV) tone-command semantics diverge; Lua freezes defaults as chat overrides

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-21, F6, SOL-18 (part)

`apply_tone_command()` has two hand-written implementations selected by
`hasattr(store, "_values")` (`app/store/config_store.py:166-218`). For a chat
tone command with no existing override, MemoryKV stores a partial
`{tone_mode, tone_preset}` patch, but the Upstash Lua branch materializes a
**full default config** (`custom_system_prompt=null`, `judge_default_n=20`),
patches mode/preset, and stores all four fields as the chat override.
`_read_override()` treats every stored field as an override
(`config_store.py:84-97`), so in production a simple `/tone scientist`
permanently freezes `judge_default_n=20` and a null custom prompt as chat
overrides, masking all later global changes. Tests exercise only the MemoryKV
branch, so production behavior is untested and different from what the suite
proves.

### U-15 — `__Host-` cookies are deleted without `Secure`, so browsers can ignore the deletion

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-13, F10, SOL-26 (part)

Session/OIDC cookies are set with `Secure` (`app/admin/routes.py:56`, 110),
but `response.delete_cookie(...)` on logout and on the OIDC callback
(`app/admin/routes.py:75`, 113) omits `secure=True`. Browsers enforce the
`__Host-` prefix requirements on *every* `Set-Cookie`, including expiries, and
can reject the deletion header. Server-side revocation still blocks API access
after logout, but stale session and pre-auth cookies linger in the browser,
violating the required cookie-expiration behavior.

### U-16 — Mutation CSRF/Origin enforcement is weaker than the ticket-05 contract

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-14, F11, SOL-26 (part)

`_require_mutation()` (`app/admin/routes.py:30-43`) checks the `Origin` header
only *if present* — a request with neither Origin nor Referer is accepted, and
the required strict Referer fallback is not implemented. The CSRF token is
compared with ordinary `!=` instead of a constant-time comparison. Logout
repeats the conditional-Origin pattern and skips CSRF when no session cookie
is presented (`routes.py:60-76`). The contract requires same-origin Origin (or
strict Referer) *and* constant-time server-session CSRF comparison for every
unsafe method.

### U-17 — Webhook registration accepts an attacker-controlled actual host

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-29, F20, SOL-13

`scripts/set_webhook.py:74-77` validates `PUBLIC_BASE_URL` with only
`startswith("https://")`. A credential-bearing URL such as
`https://trusted.example@evil.example` passes while its real host is
`evil.example`. The script would register that destination with Telegram,
sending all private-group updates plus the webhook secret header to the
attacker origin. The strict structural validator already exists
(`app/settings.py:97-107`, rejects userinfo/path/query) but the script does
not use it.

### U-18 — Ticket 02, 03, and 04 commits are not independently bootable

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-01, F14, SOL-14
**Severity votes:** Codex Critical · Fable Medium · SOL High → **High (process)**

Codex and Fable proved this for the ticket-02 commit; SOL extended it with
clean-archive reproductions of all three:

- `b582dc5` (Ticket 02): `import app.server` fails — the processor already
  imports Ticket 04 search modules (`ModuleNotFoundError: app.search`), and
  server/webhook import later admin/config/lists/rules modules.
- `4b825e3` (Ticket 03): same `app.search` import failure.
- `c8bac82` (Ticket 04): `app.server` imports `app.admin.routes`, which
  arrives only in Ticket 05 (`ModuleNotFoundError: app.admin`).

The final tree boots, but the required independently testable,
one-ticket-per-commit delivery gate was not met; bisecting and the ticket
completion definition are both compromised.

### U-19 — Policy caps do not bound webhook Redis work (unbounded fan-out)

**Found by:** SOL only (1/3) · **Sources:** SOL-16

`rules.all_rules()` fetches every indexed rule one by one before matching and
the ten-policy cap (`app/store/rules.py:80-86`, 130-153); `lists.all_lists()`
fetches every list and `member_lists()` performs a membership request per
applicable list (`app/store/lists.py:79-85`, 133-146). Admin APIs impose no
entity-count cap, these are synchronous calls in Telegram ingestion, and
automatic routing resolves rules twice (see U-24). With hundreds of entities,
one update causes hundreds of sequential Upstash REST operations even though
only ten policies reach the prompt — contradicting the "inexpensive Redis/CPU
work" webhook contract and risking deadline-driven Telegram retries.

### U-20 — Judge evidence source IDs collide / are non-sequential [reproduced ×2]

**Found by:** Fable, SOL (2/3) · **Sources:** F7, SOL-35
**Severity votes:** Fable High · SOL Low → **High** (actual collisions reproduced)

`app/telegram/processor.py:514-522` extends `evidence` with a *generator*
whose IDs are computed as `f"S{len(evidence) + index + 1}"`. `list.extend`
consumes the generator lazily, so `len(evidence)` grows while the expression
is being evaluated. SOL reproduced non-sequential IDs (`S1, S3, S5`) for one
claim; Fable reproduced genuine collisions with two claims × two sources:
`['S1', 'S3', 'S3', 'S5']` — two different URLs share the label `S3`, making
the citation allowlist, the `[Sx]` references in the verdict, and the appended
source list ambiguous. This defeats exactly the citation traceability
ticket 04 exists to guarantee. Untested.

---

## Medium findings

### U-21 — A valid slow judge run cannot fit the worker budget; the deadline excludes terminal bookkeeping

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-22, F19, SOL-12
**Severity votes:** Codex Medium · Fable Medium · SOL High → **Medium**

Two serial Pro calls at up to 180 s each (`app/llm/client.py:12`, 189-208)
plus up to three Tavily searches with up to two 12-s attempts each
(`app/search/tavily.py:59-78`) can exceed 400 s against the 240-s
`asyncio.timeout` (`app/telegram/processor.py:855-859`), with Vercel capped at
300 s. Stage checkpoints mean retries usually finish the remaining work within
the 4-attempt cap, so this burns QStash retries and latency rather than
failing permanently — but a "slow but valid" judge is over budget by design.
SOL adds: the deadline wraps only `_run_delivery`; final state writes,
failure-notice Telegram work, renewal cleanup, and lease release (lines
859-937) run after it, so a request near the budget can cross the platform
deadline during terminal bookkeeping.

### U-22 — Admin API: untyped/unbounded bodies, coercive validation, wrong status codes and ordering

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-26, CR-30, F13, SOL-22

Admin mutations call `await request.json()` and hand raw dictionaries to the
stores — no Pydantic models, no unknown-field rejection, no request-size
limits (`app/admin/routes.py:137-281`). Concrete consequences (several
reproduced by SOL):

- `int(payload["user_id"])` accepts booleans/floats/strings — JSON `true`
  becomes admin ID 1; `5.9` becomes 5.
- String `"false"` coerces to `True` for list/rule `enabled` and
  `stop_processing` (`app/store/lists.py:36-61`, `app/store/rules.py:33-60`);
  numeric list slug `1` is coerced to `"1"`.
- Invalid rule IDs and list-member IDs raise uncaught `ValueError` from
  `rules._key`/`lists._slug` → 500 instead of 4xx
  (`routes.py:215-220`, 254-259).
- `GET /api/admin/rules` returns lexical ID order (`app/store/rules.py:80-86`),
  not the canonical priority-descending/ID-ascending order — the UI preview
  can disagree with actual rule evaluation.
- Unknown username lookup returns `200 {"user": null}` instead of the required
  explanatory 422 (`routes.py:294-304`).
- The unbounded `q` query can throw during huge numeric conversion; list
  rename and member-listing APIs are absent.

### U-23 — List/rule CRUD is non-atomic and can leave orphaned state

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-15, F17 (part), SOL-19

Creation is separate `GET → SET → SADD`; deletion is `DEL → SREM`; membership
addition is check-then-write (`app/store/lists.py:88-126`,
`app/store/rules.py:89-111`). A crash after metadata `SET` but before index
`SADD` leaves an invisible entity that blocks retry as "already exists"; a
concurrent list delete after the member-existence check can create orphan
membership that reappears if the slug is recreated — contrary to the
no-orphan requirement.

### U-24 — Automatic routing TOCTOU and silent corruption handling

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-16, CR-17, F17 (part), SOL-20

Auto rules are resolved once to decide routing
(`app/telegram/webhook.py:329-340`) and again inside `_effective_policy()`
(223-239, 347-352). An admin edit/delete between the two reads can enqueue an
automatic reply with a different — or empty — rule policy (a ghost reply after
the trigger was removed). Separately, malformed list/rule JSON decodes to
`None` and is silently skipped (`app/store/lists.py:68-76`,
`app/store/rules.py:69-77`), so a possible automatic trigger becomes a silent
final "no route" instead of the retryable policy-load failure ticket 03
requires.

### U-25 — Rule match values are unbounded; multi-token `word` rules can never match [reproduced]

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-18, F17 (part), SOL-21

`rules._validate` bounds `instruction` (8,000) but places no length limit on
`match.value` (`app/store/rules.py:41-45`), which is normalized and scanned on
every webhook message — violating the bounded-input/inexpensive-routing
requirement. SOL additionally reproduced: a `word` rule with value
`"two words"` is accepted, but matching tests the whole value against
`haystack.split()` membership (lines 121-127), so such a rule can never match.

### U-26 — Retention/cooldown timing invariants are not validated

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-19, L5, SOL-15
**Severity votes:** Codex Medium · Fable Low · SOL High → **Medium**

Production validation checks only `0 < worker budget < lease < 300` and
positivity of retention/cooldown (`app/settings.py:164-185`). It never relates
job retention to QStash delivery/retry timing or to the automatic cooldown:
`JOB_RETENTION_SECONDS=1` passes readiness, and a job can then expire before
QStash's first delivery — the worker treats the missing job as terminal 200
and the reply is permanently lost. Likewise a cooldown longer than job
retention keeps suppressing new automatic work after its owning job has
expired, breaking the blocker/job invariant
(`app/store/job_backend.py:303-329`, `app/store/job_backend_upstash.py:130-153`).

### U-27 — `/mode` writes its receipt before producing the reply and can permanently lose its response

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-20, F18, SOL-29

The `/mode` branch calls `record_command()` — writing both the `cmd:` receipt
and the final dedup marker — *before* reading configuration
(`app/telegram/webhook.py:99-106`, `app/store/config_store.py:221-235`). If
`get_config()` then fails transiently, the webhook returns 503, but every
Telegram retry sees the durable receipt and returns `{"dedup": true}` — the
user never gets the `/mode` answer. The side effect and the response are not
atomic.

### U-28 — Judge structured-output, citation, and reasoning-output validation is incomplete

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-23, F15 (part), SOL-30

- Claim extraction is prompt-only text followed by `json.loads()`
  (`app/telegram/processor.py:466-474`); a ```json fence or surrounding prose
  silently yields `claims = []` and a logic-only verdict with no error class
  recorded. (The `except TavilyUnavailable: claims = []` in that stage is also
  unreachable dead code.)
- `validate_citations()` strips only bracket IDs and literal `http(s)://`
  tokens (`app/search/judge.py:31-37`); markdown links, `www.` links, and bare
  invented domains (`example.com/page`) survive despite the "model cannot
  invent a URL" contract.
- `_response_text()` returns provider content verbatim
  (`app/llm/client.py:157-164`); if a provider ever places `<think>…</think>`
  reasoning in `content`, nothing strips it.

### U-29 — Tavily's 256 KB response cap is applied after full download

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-24, L4, SOL-31

`httpx.post()` fully buffers the response before `len(response.content)` is
compared with the cap (`app/search/tavily.py:63-85`). The advertised limit
therefore bounds parsing, not network or memory consumption, against an
oversized upstream response.

### U-30 — Missing chat allowlist becomes "allow every chat" outside Vercel

**Found by:** SOL only (1/3) · **Sources:** SOL-32

`TELEGRAM_ALLOWED_CHAT_ID` defaults to `None`; readiness checks are bypassed
whenever the `VERCEL` env var is absent (`app/telegram/webhook.py:69-72`), and
the gate rejects only when the value is not `None` (`webhook.py:413-415`). A
staging or self-hosted instance with a configured webhook but omitted
allowlist will persist and process updates from every chat containing the bot
— contrary to the single-private-group boundary at the center of the design.

### U-31 — Command parsing edge cases: Unicode-digit 503 loop; foreign-bot command fallthrough [reproduced]

**Found by:** SOL only (1/3) · **Sources:** SOL-23

- `str.isdigit()` accepts Unicode digits that `int()` rejects (e.g.
  superscript `²`). With sufficient context, `/judge ²` raises `ValueError`
  at `app/telegram/webhook.py:291-294`; the public route catches it as a
  transient storage failure and returns 503 (`webhook.py:417-431`) without
  completing dedup — Telegram retries the same deterministic failure
  indefinitely.
- `parse_command()` correctly rejects `/judge@OtherBot`, but processing then
  falls through to ordinary explicit routing: in a reply to this bot, the
  foreign-addressed command was reproduced creating a Flash `reply` job. A
  command addressed to another bot should not trigger this bot at all.
- (Bare `/deep` with no question is accepted — also covered under U-10.)

### U-32 — Untrusted Tavily titles can forge the authoritative source section

**Found by:** SOL only (1/3) · **Sources:** SOL-24

Tavily titles are only stringified and truncated — newlines, control
characters, and embedded URLs remain (`app/search/tavily.py:113-119`) — and
the processor appends each title verbatim into the authoritative-looking
`Sx — title — URL` list (`app/telegram/processor.py:551-555`). A malicious
indexed page title can inject fake extra source lines or a phishing URL into
the final bot response. Search results are untrusted input and need
output-safe normalization before code-generated citation rendering.

### U-33 — Production readiness omits `LLM_MODEL_SMART` and the Tavily-disabled state

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-25 (part), F21, SOL-27

`production_config_errors()` pins/validates `LLM_MODEL_FAST` but never
`LLM_MODEL_SMART` (`app/settings.py:159-160`); `get_pro_client()` rejects a
noncanonical smart model only at call time, so health can report ready while
every `/judge`/`/deep` fails with `provider_configuration`. Missing Tavily is
an allowed degraded mode, but ticket 04 requires health to surface that
dependency as disabled — it does not (`app/server.py:41-74`).

### U-34 — Auth-route hardening gaps: no rate limiting, per-callback JWKS client, unvalidated `sub`

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-27, F9 (part), L11, SOL-25

Every unauthenticated `/api/auth/telegram/start` creates a ten-minute Redis
transaction (`app/admin/routes.py:51-57`) with no rate limit — explicitly
required by ticket 05. Each callback constructs a fresh `jwt.PyJWKClient` and
performs its synchronous key fetch inside the async route
(`app/admin/routes.py:79-102`): no shared bounded JWKS cache, no explicit
single unknown-`kid` refresh policy, and the event loop can be blocked. The
OIDC `sub` claim is required but never cross-checked against the numeric
Telegram `id` claim.

### U-35 — 4,000-code-point chunks can exceed Telegram's 4,096 UTF-16 limit

**Found by:** Fable only (1/3) · **Sources:** F16

`split_plain_text()` splits at 4,000 Python code points
(`app/telegram/client.py:188-194`), but Telegram measures message length in
UTF-16 code units — emoji count as 2. A chunk with more than ~96 astral
characters exceeds 4,096 units and receives a permanent Bot API 400, which the
delivery path classifies as `_PermanentWork` → job `failed`. Emoji-heavy LLM
output (plausible for the "street"/"sarcastic_robot" tones) can therefore
permanently fail delivery. Note: `00-ARCHITECTURE.md` itself says "4,000
Unicode characters", so the spec shares the wrong assumption. Splitting can
also cut grapheme clusters (cosmetic).

### U-36 — Service commands count toward and enter the judge transcript

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-32, L2, SOL-28 (part)
**Severity votes:** Codex Medium · Fable Low · SOL Medium → **Medium**

Judge preparation filters records flagged `is_service`
(`app/telegram/webhook.py:271-283`), but nothing ever sets that flag —
`/ping`, `/help`, and tone/mode commands are persisted as ordinary history
records (`webhook.py:310-327`, `app/telegram/models.py:186-198`). They
therefore count toward the three-message/two-author threshold and appear in
the judge transcript. Fable notes "service records" could defensibly mean
Telegram service messages (already excluded); at minimum this needs an
explicit product decision.

### U-37 — Early judge refusal paths ignore the `mark_seen` winner and can duplicate replies

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-31, L1, SOL-28 (part)
**Severity votes:** Codex Medium · Fable Low · SOL Medium → **Medium**

The non-admin and insufficient-context judge paths persist and call
`mark_seen()`, but ignore its winner boolean before unconditionally returning
a webhook-body `sendMessage` (`app/telegram/webhook.py:266-286`). Concurrent
duplicate deliveries can each emit the refusal, regressing the
final-dedup/one-response invariant (ordinary commands correctly reply
winner-only; rare in practice with `max_connections=1`).

### U-38 — Ticket 04/05 test coverage is materially insufficient

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-25 (part), FABLE test-suite assessment, SOL-33

347 tests pass and genuinely cover the hard parts of tickets 01–03 (state
transitions, crash boundaries, races, UTF-16 mentions, Lua-parity memory
semantics). But:

- No test drives the OIDC callback/`consume_state` with a realistic `https://`
  redirect URI (which is why U-01 survived), and there are no end-to-end
  session/CSRF/admin/UI/privacy tests at all.
- `tests/test_ticket04.py` has three shallow tests: no `/deep`, foreign
  suffixes, N-boundary/sufficiency, refusal races, Pro stage retry/fencing,
  intent takeover, per-search checkpoints, ownership loss, Tavily HTTP
  behavior, query privacy, partial evidence, citation, source-ID, budget, or
  degraded-health coverage.
- The Upstash Lua branch of `apply_tone_command` is untested — only the
  MemoryKV twin runs (which is how U-14 survived);
  `tests/test_job_backend_upstash.py` checks selected Lua source text rather
  than executing the concurrency/expiry contract.
- `conftest.py` forces `TELEGRAM_ALLOWED_CHAT_ID = None` globally; only a few
  tests exercise the allowed-chat gate.
- Ticket 04 claims its local automated checks are complete while its detailed
  criteria remain unchecked; README's "Tickets 01–04 are implemented" is not
  supported by the failing `/deep`, judge, privacy, and retry paths above.

---

## Low findings

### U-39 — Synthetic "message is not modified" recovery stores server time as `edit_ts`

**Found by:** Codex, Fable, SOL (3/3) · **Sources:** CR-33, L3, SOL-34

`_synthetic_edit_result()` fabricates `edit_date` from `time.time()`
(`app/telegram/processor.py:559-565`) and `outbound_history_record()` persists
it as canonical `edit_ts` (`app/telegram/client.py:262-283`) in both answer
and failure-notice recovery paths. The history contract requires `edit_ts` to
originate from Telegram's `edit_date`; server time is diagnostic-only and can
distort version/retention ordering (bot-authored records only; low impact).

### U-40 — `adminver` increments are read-modify-write, not atomic

**Found by:** Fable only (1/3) · **Sources:** L6

`app/store/admins.py:34-51` increments the admin version without atomicity.
Mitigated in practice: `require_session` also rechecks `is_admin` on every
request, so removal still takes effect immediately.

### U-41 — `config_store` bypasses the KV abstraction with duck-typed private access

**Found by:** Fable only (1/3) · **Sources:** L7

`hasattr(store, "_values")` selects between two hand-written implementations
and calls private `store._call` / `_set_unlocked` APIs
(`app/store/config_store.py:166-235`). Brittle against any backend change and
the direct structural cause of the U-14 divergence.

### U-42 — Dead and duplicated code

**Found by:** Fable only (1/3) · **Sources:** L8

The `tone`/`set_mode` branch duplicates identical if/else arms
(`app/telegram/webhook.py:312-322`); `except TavilyUnavailable: claims = []`
in the claims stage is unreachable (`app/telegram/processor.py:472-473`);
`history.append` is a thin alias.

### U-43 — Media messages without captions become empty-text history records

**Found by:** Fable only (1/3) · **Sources:** L9

Empty-text records consume slots in the 30-record buffer and force every
consumer to re-filter them.

### U-44 — Single-flight Upstash client lock can cause 503 contention

**Found by:** Fable only (1/3) · **Sources:** L10

Every Upstash call is serialized behind an in-process lock with a 2-s acquire
timeout (`app/store/redis.py:470-480`); concurrent webhook + QStash callbacks
in one instance can hit `TimeoutError("Redis client contention")` → 503
retries. Safe, but worth knowing when reading production logs.

### U-45 — Judge-scoped rules match against the command text, not the transcript

**Found by:** Fable only (1/3) · **Sources:** L12

`rules.resolve(msg.text, "judge")` is called with the trigger text
(`/judge 10`), so judge-scoped rules will almost never fire
(`app/telegram/webhook.py:303`). The spec never states what judge rules match
against — needs an explicit product decision.

### U-46 — Documentation and environment nits

**Found by:** Fable only (1/3) · **Sources:** L13

README's status paragraph has a garbled orphaned sentence
(`README.md:12-16`); no participant privacy notice text exists anywhere
despite the pre-production requirement (also part of U-03);
`.python-version` pins 3.12 while the local venv runs 3.13 (tests pass on
both; CI uses 3.12); `ruff` is pinned in `requirements-dev.txt` but was not
installed in the reviewed local venv.

### U-47 — The webhook's readiness gate makes tickets 01–04 undeployable without ticket-05 secrets

**Found by:** Fable only (1/3) · **Sources:** L14

The webhook uses the full `production_config_errors()`
(`app/telegram/webhook.py:69-72`), so on Vercel the bot refuses all Telegram
traffic until OIDC client values, `SESSION_SECRET`, and `SUPER_ADMIN_ID` are
configured — even though the admin panel they serve is currently
non-functional (U-01/U-02). Consistent with the architecture contract, but an
operational coupling worth knowing.

---

## Traceability matrix (all 103 original findings)

| Unified | Codex | Fable | SOL |
|---|---|---|---|
| U-01 | CR-02 | F1 | SOL-01 |
| U-02 | — | — | SOL-02 |
| U-03 | CR-11, CR-12 | F2 (part) | SOL-03 |
| U-04 | CR-10 | F2 (part) | SOL-04 |
| U-05 | CR-03 | F3 | SOL-17 |
| U-06 | CR-28 | F9 (part) | SOL-05 |
| U-07 | CR-05 | F8 (part) | SOL-06 |
| U-08 | CR-06 | F8 (part) | SOL-07 |
| U-09 | CR-07 | F12 | SOL-08 |
| U-10 | CR-08 | F4 | SOL-10 |
| U-11 | — | — | SOL-11 |
| U-12 | CR-09 | F15 (part) | SOL-09 |
| U-13 | CR-04 | F5 | SOL-18 (part) |
| U-14 | CR-21 | F6 | SOL-18 (part) |
| U-15 | CR-13 | F10 | SOL-26 (part) |
| U-16 | CR-14 | F11 | SOL-26 (part) |
| U-17 | CR-29 | F20 | SOL-13 |
| U-18 | CR-01 | F14 | SOL-14 |
| U-19 | — | — | SOL-16 |
| U-20 | — | F7 | SOL-35 |
| U-21 | CR-22 | F19 | SOL-12 |
| U-22 | CR-26, CR-30 | F13 | SOL-22 |
| U-23 | CR-15 | F17 (part) | SOL-19 |
| U-24 | CR-16, CR-17 | F17 (part) | SOL-20 |
| U-25 | CR-18 | F17 (part) | SOL-21 |
| U-26 | CR-19 | L5 | SOL-15 |
| U-27 | CR-20 | F18 | SOL-29 |
| U-28 | CR-23 | F15 (part), L8 (part) | SOL-30 |
| U-29 | CR-24 | L4 | SOL-31 |
| U-30 | — | — | SOL-32 |
| U-31 | — | — | SOL-23 |
| U-32 | — | — | SOL-24 |
| U-33 | CR-25 (part) | F21 | SOL-27 |
| U-34 | CR-27 | F9 (part), L11 | SOL-25 |
| U-35 | — | F16 | — |
| U-36 | CR-32 | L2 | SOL-28 (part) |
| U-37 | CR-31 | L1 | SOL-28 (part) |
| U-38 | CR-25 (part) | (test-suite section) | SOL-33 |
| U-39 | CR-33 | L3 | SOL-34 |
| U-40 | — | L6 | — |
| U-41 | — | L7 | — |
| U-42 | — | L8 | — |
| U-43 | — | L9 | — |
| U-44 | — | L10 | — |
| U-45 | — | L12 | — |
| U-46 | — | L13 | — |
| U-47 | — | L14 | — |

Every Codex finding was independently confirmed by at least one other review;
Codex contributed no finding unique to it, Fable contributed 10 unique
findings (U-20 shared with SOL; U-35, U-40–U-47 exclusive), and SOL
contributed 7 exclusive findings (U-02, U-11, U-19, U-30, U-31, U-32, plus
the ticket-03/04 extension of U-18 and the SOL-15/17/21 extensions merged
above).

## Consensus positives (confirmed by multiple reviews)

- The durable job state machine — leases, fencing tokens, send intents,
  checkpoints, `failed_ambiguous` semantics, failure-notice sub-machine — is
  implemented twice (Python and Lua) with real semantic parity and strong
  crash-boundary test coverage.
- Snapshot-before-trigger-upsert ordering, immutable request/policy snapshots,
  and QStash payloads carrying only `{"job_id"}` match the architecture
  exactly.
- The prompt boundary (one aggregate trusted system message; all Telegram
  names/text/search snippets serialized as JSON in a single untrusted user
  message) is consistently applied across reply, judge, and claim prompts.
- Secret hygiene is careful throughout: the Telegram token never escapes via
  httpx exceptions, QStash/LLM errors reduce to stable error classes, and
  logs/responses never carry transcripts.
- Telegram webhook-secret comparison is constant-time; disallowed chats are
  rejected before persistence on the intended Vercel path.
- UTF-16 entity handling, versioned history upsert with edit/rollback
  protection, and username-alias ownership transfer are correct and tested.
- Current `HEAD` boots and all existing automated gates pass
  (`pytest` 347 passed, `ruff`, `compileall`).

## Operational warnings

- **Real-Redis contamination:** the Codex review states its diagnostics
  accidentally wrote to the real Upstash database using the ignored local
  `.env` credentials (`cfg:global`, `cfg:100`, `cmd:1`, `dedup:update:1`, one
  `auth:state:*` record). The Fable and SOL reviews made no external calls.
  Clean up those keys and rotate the `.env` credentials before production.
- SOL evaluated and explicitly did *not* report the absence of an
  `Upstash-Timeout` publish header — current QStash plan defaults already
  exceed the 240-second worker budget, so omission is not a defect (setting an
  aligned timeout is optional clarity).

## Consolidated release decision

All three reviews independently reached the same conclusion: **do not deploy
ticket 05 or treat the five tickets as complete.** Recommended fix order:

1. **U-01 + U-02** (both OIDC login blockers; use JSON — not colon-joined —
   state, and Telegram's Basic-auth token exchange) with a realistic callback
   test.
2. **U-13/U-14** (tone API + Lua/Memory unification) — production data
   correctness reachable from the intended API today.
3. **U-05** (seed), **U-10/U-11** (`/deep` + judge window), **U-20** (source
   IDs), **U-07/U-08** (intent status + evidence guards/per-search
   checkpoints), **U-09** (query privacy incl. `unverified_private`).
4. **U-04, U-06, U-15, U-16, U-17, U-22** hardening; then decide whether to
   finish or descope the remaining ticket-05 surface (U-03) before any live
   acceptance of the admin panel.
5. Add the missing test coverage (U-38) for every corrected boundary, then
   repeat the review gate.
