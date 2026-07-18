# Coding Rules

These rules govern implementation of every remaining ticket. Follow them in
addition to the master architecture contract in `tickets/00-ARCHITECTURE.md`.

## Core principles

- Implement **one complete ticket at a time**. Do not begin the next ticket
  until the current ticket has passed the full workflow below.
- Treat the ticket, the master architecture, and existing implemented behavior
  as one contract. Resolve any contradiction before writing code and update the
  affected ticket/architecture document when the decision changes.
- Write production-quality code: small focused modules, clear interfaces,
  reusable helpers, explicit ownership, typed models where useful, bounded
  inputs/outputs, and deterministic behavior.
- Prefer composition over duplication. Shared concerns such as configuration,
  validation, Redis access, Telegram delivery, retries, error classification,
  and authorization belong in reusable components rather than route-specific
  copies.
- Preserve security and privacy invariants. Never hard-code or log secrets; do
  not expose private chat data in diagnostics; validate all untrusted input at
  boundaries; use least privilege and fail closed when safety-critical
  configuration is missing.
- Keep all source code, user-facing text, tests, comments, and documentation in
  English.
- Do not make unrelated refactors or behavior changes while implementing a
  ticket. If necessary cleanup is discovered, explain why it is required and
  keep it minimal.

## Required workflow for each ticket

### 1. Understand and plan

1. Read the complete ticket, `tickets/00-ARCHITECTURE.md`, this file, and all
   code/tests that the ticket extends.
2. Trace requirements to concrete modules, data keys, API contracts, and test
   cases before editing.
3. Identify risks, migrations, error paths, concurrency/idempotency constraints,
   privacy implications, and external-service contracts.
4. If the ticket needs a design decision not covered by the architecture, make
   the decision explicit in the architecture/ticket before implementation.

### 2. Implement the complete ticket

1. Implement the entire ticket scope, not merely the happy path or a partial
   vertical slice.
2. Use test-first development whenever the behavior is testable: write a
   focused failing test (or extend an existing contract test) before the
   implementation, then make it pass. A behavior is not complete without an
   automated assertion unless the ticket explicitly requires a live external
   check.
3. Add or update automated tests with the implementation. Cover normal flow,
   boundary values, malformed input, authorization, retries, concurrency,
   idempotency, cancellation, and provider failures whenever applicable.
4. Update environment templates, deployment instructions, user-facing docs, and
   privacy notices when the ticket changes them.
5. Keep public APIs, Redis keys, response shapes, and state transitions aligned
   with the master architecture.

### 3. Self-test and fix

1. Review the complete diff against every ticket checklist item.
2. Run the relevant unit, integration, contract, and end-to-end-style tests.
   Use real external services only when credentials and the task scope permit it;
   never deploy or mutate external production state without authorization.
3. Run the repository quality gates, at minimum:

   ```bash
   pytest -q
   ruff check api app scripts tests
   git diff --check
   ```

4. Add appropriate type checks, security checks, migration checks, or local
   smoke tests when the ticket introduces them.
5. Diagnose and fix every failure. Repeat this phase until the implementation is
   green and the ticket requirements are demonstrably covered.

### 4. Independent code-review pass

1. After self-testing passes, run a dedicated **Code Reviewer agent** with
   maximum effort.
2. Give the reviewer the ticket, architecture contract, changed files, test
   results, and a clear request to inspect correctness, missing requirements,
   regressions, concurrency, error handling, security, privacy, maintainability,
   test quality, and documentation alignment.
3. Evaluate every finding rather than accepting or dismissing it automatically.
   Fix all valid findings within ticket scope. If a finding is invalid, document
   the concrete evidence for that conclusion.

### 5. Re-test and second review gate

1. After any review-driven change, rerun all relevant tests and quality gates.
2. Run the Code Reviewer agent again against the final diff.
3. Do not declare the ticket complete until the second review finds no unresolved
   correctness, security, privacy, reliability, or maintainability issue.
4. If the second review finds a valid issue, return to implementation, then
   repeat self-test and code review until the gate is clean.

## Git, commit, and push workflow

The implementation and review gates are a prerequisite for every commit. Never
commit around a failing test, quality check, acceptance criterion, or unresolved
review finding.

- Keep one ticket per logical commit. Do not mix unrelated work into a ticket
  commit. Use the ticket identifier in the subject, for example:
  `TICKET-02: Add durable QStash jobs and Flash replies`.
- Before committing, inspect `git status`, review the complete diff, run
  `git diff --check`, and stage only the intended files. Do not use a broad
  staging command that could include secrets, generated files, or unrelated
  changes.
- Commits must use the repository owner's human git identity, never an agent or
  tool identity. For this repository verify (and, when the user has authorized
  repository setup, configure) the identity as:

  ```bash
  git config user.name "Anton Pazniak"
  git config user.email "anton.pazniak@gmail.com"
  ```

- Do not add `Co-Authored-By` or any AI/tool attribution, and do not mention
  Claude, Anthropic, generated-with tooling, or other agent attribution in the
  commit subject or body.
- Commit messages use an imperative, concise subject with the ticket prefix and
  a body explaining what changed and why. Do not place backticks or shell
  substitutions in `-m` arguments. The non-interactive commit form is:

  ```bash
  git -c commit.gpgsign=false commit --no-verify \
    -m "TICKET-NN: Concise imperative summary" \
    -m "Explain the problem and the chosen approach."
  ```

  `--no-verify` only avoids interactive hooks; it never replaces the required
  tests and review gates above.
- Push each completed ticket from the default `master` branch to
  `origin/master` immediately after its verified commit, but only when the
  user has authorized the requested implementation workflow to include
  repository writes. Never force-push. If a push is rejected as
  non-fast-forward, rebase with `git pull --rebase origin master` and retry the
  push.
- A ticket is not marked `Done` until its verified commit is pushed. After the
  push, update the ticket status and record a short note of what shipped and
  any deliberate deviation. If a live external check is still outstanding,
  leave the status and note explicit rather than claiming completion.
- When the user has requested a batch of tickets, continue with the next
  dependency-ready ticket after the push without waiting for an extra
  confirmation. Stop only for a real blocker or user interruption.

## Completion standard

A ticket may be reported as **fully implemented** only when all of the following
are true:

- Every in-scope requirement and acceptance criterion is implemented or clearly
  marked as requiring an authorized live external check.
- Tests and quality gates pass after the final code change.
- The final independent Code Reviewer pass has no unresolved findings.
- Documentation, environment configuration, and architecture contracts match the
  delivered code.
- No secrets, private data, or unrelated changes were introduced.
- Its ticket bookkeeping is accurate; when repository writes are authorized,
  the ticket's verified commit is present on `origin/master`.

Do not create a commit, deploy, or change external service state unless the user
explicitly authorizes that action.
