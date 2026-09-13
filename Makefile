.DEFAULT_GOAL := help
ENV ?= dev
PY_VERSION := 3.13
LAMBDA_PLATFORM := aarch64-manylinux2014
BUILD := build

.PHONY: help sync test lint typecheck build synth deploy destroy security aws-tests clean grant-owner

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install dependencies (app + dev + infra)
	uv sync

test: ## Unit tests (no AWS, no network)
	uv run pytest

lint: ## Ruff
	uv run ruff check app infra tests scripts
	uv run ruff format --check app infra tests scripts

typecheck: ## mypy on app
	uv run mypy app

build: ## Package both Lambda functions for arm64 / py$(PY_VERSION)
	rm -rf $(BUILD)
	mkdir -p $(BUILD)/authorizer $(BUILD)/mcp
	uv export --no-dev --no-group infra --no-hashes --format requirements-txt > $(BUILD)/requirements.txt
	uv pip install --quiet --no-deps --no-compile \
	  --python-platform $(LAMBDA_PLATFORM) --python-version $(PY_VERSION) \
	  --target $(BUILD)/mcp -r $(BUILD)/requirements.txt
	uv pip install --quiet --no-deps --no-compile \
	  --python-platform $(LAMBDA_PLATFORM) --python-version $(PY_VERSION) \
	  --target $(BUILD)/authorizer -r $(BUILD)/requirements.txt
	cp -R app $(BUILD)/mcp/app
	cp -R app $(BUILD)/authorizer/app
	find $(BUILD) -name '__pycache__' -type d -prune -exec rm -rf {} +

synth: build ## cdk synth for ENV (default dev)
	uv run cdk synth -c env=$(ENV)

deploy: build ## cdk deploy all stacks for ENV
	uv run cdk deploy -c env=$(ENV) --all --require-approval never

destroy: ## cdk destroy for ENV (dev only — prod buckets are retained)
	uv run cdk destroy -c env=$(ENV) --all

security: ## §12.8 checks against a deployed instance (WIKI_BASE_URL required)
	uv run pytest -m security -o addopts=""

aws-tests: ## Negative STS/S3 tests that need real credentials (build step 3)
	uv run pytest -m aws -o addopts=""

grant-owner: ## Write the bootstrap `own /` grant: make grant-owner SUBJECT=user_xxx TABLE=...
	uv run python scripts/grant_owner.py --subject $(SUBJECT) --table $(TABLE)

clean:
	rm -rf $(BUILD) cdk.out .pytest_cache .ruff_cache .mypy_cache
