# TICKET-05 — Web Admin: Telegram OIDC, CRUD/UI, and Privacy Controls

**Size:** XL · **Depends on:** 04, including Ticket 03 data and judge settings
**Unblocks:** —
**Shared contract:** 00-ARCHITECTURE.md sections 3, 4, and 8

**Status:** Implemented and shipped; live acceptance pending. Commit `b26ced7`
is on `origin/master` after the required independent review gates and 410
passing local tests. Stable-deployment Telegram OIDC, Vercel, Upstash, and group
acceptance remains pending explicit authorization.

## Goal

Give the super-admin and assigned admins a safe UI for the same tone, list, and
rule entities already used by the bot. Authentication uses modern Telegram OIDC
Authorization Code Flow with PKCE/state, followed by a short custom server-side
session with immediate role validation.

The UI is static and never embeds secret environment values. It obtains only
safe public values through /api/public/config.

## Authentication design

Use current Telegram OIDC, not the legacy iframe Login Widget/HMAC flow.

1. In BotFather → Bot Settings → Web Login, register a stable Vercel origin and
   exact callback {PUBLIC_BASE_URL}/api/auth/telegram/callback. Obtain client ID
   and client secret.
2. GET /api/auth/telegram/start creates cryptographically random state, nonce,
   PKCE verifier/challenge using S256, and an independent browser-binding handle.
   A one-time state-hash transaction stores handle hash, nonce, verifier, and
   exact redirect URI for 10 minutes. The raw handle exists only in the
   __Host-kulajaj_oidc cookie: HttpOnly, Secure, SameSite=Lax, Path=/,
   Max-Age=600, and no Domain. It never enters a URL, Redis value, or log.
3. The callback atomically consumes a transaction only when the hash of the
   presented pre-auth cookie constant-time matches its state binding and the
   redirect URI exactly matches. A missing/wrong cookie creates no session and
   does not consume someone else's transaction. Clear the cookie on every
   terminal callback response. Browser Origin is not mandatory for a cross-site
   OAuth redirect. Backend exchanges code with PKCE and client secret.
4. Validate the ID token through Telegram JWKS: allowed algorithm (default RS256),
   signature, issuer https://oauth.telegram.org, audience
   TELEGRAM_OIDC_CLIENT_ID, expiry, issued-at, and nonce. Canonical Bot API/admin
   user_id comes from the verified numeric id claim. Validate OIDC sub too, but
   never substitute it for Telegram id.
5. A valid Telegram identity alone gives no access: it must equal SUPER_ADMIN_ID
   or belong to Redis admins.
6. Create a signed JWT and Redis session:<jti>. Store it in
   __Host-kulajaj_session with HttpOnly, Secure, SameSite=Lax, Path=/, and no
   Domain.

Never expose Telegram client secret, bot token, session secret, Redis credentials,
or provider keys to frontend JavaScript.

## Sessions and immediate revocation

- Sign custom session JWT only with HS256 and a high-entropy SESSION_SECRET.
  Decode with fixed algorithms=["HS256"]; never pick an algorithm from the token
  header. Keep OIDC RS256/JWKS validation separate.
- JWT claims are issuer, audience, canonical decimal-string sub, numeric
  tg_user_id, jti, issued-at, not-before, expiry (maximum eight hours), and
  admin_version. sub must equal str(tg_user_id). Reject bool, float, sign/leading
  zero forms, and mismatched claims.
- Every require_admin request verifies signature/claims, session:<jti>, current
  adminver:<uid>, and current is_admin(uid) in Redis. JWT alone is insufficient.
- An assigned admin must be a member of the allowed group. Check with
  getChatMember during assignment and login, then recheck active sessions with a
  cache no longer than five minutes. left/kicked prevents access at next check.
  Making the bot a group admin is required in production for reliable checks.
- Removing an admin increments adminver, invalidating active requests immediately.
  SUPER_ADMIN_ID from environment cannot be removed.
- Logout deletes the server record and expires cookie. Session records expire by
  TTL. JWKS has a bounded cache; an unknown kid triggers one safe refresh.
  Never log full token or claims.

## Backend API

### Configuration and dependencies

- [x] Add TELEGRAM_OIDC_CLIENT_ID and TELEGRAM_OIDC_CLIENT_SECRET to settings and
  .env.example. Pin PyJWT[crypto]. Keep the secret server-side.
- [x] Production validation requires exact HTTPS PUBLIC_BASE_URL, OIDC values, a
  positive SUPER_ADMIN_ID, and a random SESSION_SECRET of at least 32 bytes.
  Without an environment super-admin, production does not start: there is no
  password/bootstrap endpoint. Health reports dependency names only.

### Public and authentication endpoints

- [x] GET /api/public/config returns only:

~~~json
{"telegram_bot_username":"bot_name","oidc_client_id":"123..."}
~~~

- [x] GET /api/auth/telegram/start and callback implement the above with scope
  only openid profile. Do not request phone or write permissions.
- [x] POST /api/auth/logout requires same-origin and CSRF validation and deletes
  the session.
- [x] GET /api/admin/me returns safe profile, role, and CSRF token.

### Observed users and username resolution

- [x] GET /api/admin/users?q= does a bounded exact lookup in the Ticket 01
  observed directory by numeric ID or normalized current username. Do not promise
  display-name search without an index.
- [x] Numeric Telegram ID input is allowed even before a profile is observed.
- [x] Resolve @username only through username:<casefold_without_at>. The Bot API
  cannot resolve arbitrary usernames. Unknown username returns 422 explaining
  that the person must first message in the allowed group or be entered by
  numeric ID.
- [x] Rename uses current index only. A stale alias neither resolves nor grants a
  role to another person.
- [x] DELETE /api/admin/users/{id}, super-admin only, collision-safely removes
  profile, current username key, list memberships, and assigned admin role while
  incrementing adminver. SUPER_ADMIN_ID cannot be deleted.
- [x] With purge_messages=true, atomically remove authored history, clear
  reply_to.user_id/text in other records, and cancel/delete indexed jobs whose
  snapshots contain the user. Use delivery checkpoints to remove indexed outbound
  bot history records that could quote the deleted person. If job provenance has
  expired, offer a full-chat purge honestly. A future message observes the
  profile again.

### CRUD endpoints

All endpoints require current admin unless stated otherwise.

- [x] GET/POST/DELETE /api/admin/admins. Only super-admin can mutate. Add through
  observed current @username or numeric Telegram ID.
- [x] Numeric-ID admin assignment does not require an observed profile. Backend
  calls getChatMember for the configured allowed group, requires current
  member/admin/creator state, and may seed profile from verified result.user.
  Unknown username remains 422; numeric non-member/left/kicked is refused.
  Removing an admin increments adminver.
- [x] GET/POST/PUT/DELETE /api/admin/lists plus membership add/remove. Enforce
  title, enabled, priority, applies_to, injected_prompt, length limits, and
  reserved ignore protection. UI/API can create/rename a custom list and edit
  every policy field.
- [x] GET/POST/PUT/DELETE /api/admin/rules with Ticket 03 canonical priority,
  scope, match.type/value, instruction, and stop_processing. IDs are immutable.
  Update/delete must not leave orphan indexes.
- [x] GET /api/admin/tone returns global, chat_override, and effective config.
  PUT accepts explicit scope="global" or "chat", tone_mode, canonical preset,
  custom_system_prompt, and judge_default_n in [5,30]. DELETE
  /api/admin/tone/override atomically clears chat override. API accepts only
  neutral, serious, scientist, street, and sarcastic_robot; command-only
  sarcastic alias never enters API/Redis.
- [x] GET /api/admin/logs reads bounded history only for the allowed chat.
  DELETE /api/admin/logs requires super-admin plus repeated UI confirmation,
  cancels non-terminal jobs, removes indexed private job snapshots/answers, and
  purges history. It accepts no arbitrary chat_id. QStash callback for a
  deleted/cancelled job returns terminal 2xx without side effects.
- [x] Every mutation uses typed JSON schema, rejects unknown fields, enforces
  request-size limits, returns clear 4xx, and makes no partial write on
  validation failure.

## CSRF, XSS, and HTTP security

- Every unsafe method requires both a same-origin Origin (or strict Referer
  fallback) and X-CSRF-Token compared constant-time to server-session data.
- Do not allow cross-origin CORS. Rate-limit auth routes.
- Never render logs, names, usernames, rule text, custom prompt, or errors with
  innerHTML. Use textContent/DOM APIs only; allowlist URL attributes.
- No inline JavaScript or event handlers. Load app.js with defer and local CSS.
- Vercel headers include CSP similar to:

~~~text
default-src 'self';
script-src 'self';
style-src 'self';
connect-src 'self';
img-src 'self' data: https:;
frame-ancestors 'none';
base-uri 'self';
form-action 'self' https://oauth.telegram.org
~~~

  Also set nosniff, strict Referrer-Policy, HSTS in production, and
  Cache-Control: no-store for auth/admin pages and state-changing responses.
- API errors never return stack traces or secrets.

## Frontend in public/

- [x] Landing screen fetches /api/public/config, shows bot username, and links
  “Log in with Telegram” to /api/auth/telegram/start.
- [x] After login, /api/admin/me determines role; 401 returns to login.
- [x] Implement sections:
  - **Users and lists:** observed search, numeric ID/username entry, membership,
    title/enabled/priority/applies_to/injected_prompt editing, super-admin role
    assignment.
  - **Rules:** match type/value, scope, priority, instruction, stop flag, and
    deterministic preview order.
  - **Tone:** canonical presets, custom prompt/mode, judge_default_n, visible
    global/chat/effective values, explicit global/current switch, and clear
    override.
  - **Logs:** recent records, edit/reply/bot markers, and purge action.
  - **Privacy:** retention values, copyable participant notice, observed-profile
    deletion, and history purge.
- [x] Each mutation has loading/error/success state. Destructive actions require
  explicit confirmation and reread server state after success.
- [x] Distinguish 401/403: expired/revoked session prompts login; inadequate role
  is not shown as a generic network error.

## Privacy lifecycle

- Display actual values: maximum 30 messages, per-record cutoff, sliding
  HISTORY_RETENTION_SECONDS (default 30 days), and seven-day job snapshots.
- Before production acceptance, an admin publishes a group notice that explains
  message/job retention, observed profile/list membership retention, NVIDIA
  context processing, de-identified Tavily queries, and deletion procedure.
- Purge/delete uses jobs:chat and jobs:user indexes, cancels queued work, and
  immediately removes private Redis snapshots/history. Workers recheck
  cancellation before provider calls/delivery. QStash has no transcript. Data
  already sent to an external provider cannot be recalled; say so in the notice.
- Do not show full prompts/transcripts in application logs or error records.

## Out of scope

- Multi-chat or multi-tenant roles; the product intentionally has one allowed
  chat.
- Arbitrary username resolution through MTProto.
- SPA frameworks/build pipeline and the legacy Telegram Login Widget.

## Automated checks

- [x] OIDC state one-time/expiry/mismatch, missing/wrong/swapped browser-binding
  cookie, login CSRF/session swapping, atomic consume, PKCE S256, minimal scope,
  token-exchange errors, JWKS rotation, bad algorithm/signature/issuer/audience/
  nonce, future issued-at, expiry, and distinct sub versus numeric Telegram id.
- [x] Session fixed HS256 allowlist; reject none/algorithm confusion; canonical
  sub and tg_user_id; issuer/audience/expiry/JTI/server record; logout,
  CSRF/origin, immediate admin removal, and super-admin invariant.
- [x] Missing SUPER_ADMIN_ID fails production readiness. Empty Redis admins still
  permits configured super-admin login.
- [x] Public config excludes secrets. Never serialize all Settings.
- [x] Username known/current, unknown 422, rename/stale alias, collision-safe
  cleanup, unseen numeric list member, unseen numeric group member may become
  admin through getChatMember, and non-member refusal.
- [x] CRUD validation/authorization, allowed-group membership recheck,
  deterministic entities, reserved ignore, judge_default_n, no arbitrary chat ID,
  user/chat purge cancellation, snapshot erase, and outbound history cleanup.
- [x] DOM/XSS fixtures with script/event/malicious URL strings, CSP/security/cache
  headers, and Ticket 01–04 regression suite/Ruff/Python 3.12 CI.

## Live E2E acceptance

1. On a registered stable Vercel URL, super-admin completes Telegram OIDC and
   sees the role; a valid Telegram account outside the allowlist is refused.
2. Admin API requests without cookie, without CSRF, or from another Origin are
   rejected. A removed admin loses access on the next request even before JWT
   expiry.
3. In UI, create automatic nonsense rule; an ordinary group message triggers the
   bot without redeployment. Change priority/stop and observe deterministic order.
4. Create a custom list with injected prompt, priority, and applies_to, add an
   observed @user_x, and see that user's next explicit response change.
   Unknown username gets clear 422; the same numeric ID can be added.
5. Choose global street, then chat custom with saved prompt. Subsequent Flash
   replies change; clear override returns global. judge_default_n affects /judge
   without redeployment.
6. Super-admin adds a second admin by numeric ID after group-membership check.
   That admin changes tone but cannot manage admins. After revoke, the open UI
   session stops working immediately.
7. Logs render hostile-looking text as text. Purge removes history; delete user
   clears indexes/memberships. The participant notice is published in test group.
8. Production response headers satisfy the CSP/security contract. Frontend trace
   contains no bot/OIDC client secrets, provider keys, or raw session JWT in
   JavaScript-readable storage.

## Risks

- Preview domains require separate BotFather registration; primary E2E uses a
  stable domain and exact callback URI.
- Static UI is especially exposed to DOM XSS from chat/rule text. CSP and
  textContent are mandatory safeguards, not cosmetic details.
- JWT without Redis/session/admin rechecks cannot provide immediate revoke; both
  checks are part of acceptance.
