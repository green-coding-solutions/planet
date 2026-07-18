# PLANET --- containerised PostgreSQL 18 with the extension installed.
#
#   make up             # build + start the server
#   make psql           # a shell on it
#   make coefficients   # (re)apply ../config/coefficients.json
#   make down           # stop   (make clean also drops the data volume)
#
# Calibration is not run from here: it needs an energy counter and normally runs
# on the host. See ../calibration, then `make coefficients`.

COMPOSE ?= docker compose

# Track whatever compose is using, from the same .env it reads. Note the plain
# `=`: with `?=`, an unrelated shell variable of the same name (USER is always
# set, for one) would silently redirect every target at the wrong database.
-include .env
PLANET_DB   = $(if $(POSTGRES_DB),$(POSTGRES_DB),planet)
PLANET_USER = $(if $(POSTGRES_USER),$(POSTGRES_USER),postgres)

.PHONY: build up down clean logs psql coefficients

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --build db
	$(COMPOSE) exec db bash -c 'until pg_isready -U $(PLANET_USER) -d $(PLANET_DB) -q; do sleep 1; done'
	@echo "planet: server ready. Active coefficients (LOAD first --- nothing preloads planet):"
	@$(COMPOSE) exec -u postgres db psql -d $(PLANET_DB) -Atc "LOAD 'planet';" \
	  -c "SELECT name || ' = ' || setting FROM pg_settings WHERE name LIKE 'planet.%' AND name NOT IN ('planet.enabled','planet.report');"

down:
	$(COMPOSE) down

# Also drops the planet-pgdata volume: the next `up` re-runs initdb.d, which is
# how you pick up a changed planet.conf include. For coefficients alone, use
# `make coefficients`, which needs no restart and keeps your data.
clean:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f db

psql:
	$(COMPOSE) exec -u postgres db psql -d $(PLANET_DB)

# Reads ../config/coefficients.json (bind-mounted read-only at /etc/planet).
# ALTER SYSTEM + reload, no restart.
coefficients:
	$(COMPOSE) exec db planet-apply-coefficients
