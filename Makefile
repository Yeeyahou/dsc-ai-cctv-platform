.PHONY: install lint format test migrate openapi
install:
	python -m pip install -r requirements-dev.lock
	python -m pip install --no-deps -e .
lint:
	ruff check src tests migrations scripts
	ruff format --check src tests migrations scripts
format:
	ruff check --fix src tests migrations scripts
	ruff format src tests migrations scripts
test:
	pytest
migrate:
	alembic upgrade head
openapi:
	python scripts/export_openapi.py

build-ApiFunction build-MaintenanceFunction:
	python scripts/sam_build.py "$(ARTIFACTS_DIR)"
