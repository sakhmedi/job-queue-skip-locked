# job-queue-skip-locked

Очередь заданий генерации аудио поверх PostgreSQL для GPU-воркеров.
FastAPI + `SELECT … FOR UPDATE SKIP LOCKED`, SQLAlchemy 2.0 (async), Alembic, mock-воркеры на httpx.

```
клиент ──POST /jobs──►  api  ──►  PostgreSQL (таблица jobs)
                         ▲  │
                         │  └── reaper (фоновая задача): возвращает зависшие задания
worker-1, worker-2 ──POST /jobs/claim, /complete, /fail (Bearer-токен)
```

## Быстрый старт

Нужен только Docker (Python ставить не нужно — всё работает в контейнерах на Python 3.12).

```bash
cp .env.example .env              # необязательно: без .env используются значения по умолчанию
docker compose up --build -d      # db + api + worker-1 + worker-2
docker compose exec api python scripts/enqueue.py 20
docker compose logs -f worker-1 worker-2      # Ctrl+C — выйти из логов
docker compose exec db psql -U postgres -d jobs -c "select status, count(*) from jobs group by status;"
docker compose down               # docker compose down -v — заодно удалить данные БД
```

Те же действия через Makefile: `make up`, `make enqueue N=20`, `make logs`, `make stats`, `make down`.

Swagger UI: http://localhost:8000/docs

## API

| Метод и путь | Кто вызывает | Токен | Ответы |
|---|---|---|---|
| `POST /jobs` | клиент | нет | `201` задание; `422` невалидное тело |
| `GET /jobs/{id}` | клиент | нет | `200`; `404` нет такого; `422` невалидный UUID |
| `POST /jobs/claim` | воркер | да | `200` задание; `204` очередь пуста; `401` |
| `POST /jobs/{id}/complete` | воркер | да | `200`; `404`; `409` недопустимый переход; `401` |
| `POST /jobs/{id}/fail` | воркер | да | `200`; `404`; `409`; `401` |
| `GET /health` | docker healthcheck | нет | `200` / `503` если БД недоступна |

### Примеры `curl`

```bash
TOKEN=dev-worker-token-change-me     # значение WORKER_TOKEN

# Создать задание
curl -s -X POST localhost:8000/jobs -H 'Content-Type: application/json' \
     -d '{"prompt": "lo-fi beat, 90 bpm", "params": {"duration_sec": 30}}'
# → 201 {"id":"3f2a…","status":"queued","attempts":0,"worker_id":null,...}

# Статус задания
curl -s localhost:8000/jobs/3f2a...

# Забрать задание (воркер)
curl -s -i -X POST localhost:8000/jobs/claim -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"worker_id": "curl-worker"}'
# → 200 {"id":"3f2a…","status":"running","worker_id":"curl-worker","attempts":1,...}
# → 204 No Content, если очередь пуста

# Завершить успешно
curl -s -X POST localhost:8000/jobs/3f2a.../complete -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"worker_id": "curl-worker", "result": "s3://bucket/a.mp3"}'

# Завершить с ошибкой
curl -s -X POST localhost:8000/jobs/3f2a.../fail -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"worker_id": "curl-worker", "error": "CUDA OOM"}'

# Повторный complete / чужой worker_id → 409
# {"detail":"job 3f2a… is 'succeeded', only 'running' jobs can be finished"}
# {"detail":"job 3f2a… is held by worker 'worker-1', not 'curl-worker'"}

# Без токена → 401 {"detail":"invalid or missing worker token"}

curl -s localhost:8000/health    # → {"status":"ok","db":"ok"}
```

## Схема БД

Миграция: `migrations/versions/0001_create_jobs.py`. Применяется автоматически при старте api
(`alembic upgrade head && uvicorn …`). Чтобы несколько одновременно стартующих экземпляров api
не мигрировали параллельно, `migrations/env.py` берёт `pg_advisory_xact_lock`.

| Колонка | Тип | Примечание |
|---|---|---|
| `id` | `UUID` PK | генерируется в Python (`uuid4`), есть и `DEFAULT gen_random_uuid()` |
| `prompt` | `TEXT NOT NULL` | 1..4000 символов (проверяет API) |
| `params` | `JSONB NOT NULL DEFAULT '{}'` | |
| `status` | `TEXT NOT NULL` + `CHECK` | `queued` / `running` / `succeeded` / `failed` |
| `result`, `error` | `TEXT NULL` | |
| `worker_id` | `TEXT NULL` | кто сейчас держит задание |
| `attempts` | `INT NOT NULL DEFAULT 0` | +1 при каждом claim, `CHECK (attempts >= 0)` |
| `created_at`, `updated_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | |
| `started_at`, `finished_at` | `TIMESTAMPTZ NULL` | |

Дополнительные ограничения и индексы:

- `CHECK (status <> 'running' OR (worker_id IS NOT NULL AND started_at IS NOT NULL))`:
  в БД не может оказаться running-задание без владельца, даже если в коде ошибка.
- `ix_jobs_queued_created_at ON jobs (created_at) WHERE status = 'queued'` — для claim.
  Индекс **частичный**: в нём только queued-строки, поэтому он маленький, даже если
  в таблице миллионы завершённых заданий, и claim читает из него первую же запись.
- `ix_jobs_running_started_at ON jobs (started_at) WHERE status = 'running'` — для reaper.

Почему `TEXT + CHECK`, а не `ENUM`: новый статус в ENUM добавляется через `ALTER TYPE … ADD VALUE`,
а удалить значение из ENUM нельзя совсем. CHECK меняется обычными `DROP/ADD CONSTRAINT`.

## Статусы и переходы

```mermaid
stateDiagram-v2
    [*] --> queued: POST /jobs
    queued --> running: POST /jobs/claim
    running --> succeeded: POST /complete (тот же worker_id)
    running --> failed: POST /fail (тот же worker_id)
    running --> queued: reaper (завис, attempts < MAX_ATTEMPTS)
    running --> failed: reaper (завис, attempts >= MAX_ATTEMPTS)
    succeeded --> [*]
    failed --> [*]
```

| Из \ Действие | claim | complete / fail своим `worker_id` | complete / fail чужим `worker_id` | reaper (завис) |
|---|---|---|---|---|
| `queued` | → `running`, `attempts+1` | 409 | 409 | — |
| `running` | не выдаётся | → `succeeded` / `failed` | 409 | → `queued` или `failed` |
| `succeeded` | не выдаётся | 409 | 409 | — |
| `failed` | не выдаётся | 409 | 409 | — |
| нет такого id | — | 404 | 404 | — |

Каждый переход — **один** `UPDATE`, условие перехода стоит в его `WHERE`:

```sql
UPDATE jobs SET status = 'succeeded', result = :result, finished_at = now(), updated_at = now()
WHERE id = :id AND status = 'running' AND worker_id = :worker_id
RETURNING *;
```

Если `UPDATE` вернул 0 строк, переход запрещён. Только после этого делаем `SELECT`, и то лишь
для текста ошибки (404 или 409 с причиной). Решение к этому моменту уже принято атомарно.

## Почему `FOR UPDATE SKIP LOCKED` исключает гонки

### Наивный вариант сломан

```python
job = SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1;  # шаг 1
UPDATE jobs SET status = 'running', worker_id = :me WHERE id = :job.id;          # шаг 2
```

Два воркера A и B:

```
A: SELECT → job 42
B: SELECT → job 42         ← A ещё не записал, B видит то же самое
A: UPDATE id=42 → OK
B: UPDATE id=42 → OK       ← B перезаписал владельца, задание выполнят оба
```

Между чтением и записью есть «окно», и транзакция не спасает: в `READ COMMITTED` (уровень
изоляции по умолчанию) обычный `SELECT` ничего не блокирует. Это воспроизводит тест
`test_naive_select_then_update_gives_job_twice`.

Добавить в `UPDATE` условие `AND status = 'queued'` уже лучше: B получит 0 строк. Но тогда все
воркеры дерутся за одну и ту же самую старую строку, проигравшие повторяют попытку, и под
нагрузкой это превращается в лавину повторов.

### Что делает claim

```sql
UPDATE jobs
SET status = 'running', worker_id = :w, started_at = now(), attempts = attempts + 1, updated_at = now()
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'queued'
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

1. `FOR UPDATE` ставит **блокировку строки** на найденное задание. Пока транзакция не
   завершилась, другая транзакция не может ни изменить эту строку, ни заблокировать её снова.
2. Обычный `FOR UPDATE` заставил бы второго воркера **ждать** освобождения строки (тест
   `test_without_skip_locked_claim_would_wait`), а потом всё равно получить «уже занято».
   `SKIP LOCKED` говорит: «заблокированные строки пропускай и бери следующую свободную».
   Поэтому 10 воркеров одновременно получают 10 **разных** заданий, без ожидания и без
   повторов (`test_claim_skips_row_locked_by_another_transaction`).
3. Выбор строки и её изменение происходят в одном стейтменте, а значит в одной транзакции.
   Блокировка держится от `SELECT` до `COMMIT`, так что окна между чтением и записью нет.
4. Тонкость: если строку заблокировали, изменили и закоммитили, пока наш запрос её искал,
   PostgreSQL перепроверяет `WHERE status = 'queued'` на **свежей** версии строки
   (EvalPlanQual в `READ COMMITTED`). Уже взятое задание не пройдёт эту проверку.
5. После `COMMIT` строка уже `running`, и в выборку `status = 'queued'` больше не попадает.

Проверено тестами: 2 и 10 параллельных claim на одно задание (ровно один победитель);
100 заданий и 10 воркеров (каждое выдано ровно один раз, ни одно не потеряно).
Если убрать из запроса `FOR UPDATE SKIP LOCKED`, эти тесты падают.

`ORDER BY created_at` даёт FIFO «в среднем». Под конкуренцией задания могут завершиться
не строго по порядку: второй воркер взял следующее задание, пока первое ещё в работе.
Для очереди задач это нормально.

## Reaper: возврат зависших заданий

Воркер может упасть, потерять сеть или зависнуть, и тогда задание навсегда останется в `running`.
Фоновая asyncio-задача в api (запускается в `lifespan`) раз в `REAPER_INTERVAL_SECONDS` вызывает
`requeue_stale_jobs(session, now)` (`app/reaper.py`):

```sql
WITH stale AS (
    SELECT id FROM jobs
    WHERE status = 'running' AND started_at < :now - STALE_AFTER_SECONDS
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET
    status      = CASE WHEN attempts >= :max THEN 'failed' ELSE 'queued' END,
    error       = CASE WHEN attempts >= :max THEN 'max attempts exceeded' ELSE error END,
    worker_id   = NULL, started_at = NULL, ...
FROM stale WHERE jobs.id = stale.id
RETURNING id, status;
```

- **`attempts < MAX_ATTEMPTS`** → `queued`, задание заберёт другой воркер (`attempts` станет 2, 3, …).
- **`attempts >= MAX_ATTEMPTS`** → `failed` с `error = "max attempts exceeded"`. Это защита от
  «ядовитых» заданий: если задание каждый раз роняет воркер, оно не будет крутиться по кругу
  вечно, занимая GPU. При `MAX_ATTEMPTS=3` задание выдаётся не больше трёх раз.
- **Несколько экземпляров api**: каждый запускает свой reaper. `SKIP LOCKED` не даёт двум
  reaper'ам обработать одну строку (`test_parallel_reapers_do_not_double_process`), а строку,
  которую в этот момент завершает воркер, reaper пропустит до следующего прохода.
- **Время** reaper в проде берёт у БД (`SELECT now()`), как и `started_at`, поэтому расхождение
  часов api и БД ни на что не влияет. В тестах `now` передаётся явно.
- **Поздний complete** от «мёртвого» воркера после возврата задания даёт `409`: в БД уже
  `worker_id = NULL` или id другого воркера, и `WHERE … AND worker_id = :w` не совпадает.

## Гарантии: at-least-once

Задание **может быть выполнено больше одного раза**:

- воркер работал дольше `STALE_AFTER_SECONDS` (или завис и ожил), reaper отдал задание
  другому, и генерация прошла дважды. Сохранится только результат того, кто держит задание
  сейчас; опоздавший получит 409;
- воркер выполнил задание, но не смог доставить результат (api недоступен дольше, чем длятся
  ретраи), и задание вернётся в очередь;
- ответ на `claim` потерялся в сети: задание помечено `running`, но воркер о нём не знает.
  Его вернёт reaper.

Exactly-once здесь нет и в распределённой системе бесплатно не бывает. Поэтому обработка
задания должна быть идемпотентной (например, результат пишется по ключу `job_id`).
**At-most-once** гарантирован для **результата**: в БД попадает ровно один итог, переход из
`succeeded/failed` невозможен.

## Воркер

`worker/main.py`, запуск: `python -m worker.main`.

- Цикл: `claim` → `204`: пауза `POLL_INTERVAL` ± 20 % джиттера → `200`: «генерация»
  `sleep(1–5 с)` → `complete` (или `fail` с вероятностью `FAIL_RATE`).
- Сетевые ошибки и 5xx: экспоненциальный backoff 0.5 → 1 → 2 → … → 30 с с джиттером.
  `401` считается фатальной ошибкой конфигурации, воркер завершается.
- `409` на complete означает «задание у нас забрали»: пишем warning и работаем дальше.
- SIGTERM/SIGINT: новые задания не берутся, текущее дорабатывается и отчитывается, затем выход
  (`stop_grace_period: 15s` в compose).

Пример логов:

```
12:00:01 INFO    worker-1 | claimed  job=8afb720b attempt=1 prompt='Track #1: ambient pad, 84 bpm'
12:00:01 INFO    worker-2 | claimed  job=801d761f attempt=1 prompt='Track #2: jazz piano, 161 bpm'
12:00:03 INFO    worker-2 | succeeded job=801d761f in 2.1s
12:00:04 WARNING worker-1 | api unavailable (ConnectError), retry #1 in 0.4s
```

## Переменные окружения

| Переменная | По умолчанию | Где | Описание |
|---|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/jobs` | api | |
| `WORKER_TOKEN` | — (обязательна) | api, воркер | общий секрет; в compose `dev-worker-token-change-me` |
| `STALE_AFTER_SECONDS` | `600` | api | через сколько `running` считается зависшим |
| `MAX_ATTEMPTS` | `3` | api | сколько раз задание можно выдать |
| `REAPER_INTERVAL_SECONDS` | `30` | api | период reaper |
| `REAPER_ENABLED` | `true` | api | выключается в тестах |
| `LOG_LEVEL` | `INFO` | api, воркер | |
| `TEST_DATABASE_URL` | `…/jobs_test` | тесты | база для тестов (имя обязано оканчиваться на `_test`) |
| `API_URL` | `http://localhost:8000` | воркер, enqueue | |
| `WORKER_ID` | hostname | воркер | |
| `POLL_INTERVAL` | `1.0` | воркер | пауза при пустой очереди, с |
| `FAIL_RATE` | `0.1` | воркер | вероятность имитировать ошибку |

## Тесты

```bash
make test                         # = docker compose run --rm --build api pytest
make test ARGS="-k concurrency -v"
make lint                         # ruff check + ruff format --check
```

Тесты идут на настоящем PostgreSQL 16 (сервис `db`) в отдельной базе `jobs_test`. Она создаётся
автоматически и мигрируется через `alembic upgrade head`, так что заодно проверяется миграция.
Перед каждым тестом делается `TRUNCATE jobs`. Каждая сессия в тестах открывает своё соединение
(`NullPool`), поэтому «параллельные воркеры» — это действительно разные транзакции.

| Файл | Что проверяет |
|---|---|
| `test_concurrency.py` | 2/10 параллельных claim на одно задание; 100 заданий × 10 воркеров; пропуск заблокированной строки; для сравнения — ожидание без SKIP LOCKED и поломка наивного SELECT+UPDATE |
| `test_transitions.py` | queued→running→succeeded/failed, 409 (повторно, чужой воркер, из queued), 404, 422, FIFO |
| `test_reaper.py` | зависшее → queued; свежее не трогается; повторный claim с `attempts=2`; MAX_ATTEMPTS → failed; поздний complete → 409; параллельные reaper'ы |
| `test_auth.py` | без токена / неверный / пустой / не Bearer → 401; клиентские эндпоинты без токена |
| `test_api.py` | валидация, 201/204/404/422, health |

## Что улучшил бы

- **LISTEN/NOTIFY вместо поллинга.** `POST /jobs` делает `NOTIFY jobs_new`, а воркер (или api
  через long-poll / SSE) просыпается сразу. Задержка падает с ~1 с до миллисекунд, пустых
  запросов к БД почти нет. Поллинг оставить как подстраховку: NOTIFY не доставляется, пока
  слушатель отключён.
- **Heartbeat воркера.** Сейчас «завис» определяется по `started_at`, и задание дольше
  10 минут будет отобрано у живого воркера. Лучше, чтобы воркер раз в N секунд делал
  `POST /jobs/{id}/heartbeat` (`UPDATE … SET heartbeat_at = now() WHERE worker_id = :w`),
  а reaper смотрел на `heartbeat_at`. Тогда длинные задания безопасны, а мёртвые воркеры
  обнаруживаются за секунды.
- **Приоритеты.** Колонка `priority`, `ORDER BY priority DESC, created_at` и индекс
  `(priority DESC, created_at) WHERE status = 'queued'`.
- **Ретраи при `fail`**: различать временные ошибки (OOM → повторить с backoff, `run_after`)
  и постоянные (невалидный prompt → сразу failed).
- **Отмена задания клиентом** (`cancelled`) и **TTL/архивация** завершённых заданий,
  чтобы таблица не росла бесконечно.
- **Batch claim** (`LIMIT :n`) для воркеров, которые умеют обрабатывать несколько заданий сразу.
- **Токен на каждого воркера** (или mTLS) вместо общего секрета, чтобы `worker_id` нельзя было
  подделать, и токены можно было отзывать по одному.
- **Метрики** (длина очереди, время ожидания, доля failed) и отдельный prod-образ без dev-зависимостей.

## Структура

```
app/
  main.py        FastAPI, lifespan (reaper), обработчики 404/409, /health
  config.py      настройки из env (pydantic-settings)
  db.py          engine и сессии SQLAlchemy
  models.py      ORM-модель Job, JobStatus
  schemas.py     Pydantic-схемы запросов и ответов
  repository.py  весь SQL: create / claim / complete / fail / get
  reaper.py      requeue_stale_jobs + фоновый цикл
  auth.py        проверка Bearer-токена воркера
  routers/jobs.py
migrations/      Alembic (async env, advisory lock)
worker/main.py   mock GPU-воркер
scripts/enqueue.py
tests/
```
