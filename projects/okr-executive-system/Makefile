.PHONY: help setup-dev develop test test-unit test-integration test-coverage test-watch format lint type-check quality clean install

help:
	@echo "OKR Executive System - Developer Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make setup-dev        Install dev environment"
	@echo "  make develop          Install in editable mode"
	@echo ""
	@echo "Testing:"
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests only"
	@echo "  make test-coverage    Run with coverage report"
	@echo "  make test-watch       Watch mode (auto-rerun)"
	@echo ""
	@echo "Quality:"
	@echo "  make format           Format code (black)"
	@echo "  make lint             Lint code (ruff)"
	@echo "  make type-check       Type checking (mypy)"
	@echo "  make quality          All quality checks"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean            Remove build artifacts"

setup-dev:
	@echo "Setting up development environment..."
	python -m pip install --upgrade pip setuptools wheel
	pip install -e ".[dev]"
	pre-commit install
	@echo "✅ Development environment ready!"
	@echo ""
	@echo "Next steps:"
	@echo "  make test          # Run tests"
	@echo "  make develop       # Install editable"

develop:
	pip install -e .

install:
	pip install .

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-coverage:
	pytest tests/ --cov=src/okr_executive --cov-report=html --cov-report=term-missing
	@echo "Coverage report: htmlcov/index.html"

test-watch:
	pytest-watch tests/ -v

format:
	black src/ tests/
	@echo "✅ Code formatted"

lint:
	ruff check src/ tests/
	@echo "✅ Linting passed"

type-check:
	mypy src/ --strict
	@echo "✅ Type checking passed"

quality: lint type-check
	@echo "✅ All quality checks passed"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf build/ dist/ *.egg-info/ .coverage htmlcov/
	@echo "✅ Cleaned up"

# Phase-specific targets
test-phase-1:
	pytest tests/unit/test_models.py tests/unit/test_engines.py -v -k "phase1"

test-phase-2:
	pytest tests/unit/test_models.py -v

# Development workflow
dev-setup: setup-dev
	@echo ""
	@echo "Development setup complete!"
	@echo ""
	@echo "1. Read the documentation:"
	@echo "   cat docs/ARCHITECTURE.md"
	@echo ""
	@echo "2. Run the tests:"
	@echo "   make test"
	@echo ""
	@echo "3. Start developing:"
	@echo "   cat docs/DEVELOPMENT.md"

.PHONY: test test-unit test-integration test-coverage test-watch format lint type-check quality clean setup-dev develop install help dev-setup test-phase-1 test-phase-2
