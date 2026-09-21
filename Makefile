GIT_SHA := $(shell git rev-parse --short HEAD)
TEST_DB = MEMORY_TEST_NEO4J_PORT=$(TEST_PORT) docker compose -f compose.test.yaml
TEST_PORT ?= 37687
TEST_URI := bolt://127.0.0.1:$(TEST_PORT)

.PHONY: test test-db lint typecheck image test-db-up test-db-down

# Database tests skip themselves unless MEMORY_TEST_NEO4J_URI is set.
test:
	uv run pytest -q --cov --cov-report=term

test-db: test-db-up
	MEMORY_TEST_NEO4J_URI=$(TEST_URI) uv run pytest -q --cov --cov-report=term

lint:
	uv run ruff check graph_memory tests evals
	uv run ruff format --check graph_memory tests evals

typecheck:
	uv run pyright

image:
	docker build --target production --build-arg GIT_SHA=$(GIT_SHA) -t graph-memory:$(GIT_SHA) .

test-db-up:
	$(TEST_DB) up -d --wait

test-db-down:
	$(TEST_DB) down -v
