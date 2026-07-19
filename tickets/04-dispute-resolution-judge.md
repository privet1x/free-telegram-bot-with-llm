# TICKET-04 — Admin-Only Judge, Pro Verdict, and Grounded Fact Checks

**Size:** L · **Depends on:** 03 · **Unblocks:** 05
**Shared contract:** 00-ARCHITECTURE.md sections 5–8

**Status:** Local implementation and automated checks are complete. Live
provider/Vercel/Tavily acceptance remains pending authorized deployment.

## Goal

Implement the fair dispute resolver from the product requirements. An assigned
admin requests analysis of the latest N messages. The bot analyses positions and
reasoning errors, fact-checks up to three externally verifiable claims through
Tavily, and gives an impartial verdict in the active tone. Claim extraction and
verdict generation use DeepSeek V4 Pro.

A fact is “checked” only relative to supplied search evidence. Model knowledge
without search evidence must not be described as fact checking. If search is
unavailable or insufficient, the response explicitly says the factual claim is
unverified but may still give a logic-only verdict.

Also provide an explicit complex-reasoning route: an admin can run
/deep <question> or /deep@bot <question>. It creates kind="deep_reply" and calls
Pro without Tavily or a judge verdict template. This is not a hidden classifier
or silent cost escalation: ordinary mention/reply remains Flash and Pro is always
selected explicitly.

## Authorization and routing precedence

- /judge [N], /dispute [N], and a mention of the current bot containing a
  normalised English phrase such as “judge us” or “who is right” are recognised
  as judge intent before role evaluation. Only is_admin(author_id) in the allowed
  group may run it.
- A non-admin receives a short refusal with no QStash, LLM, or Tavily call.
  Recognised judge intent is fully handled and cannot fall through to ordinary
  explicit Flash reply.
- A command suffix must be absent or identify the current bot.
- After secret/allowed-chat gating but before trigger upsert, routing precedence
  is:
  1. judge command or phrase → authorization → judge job or local refusal;
  2. /deep admin command → Pro deep_reply job;
  3. other admin commands;
  4. ordinary explicit mention/reply;
  5. unmentioned automatic rules.
  One update creates at most one job.

The common flow then upserts current history/user and either queues the job or
returns a local refusal/usage message. The command/refusal enters common history
but never its own pre-trigger snapshot.

N defaults to judge_default_n, initially 20, and is clamped to [5,30]. The
judge command/phrase is stored in history but excluded from the judge transcript.
If there are fewer than three meaningful records or fewer than two human authors,
reply locally with “not enough context” and do not call an LLM.

## Durable snapshot

- [ ] Create kind="judge" through the Ticket 02 state machine.
- [ ] request_json contains the latest N records before trigger_message_id in
  chronological order; actor, effective tone/admin policy; sorted
  member_lists(actor_id, "judge"); and matched rules scoped judge/all. Apply the
  same deterministic ordering and caps as Ticket 03.
- [ ] Take snapshot before upserting the command. With N=30, it can contain all
  30 preceding records. Later messages and placeholder cannot change the dispute.
- [ ] Transcript includes message_id, author fields, is_bot, ts, text, and reply
  reference. Exclude empty/service records before N counting.
- [ ] QStash still receives job_id only. Private transcript remains in Redis until
  JOB_RETENTION_SECONDS expires.

## Pro client

- [ ] Use a dedicated factory with LLM_MODEL_SMART and the production Pro ID:

~~~python
base = ChatNVIDIA(
    model="deepseek-ai/deepseek-v4-pro",
    api_key=settings.NVIDIA_API_KEY,
    temperature=0.2,
    max_completion_tokens=2048,
)
client = base.with_thinking_mode(enabled=False)
~~~

- [ ] Pin langchain-nvidia-ai-endpoints==1.4.3 and contract-test model ID,
  chat_template_kwargs.thinking=false, and absence of literal root extra_body or
  reasoning_effort. Enabling thinking requires a separately live-verified
  implementation change.
- [ ] Send only final response.content to Telegram. Never log or expose
  reasoning_content, thinking tags, or chain-of-thought data.
- [ ] A deep_reply snapshots ordinary Flash-style prior context and effective
  policy but calls Pro once without claim extraction or Tavily. It reuses Ticket
  02 answer/delivery checkpoints and does not use a judge verdict template.

## Grounded fact checking through Tavily

Tavily basic search has a separate quota, so FACT_CHECK_MAX_QUERIES is bounded to
three, with default and maximum value 3.

### 1. Claim extraction

- [ ] The first structured Pro call receives transcript as untrusted data and
  returns at most three impersonal, externally verifiable claims:

~~~json
{
  "claims": [
    {
      "claim_id": "C1",
      "neutral_claim": "…",
      "search_query": "short neutral factual query"
    }
  ]
}
~~~

- [ ] Do not search opinions, intentions, insults, personal information, or pure
  reasoning. Search only factual claims.
- [ ] A query must not be a verbatim chat quote. Remove or reject names,
  @usernames, Telegram IDs, links to group participants, and other identifying
  context. Bound its length. If safe de-identification is impossible, mark the
  claim unverified_private and skip search.
- [ ] Validate schema and absence of participant identifiers in code. Never rely
  solely on LLM instructions.

### 2. Tavily adapter

- [ ] Implement app/search/tavily.py as direct async HTTP to Tavily Search API.
  Keep the key server-side. Use search_depth="basic", no more than three results
  per query, strict timeouts and response-size caps, HTTPS URLs, and bounded
  concurrency.
- [ ] Save only source ID (S1 and so on), title, URL, and a short snippet.
  Do not download redirect targets or web pages.
- [ ] Save claims/evidence to the job before the final Pro call. Retry reuses
  them and spends no extra search credits.
- [ ] Treat 401/invalid configuration as dependency unavailable. Give 429,
  timeout, and 5xx a short bounded adapter retry, then degrade instead of failing
  the whole verdict.

### 3. Final verdict

- [ ] build_judge_messages(job, effective_policy, evidence) has trusted system
  content for active tone, sorted judge list/rule policy, then a final
  highest-precedence impartiality block. User/data content holds transcript and
  evidence marked as untrusted.
- [ ] The model may cite only supplied source IDs such as [S1] and [S2].
- [ ] Plain-text verdict structure:
  1. subject of dispute;
  2. positions and strongest arguments;
  3. reasoning errors;
  4. fact check: confirmed, refuted, or insufficient evidence with source IDs;
  5. conclusion: who is more likely right and confidence.
- [ ] Validate cited source IDs. Code itself appends a source list of actual
  Sx — title — URL records. The model cannot invent a URL. Remove or label
  unused/invalid citations.
- [ ] When Tavily is unavailable or returns no adequate result, begin the fact
  section with an explicit notice that external verification was unavailable or
  insufficient and the remainder is logic/context analysis only.

## Worker and delivery integration

- [ ] Dispatch judge through idempotent stages:
  claim_extraction → evidence_saved → verdict_saved → delivery.
  Checkpoint every stage/result so retry never repeats a successful Pro/search
  call.
- [ ] Use the Ticket 02 placeholder, typing, splitter, outbound-history, and
  final-delivery path.
- [ ] Keep HTTP/LLM/search timeouts inside Vercel maxDuration=300 and the common
  240-second worker budget with renewable 270-second fencing lease.
- [ ] A logic-only fallback is delivered successfully only when it explicitly
  discloses that grounded verification was unavailable.

## Privacy and safety

- Send Tavily only short neutral, programmatically scrubbed queries. Never send
  participant names, usernames, IDs, raw messages, or full transcript.
- NVIDIA receives the dispute snapshot; the participant privacy notice must say
  this.
- Search snippets are untrusted data and may contain prompt injection. They never
  become system instructions.
- Logs contain claim/result count, source domains, latency, and status only; no
  queries, snippets, or transcript.

## Configuration

~~~dotenv
LLM_MODEL_SMART=deepseek-ai/deepseek-v4-pro
TAVILY_API_KEY=replace_me
FACT_CHECK_MAX_QUERIES=3
~~~

Validate FACT_CHECK_MAX_QUERIES in range 1..3. Local work may degrade without a
Tavily key, but production health marks the dependency disabled and live
fact-check acceptance is not complete.

## Automated checks

- [x] Admin/non-admin route tests for /judge, /dispute, /deep, foreign suffix,
  phrase precedence, no Flash fall-through for rejected judge intent, and one
  job per update.
- [x] N default/clamp, snapshot cutoff before command, later-message exclusion,
  and insufficient-context branch with no LLM.
- [x] Deterministic/capped judge lists and judge/all rules in snapshot. Final
  impartiality policy cannot be weakened by list/rule text.
- [x] Pro request shape, no reasoning output exposure, and deep_reply flow.
- [x] Claim schema maximum, private/non-verifiable rejection, identifier scrub,
  and no raw quote in Tavily query.
- [x] Tavily request limits; success, empty, 401, 429, timeout, and malformed
  response. Evidence checkpoint prevents repeated search.
- [x] Citation allowlist, malicious snippet treated as data, and explicit
  degraded-verdict disclosure.
- [x] Long verdict splitting/outbound history and all Ticket 01–03 tests/Ruff/CI.

## Live E2E acceptance

1. In a test group, two participants have a dispute containing a public,
   checkable claim. An admin runs /judge 10.
2. The answer uses exactly the 10 messages before the command, attributes
   positions correctly, identifies reasoning errors, and gives clear confidence.
3. With Tavily configured, the fact section contains at least one [Sx] reference
   and an appended real Tavily title+URL. Server trace has no
   names/usernames/raw transcript in search request.
4. An admin writes @bot judge us, who is right? and follows the Pro path. The
   same phrase from a normal user is refused with no NIM/Tavily spend.
5. After /tone sarcastic_robot, /judge changes manner but preserves facts,
   citations, and impartiality.
6. An induced Tavily timeout gives a logic-only verdict with explicit warning.
   QStash retry does not repeat saved searches/verdict or duplicate Telegram
   output.
7. Observability/contract tests confirm DeepSeek V4 Pro and
   chat_template_kwargs.thinking=false; no reasoning reaches chat; execution fits
   within 300 seconds.

## Risks

- Search quota is limited: basic depth, maximum three queries, saved evidence,
  and no repeat search on retry are mandatory.
- Sources may be weak or contradictory. Verdict must represent uncertainty rather
  than treating the first snippet as truth.
- Pro plus two model calls and search can be slow. Staged checkpoints and the
  300-second function limit are essential.
