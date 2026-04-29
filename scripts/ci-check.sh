#!/usr/bin/env bash
set -euo pipefail

pytest tests/ -v --tb=short
pytest tests/ --cov=ax_cli --cov-report=term-missing --cov-fail-under=9
ruff check ax_cli/
ruff format --check ax_cli/
python -m build
twine check dist/*
