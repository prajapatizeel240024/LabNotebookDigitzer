# Lab Notebook Digitizer. Run `make help`.
COMPOSE      ?= docker compose
FAKE         := -f docker-compose.yml -f docker-compose.fake.yml
N            ?= 2
PAGES        ?= 300
WORKER_SETS  ?= 1 2 4 8
# The project's virtualenv if there is one (python3.12 -m venv .venv), else python3.
PY           ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
# Host port of the API; docker compose reads it too. Set it in the environment
# (API_PORT=8001 make up if 8000 is taken), not in .env: make does not read .env.
API_PORT     ?= 8000
# Total time `make eval` gives the 20 pages. A CPU model takes about 2-4 minutes a page.
EVAL_TIMEOUT ?= 7200

.PHONY: help env build models up up-fake down logs scale test-deps test-deps-down install test \
        test-docker lint fmt loadtest sandbox-bench eval-pages eval clean

help:  ## show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

env:  ## create .env with a random database password and the docker group id
	@test -f .env && echo ".env already exists, leaving it alone" || { \
	  umask 077; \
	  printf 'POSTGRES_PASSWORD=%s\nDOCKER_GID=%s\n' \
	    "$$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)" \
	    "$$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)" > .env; \
	  echo "wrote .env"; }

build: env  ## build the app and sandbox images
	$(COMPOSE) build

models: env  ## one-time download of the model weights into the models volume (needs internet)
	$(COMPOSE) --profile models run --rm --build models-fetch

up: env  ## start everything with the real model (run `make models` first)
	$(COMPOSE) up -d --build
	@echo "API and review screen: http://127.0.0.1:$(API_PORT)"

up-fake: env  ## start everything with the fake model server (no GPU, no download)
	$(COMPOSE) $(FAKE) up -d --build
	@echo "API and review screen: http://127.0.0.1:$(API_PORT)  (fake model: canned answers)"

down:  ## stop the stack (keeps data); add V=1 to also delete volumes
	$(COMPOSE) down $(if $(V),-v,)

logs:  ## follow logs
	$(COMPOSE) logs -f --tail=100

scale:  ## run N workers, e.g. make scale N=8
	$(COMPOSE) up -d --no-recreate --scale worker=$(N) worker

install:  ## install the package and dev tools
	$(PY) -m pip install --break-system-packages -e '.[dev]'

test-deps:  ## start throwaway Postgres and Redis for the tests
	$(COMPOSE) -f docker-compose.test.yml up -d --wait

test-deps-down:  ## stop them
	$(COMPOSE) -f docker-compose.test.yml down -v

test:  ## fast tests (needs `make test-deps`; Docker-based tests are skipped)
	$(PY) -m pytest -q -m "not docker"

test-docker: env  ## sandbox escape tests (need a Docker daemon and the sandbox image)
	$(COMPOSE) build sandbox-image
	SANDBOX_IMAGE=lnd-sandbox:latest $(PY) -m pytest -q -m docker

lint:  ## ruff
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:  ## format
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

loadtest: env  ## pages/min for 1,2,4,8 workers against the compose stack with the fake model
	$(COMPOSE) $(FAKE) up -d --build
	@for n in $(WORKER_SETS); do \
	  $(COMPOSE) $(FAKE) up -d --no-recreate --scale worker=$$n worker; \
	  $(PY) -m bench.load_test --launch external --api http://127.0.0.1:$(API_PORT) \
	    --workers $$n --pages $(PAGES); \
	done

sandbox-bench:  ## warm versus cold sandbox start (needs Docker and the sandbox image)
	SANDBOX_IMAGE=lnd-sandbox:latest $(PY) -m bench.sandbox_bench --runs 40

eval-pages:  ## render the 20 synthetic notebook pages
	$(PY) -m evaluation.render_pages

eval:  ## score extraction accuracy through the API (stack must be up with a model)
	$(PY) -m evaluation.run_eval --api http://127.0.0.1:$(API_PORT) --timeout $(EVAL_TIMEOUT)

clean:  ## remove caches and build output
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
