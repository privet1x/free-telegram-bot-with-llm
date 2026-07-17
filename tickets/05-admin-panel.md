# TICKET-05 — Web Admin: Telegram OIDC, CRUD/UI и privacy controls

**Размер:** XL · **Зависит от:** 04 (включает данные 03 и judge settings) ·
**Разблокирует:** —
**Общий контракт:** `00-ARCHITECTURE.md` (§3, §4, §8)

## Цель

Дать super-admin и назначенным admins безопасный интерфейс управления теми же
tone/list/rule сущностями Redis, которые уже исполняет бот. Вход — современный
Telegram **OIDC Authorization Code Flow** с PKCE/state, затем собственная
короткая серверная сессия с мгновенной проверкой актуальных прав.

UI статичный, поэтому секретные env в него не встраиваются. Безопасные значения
он получает через `/api/public/config`.

## Решение по авторизации

Используется current Telegram OIDC, не legacy iframe Login Widget/HMAC flow.

1. В BotFather → Bot Settings → Web Login зарегистрировать стабильный Vercel
   origin и точный callback
   `{PUBLIC_BASE_URL}/api/auth/telegram/callback`; получить Client ID/Secret.
2. `GET /api/auth/telegram/start` создаёт криптографически случайные `state`,
   `nonce`, PKCE verifier/challenge (S256) и отдельный browser-binding handle.
   One-time transaction по hash(state) хранит hash(handle), nonce, verifier и
   exact redirect URI с TTL 10 минут. Raw handle попадает только в cookie
   `__Host-kulajaj_oidc` (`HttpOnly; Secure; SameSite=Lax; Path=/`, Max-Age 600,
   без Domain), а не в URL/Redis/log.
3. Callback атомарно consume-ит transaction **только** если hash
   presented pre-auth cookie constant-time совпал с binding в этом state и exact
   redirect URI совпал. Missing/wrong cookie не создаёт сессию и не consume-ит
   чужую valid transaction; cookie очищается на любом завершающем callback
   response. Browser `Origin` для cross-site OAuth redirect не считается
   обязательным. Затем backend server-side обменивает code с PKCE и
   Client Secret.
4. ID token проверяется по Telegram JWKS: разрешённый algorithm (default RS256),
   signature, `iss=https://oauth.telegram.org`, `aud=TELEGRAM_OIDC_CLIENT_ID`,
   `exp`, `iat`, `nonce`. Canonical Bot API/admin `user_id` берётся из
   проверенного numeric claim `id`; OIDC `sub` также валидируется, но не
   подставляется вместо Telegram `id`.
5. Valid Telegram identity ещё не даёт доступ: ID должен быть
   `SUPER_ADMIN_ID` или находиться в Redis `admins`.
6. После проверки создаются signed JWT **и** `session:{jti}` в Redis. Cookie —
   `__Host-kulajaj_session`, HttpOnly, Secure, SameSite=Lax, Path=/, без Domain.

Frontend не получает Telegram Client Secret, bot token, session secret, Redis
или provider keys.

## Сессия и немедленный отзыв прав

- Своя session JWT подписывается только `HS256` высокоэнтропийным
  `SESSION_SECRET`; decode всегда передаёт fixed `algorithms=["HS256"]` и не
  выбирает algorithm из token header. OIDC ID-token validation остаётся
  отдельным RS256/JWKS contract.
- Session JWT claims: `iss`, `aud`, canonical decimal-string `sub`, numeric
  `tg_user_id`, `jti`, `iat`, `nbf`, `exp` (не более 8 часов), `admin_version`.
  `sub` обязан быть равен `str(tg_user_id)`; bool, float, leading sign/zeroes и
  несовпадение claims отклоняются.
- Каждый `require_admin` проверяет signature/claims, существование
  `session:{jti}`, current `adminver:{uid}` и **текущий** `is_admin(uid)` в Redis.
  Одной проверки JWT недостаточно.
- Назначенный admin должен быть участником разрешённой группы: это проверяется
  через `getChatMember` при назначении и login, затем перепроверяется для
  активной сессии с cache не дольше 5 минут. `left`/`kicked` закрывает доступ при
  следующей проверке; явный revoke роли действует немедленно. Production setup
  для гарантии `getChatMember` делает бота group admin.
- Remove admin увеличивает `adminver`, после чего все его старые requests сразу
  получают 401/403. Super-admin из env удалить нельзя.
- Logout удаляет server session и истекает cookie. Просроченные session records
  уходят по TTL.
- JWKS кэшируется с bounded TTL; неизвестный `kid` вызывает один безопасный
  refresh. Token/claims целиком не логируются.

## Backend API

### Config/dependencies

- [ ] Добавить `TELEGRAM_OIDC_CLIENT_ID`, `TELEGRAM_OIDC_CLIENT_SECRET` в
  settings/`.env.example`, pin `PyJWT[crypto]`; secret остаётся только server-side.
- [ ] Production validation требует exact HTTPS `PUBLIC_BASE_URL`, OIDC values,
  положительный `SUPER_ADMIN_ID` и случайный `SESSION_SECRET` минимум 32
  bytes. Без env super-admin production не стартует: это единственный
  bootstrap, пароля/bootstrap endpoint нет. Health сообщает только названия
  отсутствующих dependencies, не значения.

### Public/auth

- [ ] `GET /api/public/config` возвращает только:

  ```json
  {"telegram_bot_username":"bot_name","oidc_client_id":"123..."}
  ```

- [ ] `GET /api/auth/telegram/start` и
  `GET /api/auth/telegram/callback` реализуют flow выше со scope только
  `openid profile` (phone/write для админки не запрашиваются).
- [ ] `POST /api/auth/logout` требует same-origin + CSRF, удаляет сессию.
- [ ] `GET /api/admin/me` возвращает safe profile, роль и CSRF token текущей
  сессии.

### Observed users и username resolution

- [ ] `GET /api/admin/users?q=` делает bounded exact lookup в observed directory
  Ticket 01 по numeric ID или normalized current username. Поиск по display name
  не обещается без отдельного индекса.
- [ ] Ввод numeric Telegram ID разрешён, даже если профиль ещё не observed.
- [ ] `@username` резолвится **только** через `username:{casefold_without_at}`.
  Bot API не умеет разрешать произвольный username. Для неизвестного username
  API возвращает 422 с подсказкой: пользователь должен сначала написать в
  разрешённом чате либо admin вводит numeric ID.
- [ ] Переименование использует только current index: старый alias не назначает
  права другому человеку и не резолвится после корректного upsert.
- [ ] `DELETE /api/admin/users/{id}` (super-admin) collision-safe удаляет profile
  и его current username key, membership из lists и назначенную admin-role с
  increment `adminver` (super-admin удалить нельзя). Опция
  `purge_messages=true` атомарно удаляет его authored history records, очищает
  `reply_to.user_id/text` в чужих records и отменяет/удаляет indexed jobs,
  snapshot которых содержит пользователя. До удаления job purge берёт из его
  delivery checkpoints все outbound bot `message_id` и удаляет эти records из
  history: так производный ответ, который мог цитировать удаляемого
  участника, тоже исчезает. Если job уже истёк и provenance нет, UI честно
  предлагает full-chat purge.
  Новое сообщение пользователя создаст profile снова.

### CRUD (все требуют current admin)

- [ ] `GET/POST/DELETE /api/admin/admins` — только super-admin на mutation;
  add по current observed `@username` или numeric Telegram ID. Для numeric ID
  observed profile не обязателен: backend вызывает `getChatMember` только для
  configured allowed group, требует current member/admin/creator status и может
  seed-ит observed profile из проверенного Bot API `result.user`. Так PRD-контракт
  «по ID или @username» работает без снижения защиты chat logs. Unknown
  username по-прежнему 422, а numeric non-member/left/kicked — отказ. Remove
  увеличивает `adminver`.
- [ ] `GET/POST/PUT/DELETE /api/admin/lists` + add/remove membership. Backend
  соблюдает canonical `title`, `enabled`, `priority`, `applies_to`, `injected_prompt`
  и length limits, защищает reserved `ignore` от удаления/rename; UI/API дают
  создать/переименовать custom list и редактировать каждое из этих policy fields.
- [ ] `GET/POST/PUT/DELETE /api/admin/rules` по canonical Ticket 03 schema:
  `priority`, `scope`, `match.type/value`, `instruction`, `stop_processing`.
  IDs immutable; update/delete не оставляет orphan index.
- [ ] `GET /api/admin/tone` возвращает `global`, `chat_override` и `effective` config;
  `PUT /api/admin/tone` принимает explicit `scope="global"|"chat"` и `tone_mode`, canonical preset,
  `custom_system_prompt`, `judge_default_n` (`5..30`); `DELETE /api/admin/tone/override`
  атомарно очищает chat override. Update сразу применяется к
  следующим jobs. API принимает только canonical
  `neutral|serious|scientist|street|sarcastic_robot`; chat-only alias
  `sarcastic` сюда не попадает.
- [ ] `GET /api/admin/logs` читает bounded history только allowed chat.
  `DELETE /api/admin/logs` (super-admin + повторное подтверждение UI) полностью
  отменяет non-terminal jobs, удаляет indexed private job snapshots/answers и
  purges history; arbitrary `chat_id` не принимается. QStash callback на уже
  удалённый/cancelled job отвечает terminal `2xx` без side effect.
- [ ] Все mutation API используют typed JSON schema, reject unknown fields,
  request-size limits и понятные 4xx; никаких partial writes при validation error.

## CSRF, XSS и HTTP security

- Unsafe methods требуют одновременно:
  - same-origin `Origin` (или строго проверенный `Referer` fallback);
  - `X-CSRF-Token`, constant-time сравнимый с token server session.
- CORS не открывается внешним origins; auth endpoints имеют rate limit.
- UI никогда не рендерит logs, names, usernames, rule text, custom prompt или
  error через `innerHTML`. Только `textContent`/DOM APIs; URL attributes проходят
  allowlist.
- Нет inline JS/event handlers. `app.js` подключён `defer`, CSS локальный.
- Vercel headers включают CSP примерно
  `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self';
  img-src 'self' data: https:; frame-ancestors 'none'; base-uri 'self';
  form-action 'self' https://oauth.telegram.org`, плюс `nosniff`, строгий
  Referrer-Policy и HSTS на production.
- Ошибки API не возвращают stack trace/secrets. Для state-changing ответов
  `Cache-Control: no-store`; auth/admin pages не кэшируют приватные JSON.

## Frontend (`public/`)

- [ ] Стартовый экран fetch-ит `/api/public/config`, показывает имя бота и кнопку
  «Войти через Telegram», ведущую на `/api/auth/telegram/start`.
- [ ] После login `/api/admin/me` определяет роль; 401 возвращает на login screen.
- [ ] Разделы:
  - **Пользователи и списки:** observed search, ввод ID/username, memberships,
    создание/редактирование title/enabled/priority/applies_to/`injected_prompt`,
    назначение admin (только super-admin);
  - **Rules:** match type/value, scope, priority, instruction, stop flag,
    deterministic preview order;
  - **Tone:** canonical presets, custom mode/prompt, `judge_default_n`, visible
    global/chat/effective value, explicit global/current switch и clear override;
  - **Logs:** последние записи, edit/reply/bot markers, purge action;
  - **Privacy:** retention values, copyable participant notice, delete observed
    profile и purge history.
- [ ] У каждой mutation есть loading/error/success state; destructive actions
  требуют явного подтверждения и после успеха перечитывают server state.
- [ ] 401/403 различаются: expired/revoked session предлагает перелогиниться,
  недостаточная роль не скрывается под generic network error.

## Privacy lifecycle

- UI показывает фактические значения: максимум 30 сообщений, per-record cutoff и
  sliding key TTL `HISTORY_RETENTION_SECONDS` (default 30 дней), job snapshots 7
  дней.
- До production acceptance admin публикует в группе notice: message/job TTL,
  что observed profile/list membership живут до удаления, что context
  отправляется NVIDIA, что Tavily получает только обезличенные factual queries,
  и как попросить удаление.
- Purge/delete использует `jobs:chat:*`/`jobs:user:*`, отменяет queued jobs и
  сразу удаляет private Redis snapshots/history. Worker проверяет cancellation
  перед provider call и delivery. QStash не содержит transcript; внешний запрос,
  уже начавшийся до cancellation, и данные, ранее отправленные providers,
  отозвать нельзя — это честно указано в notice.
- Full prompts/transcripts не показываются в application logs или error records.

## Вне объёма

- Multi-chat/tenant роли: продукт намеренно ограничен одним allowed chat.
- Разрешение произвольного Telegram username через MTProto.
- SPA framework/build pipeline и legacy Telegram Login Widget.

## Автоматические проверки

- [ ] OIDC: state one-time/expiry/mismatch, missing/wrong/swapped browser-binding
  cookie, login-CSRF/session-swapping, atomic consume, PKCE S256, minimal scope, token
  exchange error, JWKS rotation, wrong alg/signature/iss/aud/nonce, future `iat`,
  expired token, distinct `sub` и numeric Telegram `id` не перепутаны.
- [ ] Session: fixed HS256 allowlist, reject `none`/algorithm confusion, canonical
  string `sub` + matching numeric `tg_user_id`, issuer/audience/expiry/JTI/server
  record, logout, CSRF/origin, remove-admin immediate revocation, super-admin
  invariant; missing `SUPER_ADMIN_ID` fails production readiness, empty Redis
  `admins` still permits configured super-admin login.
- [ ] Public config не содержит secrets; settings never serialized wholesale.
- [ ] Username: known/current, unknown 422, rename stale alias, collision-safe
  cleanup; unseen numeric list member разрешён; unseen numeric **group member**
  может стать admin после `getChatMember`, non-member запрещён.
- [ ] CRUD validation/authorization, allowed-group membership recheck,
  deterministic entities, reserved ignore, `judge_default_n`, no arbitrary chat
  ID, user/chat purge cancels indexed jobs, erases snapshots and removes indexed
  outbound bot history records.
- [ ] DOM/XSS fixtures with `<script>`, event attributes and malicious URLs;
  CSP/security/cache headers.
- [ ] Ticket 01–04 tests, `ruff`, CI Python 3.12 остаются зелёными.

## Критерии приёмки (live E2E)

1. На зарегистрированном стабильном Vercel URL super-admin проходит Telegram
   OIDC и видит роль; аккаунт вне allowlist после валидного OIDC получает отказ.
2. Прямой admin API без cookie, без CSRF и с чужим Origin отклоняется. Удалённый
   admin теряет доступ на следующем request, даже если JWT ещё не истёк.
3. В UI создать auto rule «бред» → обычное сообщение в группе вызывает бота без
   redeploy. Изменить priority/stop → порядок меняется детерминированно.
4. В UI создать custom list с `injected_prompt`, priority и `applies_to`, добавить
   **observed** `@user_x` → его следующий explicit ответ меняется. Неизвестный username
   даёт понятный 422; тот же numeric ID можно добавить.
5. Выбрать global `street`, затем chat `custom` и сохранить prompt →
   следующие flash replies меняются; clear override возвращает global.
   `judge_default_n` влияет на `/judge` без redeploy.
6. Super-admin добавляет второго admin по numeric ID после
   успешного group-membership check; тот меняет tone, но не управляет admins.
   После revoke его открытая UI session сразу перестаёт работать.
7. Logs безопасно показывают malicious-looking text как текст. Purge удаляет
   history; delete user очищает indexes/memberships. Participant notice
   опубликован в тестовой группе.
8. Production response headers соответствуют CSP/security contract, а network
   trace frontend не содержит bot/OIDC client secrets, provider keys или raw
   session JWT в JavaScript-readable storage.

## Риски

- Preview domains нужно отдельно регистрировать в BotFather; основной E2E идёт на
  стабильном домене с точным callback URI.
- Static UI особенно чувствителен к DOM XSS из chat/rule text — CSP и
  `textContent` обязательны, не косметичны.
- JWT без Redis/session/admin recheck не даёт немедленного revoke; обе проверки
  являются частью acceptance, а не будущим улучшением.
