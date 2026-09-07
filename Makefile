.PHONY: install test lint up down migrate secrets preflight bootstrap-telegram verify-linkedin
install:
	python -m pip install -r requirements-dev.lock
	python -m pip install --no-deps -e .
test:
	pytest -q --cov=linkedin_automation
lint:
	ruff check .
up:
	docker compose up -d --build
down:
	docker compose down
migrate:
	alembic upgrade head
secrets:
	python3 scripts/generate_secrets.py
preflight:
	docker compose build app
	docker compose run --rm --no-deps app python scripts/preflight.py
bootstrap-telegram:
	docker compose build app
	docker compose run --rm --no-deps app python scripts/bootstrap_telegram.py
verify-linkedin:
	docker compose build app
	docker compose run --rm --no-deps app python scripts/verify_linkedin.py
