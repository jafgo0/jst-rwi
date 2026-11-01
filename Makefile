# Developer entry points. CI runs the same commands (.github/workflows/ci.yml).
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: setup lint typecheck test test-network test-science api dashboard check clean

setup:
	uv venv --python 3.12 $(VENV)
	VIRTUAL_ENV=$(VENV) uv pip install -e ".[dev]"

lint:
	$(VENV)/bin/ruff check src tests

format:
	$(VENV)/bin/ruff check --fix src tests

typecheck:
	$(VENV)/bin/mypy src/rwi

test:
	$(VENV)/bin/pytest tests -q -m "not network"

test-network:
	RWI_RUN_NETWORK_TESTS=1 $(VENV)/bin/pytest tests -q -m network

test-science:
	$(VENV)/bin/pytest tests -q -m science --durations=0

check: lint typecheck test

api:
	$(VENV)/bin/uvicorn rwi.api.app:app --reload --port 8000

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
