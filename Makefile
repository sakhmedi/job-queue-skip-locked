COMPOSE ?= docker compose

.PHONY: help up down logs enqueue stats test lint format

help:
	@echo "make up       - собрать и запустить db, api, worker-1, worker-2"
	@echo "make down     - остановить всё"
	@echo "make logs     - логи воркеров"
	@echo "make enqueue  - поставить N=20 заданий (make enqueue N=50)"
	@echo "make stats    - статистика статусов в БД"
	@echo "make test     - тесты на настоящем PostgreSQL (в Docker)"
	@echo "make lint     - ruff check + ruff format --check"
	@echo "make format   - автоформатирование ruff (правит файлы)"

up:
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f worker-1 worker-2

N ?= 20
enqueue:
	$(COMPOSE) exec api python scripts/enqueue.py $(N)

stats:
	$(COMPOSE) exec db psql -U postgres -d jobs -c "select status, count(*) from jobs group by status order by status;"

# `run` поднимает db (depends_on: service_healthy) и запускает pytest в контейнере api.
# Тесты используют отдельную базу jobs_test, демо-данные не трогаются.
test:
	$(COMPOSE) run --rm --build api pytest $(ARGS)

lint:
	$(COMPOSE) run --rm --build --no-deps api sh -c "ruff check . && ruff format --check ."

# Монтируем исходники в контейнер, чтобы ruff мог их исправить.
format:
	$(COMPOSE) run --rm --no-deps --user root -v "$(CURDIR):/app" api sh -c "ruff format . && ruff check --fix ."
