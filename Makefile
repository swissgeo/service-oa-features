SHELL = /bin/bash

.DEFAULT_GOAL := help

SERVICE_NAME := service-oa-features

# Docker metadata
GIT_HASH := $(shell git rev-parse HEAD)
GIT_HASH_SHORT := $(shell git rev-parse --short HEAD)
GIT_BRANCH := $(shell git symbolic-ref HEAD --short 2>/dev/null)
GIT_DIRTY := $(shell git status --porcelain)
GIT_TAG := $(shell git describe --tags || echo "no version info")
AUTHOR := $(USER)

# Docker variables
DOCKER_REGISTRY = 074597099015.dkr.ecr.eu-central-1.amazonaws.com
DOCKER_IMG_LOCAL_TAG := $(DOCKER_REGISTRY)/swissgeo/$(SERVICE_NAME):local-$(USER)-$(GIT_HASH_SHORT)

# AWS variables
AWS_DEFAULT_REGION = eu-central-1

# Commands
UV_RUN := uv run
PYTHON := $(UV_RUN) python3
TEST := $(UV_RUN) pytest
RUFF := $(UV_RUN) ruff
TY := $(UV_RUN) ty
PRE_COMMIT := $(UV_RUN) pre-commit
UVICORN := $(UV_RUN) uvicorn

# Application specific
APP_PACKAGE := swissgeo_oaf
HTTP_PORT ?= 8080
SERVE_ARGS := $(APP_PACKAGE).app:APP --host 0.0.0.0 --port $(HTTP_PORT) \
	--log-config config-files/logging-conf.yaml

# The app always runs on the host, so one env file covers both. uv loads it into the environment of every `uv run` below; `dockerrun` passes
# the relevant values through to the container, which runs with --net=host.
# `.env` is created from `.env.default`; `.env.local` overrides it when present.
ENV_FILE ?= $(if $(wildcard .env.local),.env.local,.env)
# $(wildcard ...) so the variable is empty when the file does not exist: uv errors
# out on a missing UV_ENV_FILE, which would break CI-only targets (format, lint,
# test) that need no runtime config.
export UV_ENV_FILE := $(wildcard $(ENV_FILE))

# Read back the value the recipes need at the shell level (i.e. outside `uv run`).
OTEL_ENDPOINT = $(shell sed -n 's/^OTEL_EXPORTER_OTLP_ENDPOINT=//p' $(ENV_FILE) 2>/dev/null)


.env:
	cp .env.default .env


.PHONY: git-info
git-info: ## Print the current version information
	@echo "GIT_HASH=$(GIT_HASH)"
	@echo "GIT_HASH_SHORT=$(GIT_HASH_SHORT)"
	@echo "GIT_BRANCH=$(GIT_BRANCH)"
	@echo "GIT_DIRTY=$(GIT_DIRTY)"
	@echo "GIT_TAG=$(GIT_TAG)"
	@echo "AUTHOR=$(AUTHOR)"
	@echo "DOCKER_IMG_LOCAL_TAG=$(DOCKER_IMG_LOCAL_TAG)"


.PHONY: setup
setup: .env ## Create virtualenv with all packages for development
	uv sync
	$(PRE_COMMIT) install
	# Start a new shell with the virtualenv activated and the .env variables exported,
	# so that the app and the scripts find the local PostGIS.
	uv run $$SHELL


.PHONY: ci
ci: .env ## Create virtual env with all packages for development using the uv.lock (CI)
	uv sync --frozen


.PHONY: format
format: ## Call ruff format to make sure your code is easier to read and respects some conventions.
	$(RUFF) format
	$(RUFF) check --select I --fix


.PHONY: ci-check-format
ci-check-format: format ## Check the format (CI)
	@if [[ -n `git status --porcelain --untracked-files=no` ]]; then \
	 	>&2 echo "ERROR: the following files are not formatted correctly"; \
	 	>&2 echo "'git status --porcelain' reported changes in those files after a 'make format' :"; \
		>&2 git status --porcelain --untracked-files=no; \
		exit 1; \
	fi

.PHONY: services-up
services-up: ## Start PostGIS, collector, Jaeger, Prometheus and (re)apply the database bootstrap
	docker compose up --detach --wait postgres otel-collector jaeger prometheus
	$(MAKE) reset-db


.PHONY: services-down
services-down: ## Stop the backing services (the PostGIS data volume is kept)
	docker compose down


.PHONY: dbshell
dbshell: ## Open a psql shell on the local PostGIS container
	docker compose exec postgres psql -U swissgeo -d swissgeo


.PHONY: reset-db
reset-db: ## Re-apply the PostGIS bootstrap (role, database, PostGIS) to the running database
	docker compose run --rm --build init-database


.PHONY: reseed
reseed: .env ## Recreate the sample table (the app also does this on every start)
	$(PYTHON) -m swissgeo_oaf.sample_data


.PHONY: check-db
check-db: .env ## Check that PostGIS is reachable
	@$(PYTHON) scripts/check_database.py


.PHONY: openapi
openapi: .env ## Discover the PostGIS tables and generate the pygeoapi config and OpenAPI document
	# The app does this itself on startup (swissgeo_oaf.discovery); this target is for
	# inspecting the result without serving. Needs the database, like `make serve`.
	$(PYTHON) -m swissgeo_oaf.discovery


.PHONY: serve
serve: check-db ## Serve the application locally
	$(UVICORN) $(SERVE_ARGS) --reload


.PHONY: serve-debug
serve-debug: check-db ## Serve the application locally for debugging
	$(PYTHON) -m debugpy --listen localhost:5678 --wait-for-client \
		-m uvicorn $(SERVE_ARGS)


.PHONY: dockerbuild
dockerbuild:  ## Build the docker image locally
	docker build \
		--build-arg GIT_HASH="$(GIT_HASH)" \
		--build-arg GIT_BRANCH="$(GIT_BRANCH)" \
		--build-arg GIT_DIRTY="$(GIT_DIRTY)" \
		--build-arg VERSION="$(GIT_TAG)" \
		--build-arg AUTHOR="$(AUTHOR)" -t $(DOCKER_IMG_LOCAL_TAG) .


.PHONY: dockerlogin
dockerlogin: ## Login to the AWS Docker Registry (ECR)
	aws --profile swisstopo-swissgeo-builder ecr get-login-password --region $(AWS_DEFAULT_REGION) | docker login --username AWS --password-stdin $(DOCKER_REGISTRY)


.PHONY: dockerpush
dockerpush: dockerbuild ## Push to the docker registry
	set -o pipefail; \
	docker push $(DOCKER_IMG_LOCAL_TAG) 2>&1 | grep --color=always -E "tag invalid: .*|$$"


.PHONY: dockerrun
dockerrun: check-db dockerbuild ## Run the locally built docker image
	# --net=host so that localhost:5432 (PostGIS) and localhost:4317 (collector)
	# resolve exactly as they do for `make serve`. PYGEOAPI_* are already baked into
	# the image, so only the connection settings are passed through.
	docker run \
		-it --net=host \
		--env-file <(grep -E '^DB_' $(ENV_FILE)) \
		--env OTEL_EXPORTER_OTLP_ENDPOINT="$(OTEL_ENDPOINT)" \
		--env OTEL_EXPORTER_OTLP_INSECURE=true \
		$(DOCKER_IMG_LOCAL_TAG)


.PHONY: lint
lint: ## Run the ruff linter on the code base and type-checker ty
	$(RUFF) check --fix
	$(TY) check


.PHONY: test-ci
test-ci: ## Run tests in the CI (with coverage report in xml format for codecov)
	$(TEST) --cov --cov-branch --cov-report=xml:coverage.xml


.PHONY: test
test: ## Run tests with coverage report in html format
	$(TEST) --cov --cov-branch --cov-report=html


.PHONY: help
help: ## Display this help
# automatically generate the help page based on the documentation after each make target
# from https://gist.github.com/prwhite/8168133
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m\033[0m\n"} /^[$$()% a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)
