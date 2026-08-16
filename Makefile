.PHONY: help install test test-unit test-integration test-hardware lint fmt

help:
	@echo "install           uv sync --extra dev"
	@echo "test              unit + integration (no hardware) — the CI gate"
	@echo "test-unit         protocol/decode tests, pure, no I/O"
	@echo "test-integration  service tests against the mock transport"
	@echo "test-hardware     ⚠️  REAL DEVICE. Human present. Never in CI."
	@echo "lint              ruff + mypy --strict"

install:
	uv sync --extra dev

test:
	uv run pytest tests/unit tests/integration

test-unit:
	uv run pytest tests/unit

test-integration:
	uv run pytest tests/integration

# Explicit invocation only. Requires a powered BedJet in BLE range and a human
# watching it. See docs/SAFETY.md.
test-hardware:
	@echo "⚠️  Hardware tests talk to the real BedJet."
	@echo "   Device powered? In range? Human present? Physical remote in reach?"
	@read -p "   Type 'yes' to continue: " ok && [ "$$ok" = "yes" ]
	uv run pytest tests/hardware -m hardware -v

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests
