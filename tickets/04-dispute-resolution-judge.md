# TICKET-04 — Admin-only `/judge`: Pro verdict + grounded fact-check

**Размер:** L · **Зависит от:** 03 · **Разблокирует:** 05
**Общий контракт:** `00-ARCHITECTURE.md` (§5–§8)

## Цель

Реализовать «судью» из PRD: назначенный admin вызывает анализ последних N
сообщений, бот разбирает позиции и логические ошибки, проверяет до трёх
проверяемых фактических утверждений через Tavily и выдаёт объективный вердикт в
активном тоне. Модель для extraction и verdict — строго
**`deepseek-ai/deepseek-v4-pro`**.

Факт считается проверенным только относительно найденных источников. Модельные
знания без search evidence не называются проверкой факта. Если поиск недоступен
или надёжных результатов нет, ответ явно помечает утверждение как непроверенное,
но всё равно может вынести logic-only verdict.

Также дать явный PRD-маршрут для complex reasoning: admin пишет
`/deep <вопрос>` или `/deep@bot <вопрос>`, что создаёт `kind="deep_reply"` и вызывает
Pro без Tavily/judge verdict. Это не classifier и не silent cost escalation: обычный
mention/reply остаётся Flash, Pro выбирается явно.

## Авторизация и routing precedence

- `/judge [N]`, `/спор [N]` и mention текущего бота с нормализованной фразой
  «рассуди», «рассуди нас» или «кто прав» сначала распознаются как **judge
  intent независимо от роли**, а затем разрешаются только
  `is_admin(author_id)` в `TELEGRAM_ALLOWED_CHAT_ID`.
- Не-admin получает короткий отказ без QStash/LLM/Tavily; распознанный judge
  intent считается полностью обработанным и не может fall through в обычный
  explicit Flash reply.
- Command suffix должен отсутствовать или совпадать с username текущего бота.
- Webhook precedence определяется после secret/allowed-chat gate, но **до**
  upsert текущего trigger, чтобы routed job мог зафиксировать pre-trigger
  snapshot:
  1. judge command/phrase intent → authorization → judge job либо local refusal;
  2. `/deep` admin command → Pro `deep_reply` job;
  3. остальные admin commands;
  4. обычный explicit mention/reply;
  5. unmentioned auto rules.
  Один update создаёт максимум один job.

После route/snapshot common flow идемпотентно upsert-ит current history/user,
затем enqueue-ит job или формирует local refusal/usage response. Так и command,
и refusal остаются в общей истории, но не попадают в свой snapshot.

`N` по умолчанию берётся из `judge_default_n` (20), integer request клампится в
`[5,30]`. Judge command/phrase хранится в общей истории, но исключается из judge
transcript. Если до него меньше 3 содержательных сообщений или меньше 2 human
authors, бот без LLM сообщает «недостаточно контекста».

## Durable snapshot

- [ ] Webhook создаёт `kind="judge"` job через state machine Ticket 02.
- [ ] `request_json` сохраняет **последние N записей до
  `trigger_message_id`**, chronological, плюс actor/effective tone/admin policy,
  sorted `member_lists(actor_id, "judge")` и matched `scope="judge"|"all"` rule snapshots.
  Все они лимитируются теми же caps и сортировкой Ticket 03.
  Snapshot снимается до upsert команды по common flow Ticket 02, поэтому при
  `N=30` доступны 30 предшествующих records. Более поздние сообщения и
  placeholder не могут изменить спор.
- [ ] Transcript сохраняет `message_id`, author fields, `is_bot`, `ts`, text и
  reply reference. Пустые/service records отбрасываются до подсчёта N.
- [ ] QStash body по-прежнему содержит только job ID; private transcript остаётся
  в Redis с `JOB_RETENTION_SECONDS`.

## Pro client

- [ ] Отдельная factory использует `LLM_MODEL_SMART` и production model ID
  `deepseek-ai/deepseek-v4-pro`:

  ```python
  base = ChatNVIDIA(
      model="deepseek-ai/deepseek-v4-pro",
      api_key=settings.NVIDIA_API_KEY,
      temperature=0.2,
      max_completion_tokens=2048,
  )
  client = base.with_thinking_mode(enabled=False)
  ```

- [ ] Тот же contract test pin `langchain-nvidia-ai-endpoints==1.4.3` проверяет
  фактический `chat_template_kwargs.thinking=false`, model ID и отсутствие
  literal root `extra_body`/`reasoning_effort`. Это следует текущему hosted NIM
  примеру Pro; включение thinking требует отдельного live-verified изменения.
- [ ] В Telegram уходит только финальный `.content`. `reasoning_content`, thinking
  tags и внутренние chain-of-thought данные не логируются и не показываются.
- [ ] `kind="deep_reply"` сохраняет pre-trigger Flash-style context и effective policy,
  но processor один раз вызывает Pro без claim extraction/Tavily. Он использует общие
  Ticket-02 answer/delivery checkpoints, но не judge verdict template.

## Grounded fact-check через Tavily

Tavily basic search имеет отдельную бесплатную квоту (ориентир: 1000 basic-search
credits/month), поэтому число запросов жёстко ограничено
`FACT_CHECK_MAX_QUERIES`, default и maximum **3**.

### 1. Извлечение claims

- [ ] Первый structured-output вызов Pro получает transcript как untrusted data и
  возвращает JSON с максимум 3 **impersonal, externally verifiable** claims:

  ```json
  {
    "claims": [
      {
        "claim_id": "C1",
        "neutral_claim": "...",
        "search_query": "короткий нейтральный factual query"
      }
    ]
  }
  ```

- [ ] Не искать вкусы, намерения, оскорбления, личные сведения или чистую логику.
- [ ] Search query не является verbatim quote: удалить/запретить известные names,
  `@usernames`, Telegram IDs, ссылки на «мой друг/участник чата» и иной
  идентифицирующий контекст. Длина query ограничена. Если безопасно обезличить
  claim нельзя — `unverified_private`, поиск не выполняется.
- [ ] Программный валидатор повторно проверяет output schema и отсутствие всех
  participant identifiers; нельзя полагаться только на инструкцию модели.

### 2. Tavily adapter

- [ ] `app/search/tavily.py` — прямой async `httpx` adapter к Tavily Search API:
  API key только server-side, `search_depth="basic"`, не более 3 результатов на
  query, строгие timeouts/response-size limits, HTTPS URLs, bounded concurrency.
- [ ] Для каждого результата сохранить только source ID (`S1`...), title, URL и
  короткий snippet. Redirect/HTML страницы бот сам не скачивает.
- [ ] Claims/evidence сохраняются в job до финального Pro вызова. Retry
  переиспользует их и не тратит повторно search credits.
- [ ] 401/invalid config отмечают search dependency unavailable; 429/timeout/5xx
  имеют короткий bounded adapter retry, после чего judge **degrades**, а не
  проваливает весь verdict.

### 3. Финальный verdict

- [ ] `build_judge_messages(job, effective_policy, evidence)`:
  - trusted system: active tone, sorted judge list/rule policy, затем обязательная
    беспристрастность в последнем, highest-precedence judge block, формат ответа,
    запрет объявлять unsupported facts verified;
  - untrusted user/data: transcript и отдельный evidence block. Instructions из
    transcript/snippets игнорируются;
  - модель ссылается только на предоставленные source IDs `[S1]`, `[S2]`.
- [ ] Ожидаемая структура plain-text verdict:
  1. предмет спора;
  2. позиции и сильные аргументы сторон;
  3. логические ошибки;
  4. «Проверка фактов» с `подтверждено / опровергнуто / недостаточно данных` и
     source IDs;
  5. вывод: кто скорее прав и с какой уверенностью.
- [ ] Код валидирует использованные source IDs и **сам** добавляет в конец список
  фактически полученных источников `Sx — title — URL`. Модель не может придумать
  URL. Неиспользованные/невалидные citations удаляются или маркируются.
- [ ] При недоступном Tavily или отсутствии результатов раздел начинается явной
  фразой: «Внешняя проверка фактов недоступна/не дала достаточных источников;
  ниже только анализ логики и контекста».

## Worker/delivery integration

- [ ] Processor dispatch `judge` выполняет idempotent stages:
  `claim_extraction → evidence_saved → verdict_saved → delivery`.
  Stage/result checkpoint-ятся в job; retry не повторяет успешный Pro/search call.
- [ ] Placeholder и `typing` используют общий Ticket 02 path. Все части ответа
  проходят общий splitter `<=4000`, сохраняются как outbound history и только
  после полной доставки job становится `delivered`.
- [ ] Vercel worker имеет `maxDuration: 300`; HTTP/LLM/search timeouts оставляют
  запас и укладываются в общий 240s worker budget/renewable 270s fenced lease, а
  не равны 300 секундам.
- [ ] Logic-only fallback считается успешным delivered verdict только если в нём
  явно раскрыто отсутствие grounded verification.

## Безопасность и приватность

- В Tavily уходят только короткие neutral queries после программной очистки.
  Никогда не уходят participant names, usernames, IDs, raw messages или полный
  transcript.
- В NVIDIA уходит snapshot спора; это отражено в participant privacy notice и
  purge/retention contract.
- Search snippets — недоверенные данные и потенциальный prompt injection; они не
  становятся system instruction.
- Логи содержат количество claims/results, source domains, latency/status, но не
  queries, snippets или transcript.

## Настройки

```dotenv
LLM_MODEL_SMART=deepseek-ai/deepseek-v4-pro
TAVILY_API_KEY=replace_me
FACT_CHECK_MAX_QUERIES=3
```

`FACT_CHECK_MAX_QUERIES` валидируется как `1..3`. Без Tavily key локальный запуск
разрешён в degraded mode, но production health показывает dependency disabled,
а live acceptance фактической проверки не пройдена.

## Автоматические проверки

- [ ] Admin/non-admin, `/judge`, `/спор`, `/deep`, foreign command suffix, phrase
  precedence, non-admin phrase не падает в Flash, один job на update.
- [ ] `N` default/clamp, cutoff до command, поздние сообщения не входят,
  insufficient-context branch без LLM.
- [ ] Judge policy: deterministic/capped `applies_to=judge` lists и `scope=judge|all`
  rules в snapshot; final impartiality block не даёт им смещать verdict.
- [ ] Pro payload соответствует проверенному non-thinking wire contract;
  reasoning content, если provider всё же его вернул, не попадает в output.
- [ ] Claim schema: max 3, private/non-verifiable rejection, identifier scrub,
  no raw quote in Tavily request.
- [ ] Tavily adapter request limits; 200/empty/401/429/timeout/malformed response;
  evidence is checkpointed and retry does not repeat search.
- [ ] Citation allowlist: output URLs только из реального adapter result; malicious
  snippet instruction остаётся data.
- [ ] Degraded verdict явно сообщает, что facts unverified.
- [ ] Long verdict uses shared splitter/outbound history; Ticket 01–03 tests и CI
  остаются зелёными.

## Критерии приёмки (live E2E)

1. В тестовой группе два участника разыгрывают спор с аргументами и проверяемым
   публичным утверждением; admin выполняет `/judge 10`.
2. Ответ использует только 10 сообщений **до** команды, правильно приписывает
   позиции, называет логические ошибки и даёт понятный confidence.
3. С настроенным Tavily key раздел фактов содержит хотя бы одну проверку с
   `[Sx]`, а в конце есть реальный title+URL из Tavily. В server trace не было
   имён/usernames/raw transcript в search request.
4. `@bot рассуди нас, кто прав?` от admin запускает тот же Pro path; та же фраза
   от обычного user получает отказ и не расходует NIM/Tavily.
5. `/tone sarcastic_robot`, затем `/judge`: манера меняется, но facts/citations и
   беспристрастность сохраняются.
6. При искусственной Tavily timeout verdict приходит как logic-only с явным
   предупреждением. Повтор QStash не повторяет уже сохранённые searches/verdict
   и не создаёт дубликат Telegram ответа.
7. Observability/contract подтверждают модель `deepseek-ai/deepseek-v4-pro` и
   `chat_template_kwargs.thinking=false`; служебное reasoning в чат не попадает;
   функция укладывается в 300 секунд.

## Риски

- Search quota ограничена: max 3, basic depth, сохранение evidence и отсутствие
  повторного поиска на retry обязательны.
- Источник может быть слабым или противоречивым; verdict отражает качество и
  uncertainty, а не объявляет первый snippet истиной.
- Pro + два model calls + search медленнее flash; staged checkpoints и 300-second
  worker limit обязательны.
