.PHONY: init plan run start stop restart update logs undo test

COMPOSE ?= docker compose
RUN_ONCE := $(COMPOSE) run --rm --build autopilot python -m autopilot

init: .env rules.toml
	mkdir -p data

rules.toml:
	cp rules.example.toml rules.toml
	@echo "Created rules.toml from the example — put your account, payee and category names there."

.env:
	cp .env.example .env
	chmod 600 .env
	@echo "Created .env — fill in ZENMONEY_TOKEN."

plan:            ## what would be written now; writes nothing
	$(RUN_ONCE) plan

run:             ## apply once
	$(RUN_ONCE) run

start: init      ## apply every 30 minutes in the background
	$(COMPOSE) up -d --build

stop:
	$(COMPOSE) down

restart:
	$(COMPOSE) up -d --build --force-recreate

update:          ## pull the code and restart with it
	git pull --ff-only
	$(COMPOSE) up -d --build

logs:
	$(COMPOSE) logs -f -t

undo:            ## make undo JOURNAL=data/journal/<run>.json
	@test -n "$(JOURNAL)" || (echo "Usage: make undo JOURNAL=data/journal/<run>.json" && exit 2)
	$(RUN_ONCE) undo /app/$(JOURNAL)

test:
	uv run pytest -q
