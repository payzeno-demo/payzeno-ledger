# payzeno-ledger — the four targets you actually need are: install, test, lint, migrate.
# Everything else exists because someone got tired of typing it.

SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
COMPOSE ?= docker compose -f ../payzeno-infrastructure/docker-compose.yml -f docker-compose.override.yml
PYTEST_ARGS ?=

.PHONY: help
help: ## show this
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
.PHONY: install
install: ## create .venv and install app + dev deps from the lock
	$(UV) sync --locked --all-extras

.PHONY: lock
lock: ## re-resolve uv.lock after editing pyproject.toml (commit the result)
	$(UV) lock

# ---------------------------------------------------------------------------
# quality gates — the same three commands CI runs, in the same order
# ---------------------------------------------------------------------------
.PHONY: lint
lint: ## ruff check + format check
	$(UV) run ruff check app tests migrations
	$(UV) run ruff format --check app tests migrations

.PHONY: fmt
fmt: ## ruff format, in place
	$(UV) run ruff format app tests migrations
	$(UV) run ruff check --fix app tests migrations

.PHONY: types
types: ## mypy --strict over app/ (tests are lenient, see pyproject)
	$(UV) run mypy --strict app

.PHONY: test
test: ## unit + service + api tests (no Postgres needed)
	$(UV) run pytest -m "not integration" $(PYTEST_ARGS)

.PHONY: test-integration
test-integration: ## the testcontainers suite — needs a working Docker socket
	$(UV) run pytest -m integration $(PYTEST_ARGS)

.PHONY: test-all
test-all: test test-integration ## everything

.PHONY: cov
cov: ## coverage with the two gates CI enforces: domain 100%, services 85%
	$(UV) run pytest -m "not integration" --cov=app --cov-report=term-missing
	$(UV) run coverage report --include="app/domain/*" --fail-under=100
	$(UV) run coverage report --include="app/services/*" --fail-under=85

.PHONY: check
check: lint types test ## what you should run before opening a PR

# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## alembic upgrade head
	$(UV) run alembic upgrade head

.PHONY: downgrade
downgrade: ## alembic downgrade -1 (check the migration has a real downgrade first)
	$(UV) run alembic downgrade -1

.PHONY: revision
revision: ## make a revision: make revision m="add capture_attempt"
	@test -n "$(m)" || (echo "usage: make revision m=\"...\"" && exit 1)
	$(UV) run alembic revision --autogenerate -m "$(m)"
	@echo
	@echo ">> rename it to migrations/versions/NNNN_<slug>.py and set revision='NNNN'."
	@echo ">> the chain is linear. read the diff. autogenerate gets indexes wrong."

.PHONY: heads
heads: ## assert the migration chain has exactly one head
	@test "$$($(UV) run alembic heads | wc -l)" -eq 1 \
	  || (echo "more than one head — rebase, do not merge-revision" && exit 1)

# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------
.PHONY: run
run: ## uvicorn on :8000 against whatever DATABASE_URL is in .env
	$(UV) run uvicorn app.main:create_app --factory --reload --port 8000

.PHONY: worker
worker: ## the APScheduler process (11 periodic jobs)
	$(UV) run python -m app.workers

.PHONY: consumer
consumer: ## the two SQS consumers
	$(UV) run python -m app.consumers

.PHONY: up
up: ## compose: postgres + localstack + the ledger
	$(COMPOSE) up --build payzeno-ledger

.PHONY: down
down:
	$(COMPOSE) down -v

.PHONY: logs
logs:
	$(COMPOSE) logs -f payzeno-ledger

# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------
.PHONY: openapi
openapi: ## regenerate contracts/ledger-openapi.json — payzeno-api asserts against it
	$(UV) run python -m app.ops.cli dump-openapi > contracts/ledger-openapi.json
	@echo ">> if this diff is not empty, payzeno-api's contract test will fail until they pull."
