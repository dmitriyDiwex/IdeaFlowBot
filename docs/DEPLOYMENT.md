# Деплой И Сервер

## 1. Какой Сервер Нужен

Для такой системы достаточно обычного VPS.

Минимально разумно:

- 2 vCPU
- 4 GB RAM
- 30-40 GB SSD

Если каналов много и generation используется активно, лучше 4 vCPU и 8 GB RAM.

## 2. Какие Компоненты Запускаются

Проект состоит из нескольких процессов:

- `collector-bot` — старый Telegram collector
- `editorial-api` — HTTP API для review/import/manual actions
- `editorial-mcp` — защищённый MCP endpoint для модерации через Codex
- `editorial-importer` — переносит новые legacy сообщения в `submissions`
- `editorial-auto-slots` — раз в минуту распределяет автопланирование каналов внутри 30-минутного окна
- `editorial-scheduler` — раскладывает approved контент по слотам
- `editorial-publisher` — публикует scheduled контент в Telegram
- `postgres` — новая editorial база
- `redis` — резерв под фоновые задачи и расширение системы

## 3. Почему Здесь Есть И SQLite, И PostgreSQL

SQLite остаётся только для старого collector-слоя.

PostgreSQL нужен для новой редакционной системы, потому что там:

- больше таблиц;
- больше индексов;
- нужен `pg_trgm` для анти-повторов;
- нужна нормальная схема для scheduler/review/paste library.

## 4. Запуск Через Docker Compose

1. Скопируй `.env.example` в `.env`
2. Заполни обязательные переменные
3. Запусти:

```bash
docker compose up --build -d
```

После этого:

- PostgreSQL и Redis поднимутся как отдельные контейнеры
- API само прогонит `alembic upgrade head`
- importer/scheduler/publisher стартуют в цикле
- collector-bot поднимется отдельно
- `./data` будет общим volume для collector/importer/publisher, поэтому legacy SQLite не разъедется по разным контейнерам

## 5. Если Хочешь Запускать Процессы Вручную

Миграции:

```bash
alembic upgrade head
```

Collector:

```bash
python main.py
```

API:

```bash
uvicorn src.editorial.api.app:app --host 0.0.0.0 --port 8080
```

Importer:

```bash
python -m src.editorial.cli import-legacy
```

Auto slots:

```bash
python -m src.editorial.cli auto-slots
```

Scheduler:

```bash
python -m src.editorial.cli sync-channel-profiles
python -m src.editorial.cli schedule
```

`editorial-auto-slots` in `docker-compose.yml` checks channels once per minute. Channels sharing the same
`auto_slots_plan_time` receive stable offsets from 0 to 25 minutes, so they do not all hit PostgreSQL at once.
The planner changes only channels with `auto_slots_enabled=true`, never plans the next day early, and skips a
channel/date after a successful plan. `editorial-scheduler` continues to run independently every five minutes.

Each scheduler pass preloads active slots, paste cooldown history, reservations, and tag rules in bulk. `EDITORIAL_SCHEDULER_COMMIT_BATCH_SIZE` controls how many database-only scheduling changes are committed together (default: `25`). This does not change slot or paste eligibility rules.

`editorial-profile-sync` in `docker-compose.yml` updates channel subscriber counts and applies subscriber-based profiles once per hour.

Publisher:

```bash
python -m src.editorial.cli publish
```

Generation:

```bash
python -m src.editorial.cli generate --channel-id 1
```

## 6. Что Важно Настроить Перед Боевым Запуском

- `BOT_API_TOKEN`
- `GENERAL_ADMIN`
- `PUBLICATION_SIGNATURE_SKIP_IF_ADMIN_BOT_USERNAME`, если подпись нужно отключать в каналах, где уже админит отдельный бот подписей
- `EDITORIAL_POSTGRES_DSN`
- `EDITORIAL_REVIEW_API_KEY`
- `EDITORIAL_MCP_TOKEN` и `EDITORIAL_MCP_WRITE_ENABLED` для MCP-модерации
- `OPENROUTER_API_KEY`, если нужен OpenRouter
- слоты публикации `channel_slots`

Рекомендуемые настройки защиты от сетевых зависаний Telegram:

```env
TELEGRAM_REQUEST_TIMEOUT_SECONDS=15
TELEGRAM_RETRY_DELAY_SECONDS=60
EDITORIAL_PUBLISHER_RETRY_BASE_SECONDS=60
EDITORIAL_PUBLISHER_RETRY_MAX_SECONDS=900
```

Без слотов scheduler ничего не запланирует, и это нормально.

Подключение агента и безопасный порядок запуска описаны в `docs/MCP_MODERATION.md`.

Если пока хочешь жить без генерации, просто выставь:

```env
EDITORIAL_GENERATION_ENABLED=false
```

## 7. Как Понять, Что Всё Работает

Порядок проверки:

1. Collector продолжает принимать сообщения.
2. Importer создаёт `submissions`.
3. Через API видно pending submissions/content items.
4. Approved item попадает в `scheduled`.
5. Publisher отправляет пост в Telegram канал.
6. В `publication_log` появляется `sent`.

## 8. Более Простой Повседневный Сценарий

После локального запуска открой в Telegram главного бота и используй `/panel`.

Что умеет панель:

- модератор:
  - импорт новых сообщений;
  - просмотр incoming submissions;
  - approve / reject / hold / publish now;
  - создание paste из submission;
  - запуск scheduler/publisher;
  - создание стандартных слотов канала.
- генеральный админ:
  - всё вышеперечисленное;
  - добавление и удаление динамических модераторов;
  - добавление и удаление сабботов кнопками.

## Восстановление пропущенных медиа в альбомах

Importer ищет отсутствующие `legacy_source + legacy_row_id`, включая пропуски ниже ранее импортированного максимального ID. В общей PostgreSQL-базе это ограниченный запрос `NOT EXISTS`; для отдельного SQLite-источника используются последовательные небольшие пачки. Старый пропуск больше не блокируется более поздним импортом или ручным одобрением.

Одобрение карточки альбома переносит всю группу из `sender_info` до создания контента. Восстановленные вложения наследуют статус, анонимность и решение уже обработанной группы. Старые одобрения и опубликованные записи не создают новые посты автоматически. Перед отправкой publisher повторно проверяет исходную группу и восстанавливает недостающие источники.

Если Telegram скопировал только часть альбома, публикация получает `failed`, контент — `hold`; ошибка содержит ID уже отправленных сообщений. Автоматический повтор подавлен, чтобы не продублировать отправленные фотографии. Такая публикация требует ручной проверки канала.

После обновления кода пересоберите общий образ и обновите процессы, использующие импорт и модерацию:

```bash
docker compose build collector-bot
docker compose up -d collector-bot editorial-importer editorial-publisher editorial-api editorial-mcp
```

Миграции для этого исправления не нужны. После запуска `editorial-importer` восстанавливает пропуски автоматически, до `EDITORIAL_IMPORT_BATCH_SIZE` записей за проход (по умолчанию 200). Для немедленного дополнительного прохода:

```bash
docker compose exec editorial-importer python -m src.editorial.cli import-legacy
```

Восстановление записей базы не изменяет уже опубликованные сообщения Telegram и не публикует их повторно.

## Дубли в очереди автослотов

При расчёте дневной сетки учитываются уникальные одобренные submission/editorial, прошедшие ту же проверку дублей опубликованного контента, что и scheduler. Повторные тексты внутри очереди и несколько записей одного Telegram-альбома дают один слот. Разные медиа с одинаковыми подписями считаются отдельными постами. Статусы дублей не изменяются автоматически.

Scheduler проверяет очередь пачками по 50 записей до первого пригодного кандидата: старые дубли больше не скрывают живые посты на следующих страницах. Настройки min_slots_per_day, allow_pastes и max_paste_per_day сохраняют свою роль. Если уникальных живых постов хватает для рассчитанной сетки, дубли не добавляют места, которые затем занимали пасты.

Исправление применяется при следующем ежедневном планировании. Уже рассчитанная сетка текущего дня автоматически не пересоздаётся. Миграций нет; после пересборки общего образа перезапустите editorial-auto-slots, editorial-scheduler и editorial-publisher (либо весь compose-стек).
