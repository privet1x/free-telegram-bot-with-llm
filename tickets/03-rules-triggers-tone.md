# TICKET-03 — Детерминированные rules/lists, auto-trigger и управление тоном

**Размер:** L · **Зависит от:** 02 · **Разблокирует:** 04
**Общий контракт:** `00-ARCHITECTURE.md` (§4 и §6)

## Цель

Сделать поведение бота управляемым данными: встроенные/кастомный тон,
персональные политики через списки, текстовые правила и автоматическое
вмешательство без mention. Этот тикет реализует prompt-слои **1–4**. Слой 5
`/judge` остаётся Ticket 04.

Главный E2E этого тикета — обычное «это бред» без упоминания действительно
доходит до processor. Поэтому matching/routing добавляется в webhook, а не
только в worker, который никогда бы не получил такое сообщение.

## Канонические модели

### Tone config

```json
{
  "tone_mode": "preset",
  "tone_preset": "neutral",
  "custom_system_prompt": null,
  "judge_default_n": 20
}
```

- `tone_mode`: `preset` или `custom`.
- Встроенные canonical slug: `neutral`, `serious`, `scientist`, `street`,
  `sarcastic_robot`. Обязательные PRD presets — neutral/scientist/street/
  sarcastic robot; `serious` — сдержанный пресет для прямо указанной в
  PRD команды `/set_mode serious`.
- В `custom` mode непустой `custom_system_prompt` заменяет base preset; в
  `preset` mode кастомный текст хранится, но не активен.
- Chat config `cfg:{TELEGRAM_ALLOWED_CHAT_ID}` перекрывает `cfg:global` по
  отдельным полям; неизвестные/пустые значения fail validation, а не создают
  новый неявный preset.

### Персональные списки

```json
{
  "slug": "aggressive",
  "title": "Агрессивный ответ",
  "enabled": true,
  "priority": 50,
  "applies_to": ["explicit", "auto", "judge"],
  "injected_prompt": "Отвечай этому пользователю саркастично, без оскорблений."
}
```

- Membership всегда numeric Telegram `user_id`.
- Персональное поведение выражается списком + `injected_prompt`; отдельного
  `rule.kind=personal` нет.
- Reserved slug `ignore` нельзя удалить/переименовать. Он не инжектит prompt и
  подавляет только автоматические ответы; явный mention/reply доступен.

### Текстовые правила

```json
{
  "id": "bred",
  "enabled": true,
  "priority": 50,
  "scope": "all",
  "match": {"type": "substring", "value": "бред"},
  "instruction": "Аргументированно объясни, почему обсуждаемое не является бредом.",
  "stop_processing": false
}
```

- `scope`: `auto`, `explicit`, `judge`, `all`. `auto`/`all` может создать job без mention;
  `explicit`/`all` добавляет policy к явному Flash request, `judge`/`all` — к
  judge snapshot Ticket 04.
- `match.type`: `substring`, `word`, `phrase`. Regex вне MVP.
- Нормализация для matching: NFKC → casefold → punctuation/whitespace
  normalization. `word` сравнивает полный Unicode-токен; `phrase` сохраняет
  границы нормализованных слов; `substring` предназначен для корня.
- Валидация: непустой value/instruction, ограничение длины, `priority` в
  `[-1000,1000]`, безопасные ID/slug. Исходный текст не модифицируется для LLM.

## Детерминированность и конфликты

1. Совпавшие rules сортируются `priority DESC, id ASC`.
2. Rules обрабатываются группами одинакового priority в порядке `id ASC`.
   Включается вся текущая группа; если хотя бы у одного её rule
   `stop_processing=true`, все группы с меньшим priority отбрасываются.
3. Активные member-lists сортируются `priority DESC, slug ASC`.
4. Сначала base tone, затем проверенные numeric actor ID/admin status, затем list
   policies и matched rules. Telegram name/username и сырые сообщения идут
   последним недоверенным data-блоком.
5. На один job действует максимум 10 list policies и 10 rule policies после
   сортировки; превышение логируется метрикой и не раздувает prompt бесконечно.
6. Для стабильности enqueue сохраняет в `request_json.effective_policy` уже
   выбранные tone text, вычисленный `is_admin`, list policies и matched rule
   snapshots. Изменение Redis после enqueue влияет на следующие, не текущий job.

## Объём работ

### 1. Stores и validation

- [ ] `app/store/config_store.py`: merge global/chat config, get/set
  `tone_mode`, preset, custom prompt; optimistic/atomic update, schema validation.
  Mutation явно выбирает `scope="global"` (`cfg:global`) или `scope="chat"`
  (`cfg:{allowed_chat_id}`); есть atomic clear chat override, а read возвращает global,
  override и effective config.
- [ ] `app/store/lists.py`: CRUD meta/membership, `member_lists(user_id, kind)`,
  deterministic sort, reserved `ignore`; не оставлять orphan keys/index entries.
- [ ] `app/store/rules.py`: CRUD, normalization/matching, deterministic resolution,
  scope filter; не оставлять orphan keys/index entries.
- [ ] Расширить/проверить `app/store/admins.py` из Ticket 02: list/add/remove;
  super-admin из env всегда true и не может быть удалён.
- [ ] Ограничить admin-authored prompt/instruction по размеру и показывать в
  API validation error конкретное поле.

### 2. Webhook routing — обязательное расширение

После Ticket 02 routing меняется так:

1. После secret/allowed-chat gate `edited_message` только upsert-ится и не
   запускает command/explicit/auto routing второй раз.
2. Для нового message сначала распознать route и зафиксировать pre-trigger
   snapshot durable helper-ом Ticket 02; command text не участвует в auto rules.
3. Для явного mention/reply сопоставить rules `explicit|all`, собрать
   `effective_policy`, создать обычный reply job. `ignore` и auto cooldown его
   не подавляют.
4. Для обычного text сопоставить rules `auto|all`. Если совпадений нет — только
   history. Если автор в `ignore` — только history.
5. До нового matching сначала загрузить `job:{update_id}`: существующий
   `received` auto job всегда продолжает history/publish и не подавляется чужим
   cooldown либо новой конфигурацией.
6. Для нового auto route одной Lua/CAS-операцией проверить отсутствие cooldown,
   записать owner value `{update_id}:{random_token}` с EX и создать durable
   `received` job со snapshot. Нельзя оставлять crash window «cooldown есть, job
   ещё нет».
7. Затем upsert current history/user и publish по QStash dedup ID. Известная
   publish failure **не** снимает cooldown: `received` job остаётся его owner,
   Telegram retry сначала находит этот job и может повторить publish, а новые
   auto updates до EX подавляются. Cooldown compare-delete разрешён только
   owner-токеном в той же атомарной операции, которая удаляет/отменяет сам
   job; нельзя оставить resumable job без его blocker.

Matching в webhook — дешёвая Redis/CPU операция; LLM там не вызывается.
Processor не принимает решение «ответить ли» заново: он исполняет сохранённый
route/effective policy. Это предотвращает конфигурационный drift.

### 3. Prompt builder и processor

- [ ] Расширить `build_reply_messages`:
  - layer 1 — effective preset или custom base;
  - layer 2 — trusted numeric actor ID и вычисленный `is_admin`;
  - layer 3 — sorted list instructions;
  - layer 4 — sorted matched rule instructions;
  - final user/data — Telegram name/username, transcript, reply target, current
    message.
- [ ] Любой текст/username/name из Telegram сериализуется как данные. Даже если
  в transcript написано «ignore previous system», это не меняет trusted policy.
- [ ] Processor обслуживает `reply` и `auto_rule` одним flash client
  `deepseek-ai/deepseek-v4-flash`; delivery/split/retry полностью общий с Ticket 02.

### 4. Команды из чата

Только admin/super-admin разрешённого чата:

- [ ] `/tone <neutral|serious|scientist|street|sarcastic_robot>` и alias
  `/set_mode <slug>`: выбрать preset для current allowed chat (`cfg:{chat_id}`)
  и `tone_mode=preset`, не удаляя сохранённый custom prompt. `/tone global <slug>` /
  `/set_mode global <slug>` меняет fallback `cfg:global`; `/tone clear` удаляет
  chat override и возвращается к global.
- [ ] Ergonomic value alias `sarcastic` канонизируется в
  `sarcastic_robot`; `/tone sarcastic` и `/set_mode serious` из PRD оба
  работают. Config/API хранят только canonical slug.
- [ ] `/mode`: показать active mode/preset и разрешённые slug.
- [ ] Команды с `@suffix` исполняются только при suffix текущего бота.
- [ ] Не-админ получает короткий отказ; invalid slug — usage + список slug.
- [ ] Command path идемпотентен: после history/user upsert одна Lua/CAS
  операция create-if-absent пишет `cmd:{update_id}` с TTL 30 дней,
  применяет typed canonical config value и ставит финальный
  `dedup:update:*`. Только winner возвращает короткий Telegram webhook reply;
  retry по existing command receipt не меняет config и отвечает `200` без второго
  reply. Crash до atomic operation не меняет ничего; crash после неё не
  повторяет mutation. Это также не даёт старому Telegram retry откатить
  более новую UI/chat настройку.
- [ ] Telegram webhook reply не возвращает Bot API `message_id`, поэтому он
  честно не добавляется в canonical history. В history попадают только
  outbound `sendMessage`/`editMessageText` с реальным result ID.

### 5. Runtime config

- [ ] Settings/`.env.example` добавляют `AUTO_TRIGGER_COOLDOWN_SECONDS` и bounded
  `MAX_LIST_POLICIES=10`/`MAX_RULE_POLICIES=10`; production validation требует
  positive cooldown и current Ticket-02 queue dependencies. Health не считается ready,
  если auto routing включён, а queue/config dependency отсутствует.

### 6. Seed

- [ ] `scripts/seed.py` идемпотентно создаёт reserved `ignore`, demo list
  `aggressive` и demo rule `bred` со `scope="all"`, но не перезаписывает
  изменённые админом сущности без явного `--force`.
- [ ] Скрипт принимает только разрешённый chat config, возвращает non-zero при
  Redis/API ошибке и не печатает секреты.

## Ошибки и лимиты

- Неуспешный rules/config read для потенциального auto-trigger не означает
  «молча не отвечать»: webhook возвращает retryable error до успешного enqueue.
- Cooldown применяется только к auto-trigger и атомарно создаётся вместе с job
  до publish. Publish failure оставляет и resumable job, и его blocker до EX;
  только retry именно этого update обходит свой cooldown. Suppressed update всё
  равно получает history + final dedup marker.
- Rule/list policy из Redis считается trusted только потому, что её API будет
  admin-only; до Ticket 05 изменения доступны лишь seed/manual operations.
- Слишком много совпадений не меняет сортировку; применяется документированный
  cap, а не случайный порядок Redis SET.

## Вне объёма

- Judge-layer, grounded facts и pro — Ticket 04.
- Web CRUD/UI и username resolution — Ticket 05.

## Автоматические проверки

- [ ] Все match types, Unicode/NFKC/casefold, punctuation и false positives.
- [ ] Deterministic rule/list ordering, equal priorities, stop processing, cap.
- [ ] Scope matrix (`auto|explicit|judge|all`), ignore semantics и admin status layer.
- [ ] Unmentioned auto message создаёт job; no-match не создаёт; command не
  запускает rule; mention получает explicit/all rules; edit не создаёт job.
- [ ] Auto cooldown parallel race, crash между gate/job (atomic invariant),
  publish failure сохраняет owner blocker, retry существующего `received`
  job обходит его, а новый update при чужом token подавляется.
- [ ] Tone commands: admin/non-admin, foreign suffix, invalid slug,
  `sarcastic→sarcastic_robot`, canonical `serious`, parallel duplicate and
  crash before/after atomic config+command-receipt+dedup, chat/global/clear scope,
  custom text сохраняется при переключении preset.
- [ ] Prompt-role snapshot: raw transcript отсутствует в system content.
- [ ] Ticket 01–02 tests, `ruff`, CI Python 3.12 остаются зелёными.

## Критерии приёмки (live E2E)

1. Любой не-ignored user пишет «это полный бред» **без mention** — webhook ставит
   `auto_rule` job, бот отвечает с rule instruction.
2. Два rules одинакового priority дают один и тот же порядок на повторных тестах;
   `stop_processing` отсекает только ожидаемые rules.
3. Участник `aggressive` получает его policy, другой участник — нет. Участник
   `ignore` не вызывает auto rule, но получает ответ на явный `@bot`.
4. Admin выполняет `/tone scientist`, `/tone sarcastic`, `/set_mode serious`,
   затем `/tone global street` и `/tone clear`; aliases канонизируются, current/global
   precedence видна, config сохраняет valid slug и стиль меняется.
5. Не-admin не меняет тон. В prompt contract присутствует вычисленный admin
   status, а raw chat остаётся в user/data role.
6. Серия auto-trigger сообщений даёт не более одного job за cooldown window;
   явный mention в этот момент всё равно обслуживается.

## Риски

- Автоответы расходуют NIM/QStash quota — cap/cooldown/ignore обязательны.
- `substring` для корня сознательно даёт больше совпадений; UI должен объяснять
  различие с `word`/`phrase`.
- Admin-authored custom policy имеет высокий приоритет и потенциально опасна;
  жёсткий лимит размера, typed validation, admin-only mutation и немедленный
  session/role recheck обязательны в Ticket 05.
