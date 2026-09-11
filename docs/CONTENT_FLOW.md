# Поток Контента

## Быстрое объяснение панели

Чтобы не путаться в новых кнопках:

- `Поступившие сообщения` — это сырые входящие `submissions`, которые еще не стали постами.
- `Все сообщения` — это журнал всех `submissions`, уже лежащих в новой БД.
- `Черновики на review` — это `content_items`, то есть почти готовые посты после сборки.
- `Пасты` — это библиотека `paste_library`.

Подробное описание кнопочной панели есть в [docs/TELEGRAM_PANEL.md](docs/TELEGRAM_PANEL.md).

## 1. Legacy Inbox

Пользователь пишет в предложку. Старый бот сохраняет запись в legacy SQLite-таблицу `sender_info`.

Эти поля не ломаются и продолжают использоваться:

- `user_id`
- `channel_id`
- `bot_username`
- `username`
- `first_name`
- `message_id`
- `chat_id`
- `text_post`
- `timestamp`
- `id`

## 2. Importer

Сервис [src/editorial/services/import_legacy.py](/D:/VS%20projects/bot_pslshke/IdeaFlowBot/src/editorial/services/import_legacy.py:1):

- читает новые строки из `sender_info`;
- ищет или создаёт записи каналов в `channels`;
- чистит текст;
- строит `normalized_text` и `text_hash`;
- пытается определить простые теги;
- создаёт записи в `submissions`.

Главная идея: один legacy row импортируется только один раз через `legacy_source + legacy_row_id`.

Теперь старый collector сохраняет входящие сообщения в legacy inbox сразу при приёме, а не только после старого approve-flow. Это позволяет новой панели видеть реальные поступающие сообщения почти сразу после `import`.

В legacy-чате модерации у сообщения есть две разные кнопки одобрения: старая кнопка `Одобрить` публикует сразу в канал, а кнопка `Одобрить в слот` создаёт/одобряет `content_item` и отдаёт его новому scheduler для публикации в свободный слот.
После `Одобрить в слот` появляется кнопка `Отменить слот`: она переводит `content_item` в `hold`, очищает `scheduled_for` и отменяет ещё не опубликованные scheduled-логи. Уже опубликованный в Telegram пост таким действием не удаляется.

## 3. Review Submissions

С `submissions` можно сделать несколько вещей:

- отклонить;
- оставить как source для генерации;
- отметить как paste candidate;
- создать `content_item`.

Сам `submission` не публикуется напрямую.

## 4. Content Items

`content_items` — это единый публикуемый объект.

Он может быть создан:

- из `submission`
- из `paste_library`
- генератором как `generated`
- вручную как `editorial`

Основные статусы:

- `draft`
- `pending_review`
- `approved`
- `scheduled`
- `published`
- `rejected`
- `hold`

## 5. Review Content Items

Модератор работает уже с `content_items`.

Можно:

- approve
- reject
- hold
- edit_and_approve

История сохраняется в таблицу `reviews`.

## 5.1 Telegram-Panel Review

Теперь для базового управления не обязательно идти в HTTP API.

Через `/panel` в главном боте модератор может кнопками:

- импортировать новые сообщения;
- открыть список pending submissions;
- approve / reject / hold;
- сделать `publish now`;
- сохранить submission как paste;
- открыть pending content items и одобрить их.

## 6. Пасты

Паста — это не AI-текст и не прямой `submission`.

Паста — отдельная библиотечная запись в `paste_library`, которую можно:

- сделать из `submission`
- сделать из `content_item`
- создать вручную

Важно:

- паста не публикуется напрямую;
- сначала из неё создаётся `content_item(source_type='paste')`;
- затем этот item проходит review и только потом может попасть в планировщик.

## 7. Генерация

Генерация сделана специально простой:

- выбираются 3-6 подходящих source submissions;
- собирается короткий prompt;
- provider создаёт 2-3 варианта;
- варианты сохраняются как `content_items(source_type='generated')`;
- статус у них всегда `pending_review`.

Автопубликации generated-контента нет.

## 7.1 Теги

Теги больше не зашиты только в код.

Editorial-слой хранит управляемый словарь:

- `tag_definitions` — сами теги;
- `tag_keywords` — ключевые слова для автоопределения;
- `paste_tag_assignments` — auto/manual теги конкретных паст;
- `channel_paste_tag_rules` — правила каналов для паст по тегам.

У `submissions`, `content_items` и `paste_library` остаются поля `tags` / `primary_tag`.
Это рабочий кэш для панели, scheduler и быстрых проверок.

Для паст важно:

- auto-теги пересчитываются по ключевым словам;
- manual-теги ставит админ через панель;
- manual-теги не удаляются при пересчёте auto-тегов;
- `primary_tag` используется для cooldown по тегу.

Правила каналов применяются именно к пастам:

- `include` — если есть хотя бы одно active-правило, канал берёт только пасты с этими тегами;
- `exclude` — пасты с этими тегами не берутся;
- `exclude` сильнее `include`.

## 8. Планировщик

### 8.1 Профили Каналов По Подписчикам

Каналы могут автоматически получать настройки из профилей `channel_setting_profiles`.

Профиль задает:

- диапазон подписчиков: `min_subscribers` / `max_subscribers`;
- параметры публикации: `min_slots_per_day`, `max_posts_per_day`, `max_paste_per_day`, cooldown, gaps;
- параметры автослотов: `auto_slots_enabled`, окно публикаций, время построения плана;
- разрешения: `allow_pastes`, `allow_generated`.

Фоновый sync:

1. Берет привязки сабботов из legacy `bots_data`.
2. Через токен саббота вызывает Telegram `get_chat_member_count`.
3. Сохраняет `subscriber_count` и `subscriber_count_checked_at` в `channels`.
4. Если `settings_profile_auto_enabled=true`, выбирает профиль по диапазону подписчиков.
5. Применяет настройки профиля к каналу.

Ручной override:

- `settings_profile_auto_enabled=false` замораживает канал;
- после этого профиль и отдельные настройки можно менять вручную;
- следующий auto-sync такой канал пропускает.

Команды:

```bash
python -m src.editorial.cli sync-channel-profiles
python -m src.editorial.cli upsert-channel-profile --slug growing --min-subs 50 --max-subs 999 --set min_slots_per_day=4 --set max_posts_per_day=6 --set max_paste_per_day=3
python -m src.editorial.cli apply-channel-profile --channel-id 1 --profile growing
```

В Telegram-панели есть раздел `Профили`:

- после входа показываются кнопки всех текущих профилей и кнопка `Добавить профиль`;
- `Добавить профиль` принимает строку `slug :: title :: min_subscribers :: max_subscribers`;
- при открытии профиля есть кнопки `Настроить профиль` и `Выставить профиль`;
- `Настроить профиль` показывает текущие параметры и принимает строку `<field> <value>`, например `max_posts_per_day 8`;
- `Выставить профиль` принимает номера каналов (`1,2,3`, `1-20`, `all`);
- ручное выставление переводит каналы в manual mode (`settings_profile_auto_enabled=false`), чтобы auto-sync не перезаписал выбор.

### 8.2 Автослоты

Канал может сам строить слоты на текущий день через автополитику:

- `auto_slots_enabled` — включает автоматическое построение слотов;
- `auto_slots_plan_time` — локальное базовое время канала для построения плана на текущий день;
- `auto_slots_window_start` / `auto_slots_window_end` — рабочее окно, внутри которого слоты распределяются равномерно;
- `auto_slots_replace_manual` — если включено, автоплан заменяет слоты выбранного дня недели;
- `auto_slots_last_planned_for` — дата, для которой уже был построен последний план.

Расчет:

1. Считаются готовые `approved` записи канала из живого контента (`submission` и `editorial`), которые еще не запланированы.
2. Если пасты разрешены, fallback равен `max_paste_per_day`; если пасты отключены, fallback равен `0`.
3. `target_slots = max(approved_ready_count, fallback_paste_slots, min_slots_per_day)`.
4. `target_slots` ограничивается `max_posts_per_day` и вместимостью окна с учетом `min_gap_minutes`.
5. `paste_slots = min(target_slots - approved_ready_count, fallback_paste_slots)`.
6. Слоты равномерно раскладываются между `auto_slots_window_start` и `auto_slots_window_end`.

Чтобы одновременное время у большого числа каналов не создавало пиковую нагрузку, каждому каналу назначается
стабильное смещение от `0` до `25` минут после `auto_slots_plan_time`. Смещение определяется по `channel_id`,
не меняется при перезапуске и равномерно распределяет каналы по минутам. Отдельный worker проверяет автослоты
каждую минуту. Пять минут оставлены как запас на polling и сам расчет. Жесткий предел планирования — `30`
минут после `auto_slots_plan_time`; следующий день заранее не планируется.

Если живого контента мало или нет, оставшиеся слоты займут пасты. Если живого контента больше лимита паст, пасты вытесняются. Дальше обычный scheduler использует эти слоты и применяет уже существующий `slot_jitter_minutes`.

Команда ручного запуска:

```bash
python -m src.editorial.cli auto-slots
```

Планировщик:

- смотрит активные каналы;
- смотрит их `channel_slots`;
- проверяет лимиты на день;
- учитывает min gap;
- учитывает cooldown по тегу, шаблону и пастам;
- учитывает include/exclude правила тегов паст для канала;
- проверяет exact/near duplicates;
- выбирает лучший `approved` item.

Приоритет выбора:

1. `submission`
2. `paste`
3. `generated`

Если для канала нет подходящего `approved` real content, scheduler теперь может сам:

- взять доступную пасту из `paste_library`;
- выбрать пасту, которая давно не использовалась;
- создать из неё `content_item(source_type='paste')`;
- автоматически поставить его в `approved`;
- и уже после этого запланировать на слот.

Если хорошего контента нет, слот остаётся пустым.

Через Telegram-панель можно отдельно:

- создать стандартные слоты для канала;
- вручную запустить scheduler;
- вручную запустить publisher.

## 9. Publisher

Publisher берёт записи со статусом `scheduled` и:

- находит bot token через legacy `bots_data`;
- отправляет текст в Telegram канал;
- если `PUBLICATION_SIGNATURE_ENABLED=true`, добавляет в конец публикации кликабельную подпись канала со ссылкой на публичный `t.me`;
- если `PUBLICATION_SIGNATURE_SKIP_IF_ADMIN_BOT_USERNAME` заполнен и этот бот уже является админом канала, подпись канала для публикации не добавляется;
- при успехе ставит:
  - `content_item -> published`
  - `publication_log -> sent`
- при временной ошибке Telegram (`timeout`, `429`, `5xx`, сетевой сбой):
  - оставляет `publication_log -> scheduled`;
  - оставляет `content_item -> scheduled`;
  - записывает `retry_after`, `attempt_count` и `last_attempt_at`;
  - автоматически повторяет отправку позже с увеличивающейся паузой;
- только при постоянной ошибке данных или конфигурации ставит:
  - `publication_log -> failed`;
  - `content_item -> approved`.

Telegram-вызовы имеют короткий общий timeout. Publisher блокирует в БД только одну публикацию и фиксирует результат после каждой записи, поэтому зависший канал не удерживает весь пакет.
