# Makefile — API FastAPI (dev via Docker)

COMPOSE := docker compose -f compose.dev.yml
SERVICE := api

.DEFAULT_GOAL := help

## help : liste les cibles
.PHONY: help
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'

## env : crée .env depuis .env.example s'il n'existe pas
.PHONY: env
env:
	@test -f .env || (cp .env.example .env && echo ".env créé depuis .env.example")

## up : démarre l'API + Postgres en dev (http://localhost:8000/docs)
.PHONY: up
up: env
	$(COMPOSE) up --build

## up-d : idem en arrière-plan
.PHONY: up-d
up-d: env
	$(COMPOSE) up --build -d

## down : arrête les containers
.PHONY: down
down:
	$(COMPOSE) down

## clean : arrête tout et supprime le volume Postgres
.PHONY: clean
clean:
	$(COMPOSE) down -v

## logs : logs en continu
.PHONY: logs
logs:
	$(COMPOSE) logs -f

## sh : shell dans le container api
.PHONY: sh
sh:
	$(COMPOSE) exec $(SERVICE) bash

## lint : ruff check
.PHONY: lint
lint:
	$(COMPOSE) run --rm $(SERVICE) ruff check .

## fmt : ruff format + fix
.PHONY: fmt
fmt:
	$(COMPOSE) run --rm $(SERVICE) sh -c "ruff check --fix . && ruff format ."

## test : pytest
.PHONY: test
test:
	$(COMPOSE) run --rm $(SERVICE) pytest

## migrate : applique les migrations Alembic
.PHONY: migrate
migrate:
	$(COMPOSE) exec $(SERVICE) alembic upgrade head

## revision : génère une migration (make revision m="message")
.PHONY: revision
revision:
	$(COMPOSE) exec $(SERVICE) alembic revision --autogenerate -m "$(m)"
