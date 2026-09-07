# Changelog

All notable changes are documented here. The project follows semantic versioning.

## 0.1.1 — 2026-09-07

- Split the immutable-audit function and trigger DDL into individual migration statements so the
  initial migration runs correctly through SQLAlchemy's `asyncpg` driver on PostgreSQL 16.
- Run async tests and fixtures on one session event loop so the shared integration-test connection
  pool cannot reuse `asyncpg` connections across incompatible loops.

## 0.1.0 — 2026-09-06

- Added OpenAI-backed source research, strict structured generation, independent quality review,
  weighted scoring, embedding duplicate detection, and optional content-addressed PNG generation.
- Added private Telegram draft review, text and image revision, rejection, explicit approval,
  scheduling, natural-language topic/calendar/style administration, and publication reconciliation.
- Added the official versioned LinkedIn Posts, Images, and optional member analytics integrations.
- Enforced immutable revisions, approvals, and audit events plus exact text/image digest validation
  before every publication.
- Added durable Telegram inbox, pending actions, generation runs, notification outbox, bounded
  retries, stale leases, dead-letter states, and ambiguous-outcome protection.
- Added PostgreSQL/Supabase-compatible migration, hardened Docker Compose deployment, locked runtime
  and development dependencies, tests, CI, CodeQL, Trivy, Dependabot, and operations documentation.
