# Contributing

## Development setup

Use Python 3.12 and PostgreSQL 16. Install exactly the locked development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
```

Run the local checks before opening a pull request:

```bash
ruff format --check .
ruff check .
pytest -q
alembic upgrade head --sql
pip-audit -r requirements.lock
pip-audit -r requirements-dev.lock
```

CI also exercises the migration upgrade/seed/downgrade/upgrade cycle and PostgreSQL integration
tests against a real PostgreSQL 16 service, builds the container, runs a health smoke test, scans
the image with Trivy, and runs CodeQL.

## Pull-request expectations

- Keep changes focused and document any operator-visible behavior.
- Add tests for success, rejected input, replay, and failure recovery where applicable.
- Never commit `.env` files, credentials, production content, database dumps, or generated images.
- Add a new forward migration after a schema version has been released; do not rewrite a released
  migration.
- Update both lock files in the same pull request when dependency constraints change. Generate the
  runtime lock from the base project and the development lock with the `dev` extra.
- Preserve the single-replica limitation unless scheduler leadership and all remaining side effects
  have been made safe for horizontal execution.

## Non-negotiable publication rules

No change may allow AI output, a schedule, a retry, or an administrator intent to bypass explicit
approval of the current text and image. Every revision must invalidate the previous nonce. The
publisher must verify the immutable approval digests immediately before the LinkedIn call. A post
request with an ambiguous outcome must stop for human reconciliation rather than being replayed.

Browser automation, LinkedIn cookies/passwords, scraping, private endpoints, and silent analytics
fallbacks are out of scope.
