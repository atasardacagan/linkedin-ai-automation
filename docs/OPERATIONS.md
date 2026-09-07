# Operations runbook

## Supported production topology

Run one application replica, one PostgreSQL database, one persistent image volume, and an HTTPS reverse proxy. The bundled APScheduler is in the application process; multiple replicas would duplicate weekly reports and token warnings even though core queues use row locks. Split scheduler and workers into separately elected services before scaling horizontally.

The Compose deployment applies migrations in a one-shot `migrate` service. The long-running `app` container runs as an unprivileged user, has all Linux capabilities dropped, is read-only except for `/app/storage`, and binds its cleartext port only to host loopback.

## First deployment

1. Create `.env` from `.env.example`.
2. Generate independent database and Telegram webhook secrets with `make secrets`.
3. Complete Telegram IDs and LinkedIn authorization using the README and `LINKEDIN_OAUTH.md`.
4. Set `PUBLIC_BASE_URL` to the deployed HTTPS origin, with no path, query, or fragment.
5. Keep `DRY_RUN=true`.
6. Validate without displaying credentials:

   ```bash
   make preflight
   ```

7. Start and inspect:

   ```bash
   docker compose up -d --build
   docker compose ps
   curl --fail http://127.0.0.1:8000/health
   docker compose logs --tail=100 app
   ```

8. Confirm the `migrate` service exited successfully and the `app` service is healthy.
9. Send `/start` and `/status` to the bot.
10. Complete one end-to-end dry-run approval. Enable live LinkedIn calls only after it succeeds.

Do not expose port 8000 directly to the internet. Route only the public HTTPS origin through a managed load balancer or reverse proxy to `127.0.0.1:8000`. Preserve the request path `/webhooks/telegram`. The application authenticates the Telegram secret header but expects the platform to provide TLS, certificate renewal, firewalling, and denial-of-service controls.

## Secret handling

- Store production `.env` outside Git and restrict it to the deployment account.
- Prefer the hosting platform's encrypted secret store over a file when available.
- Never send the OpenAI key, Telegram bot token, LinkedIn token, database password, or OAuth client secret in Telegram or application logs.
- Use distinct secrets for PostgreSQL and the Telegram webhook.
- Rotate a suspected credential first at its provider, then update the deployment and restart.
- Logs redact bearer tokens and secret-like structured keys, but this is a final safeguard—not permission to log credentials.
- Backups contain drafts, revisions, operator instructions, source URLs, and analytics; classify and encrypt them accordingly.

## Release procedure

1. Review the release diff and dependency updates.
2. Run the same local checks as CI:

   ```bash
   ruff format --check .
   ruff check .
   pytest -q
   alembic upgrade head --sql
   pip-audit -r requirements.lock
   pip-audit -r requirements-dev.lock
   ```

3. Back up PostgreSQL and the image volume.
4. Build the new image without stopping the old application.
5. Pause automatic draft generation with `/pause` if the release changes workflows.
6. Run the migration job once.
7. Replace the application container and wait for `/health`.
8. Verify `/status` in Telegram and run a dry-run when the release affects publishing.
9. Resume generation only after checks pass.

Future migrations should follow expand/migrate/contract design. Never run `alembic downgrade base` against production: the initial downgrade intentionally drops all application tables.

## PostgreSQL and Supabase

For the bundled database, `DATABASE_URL` points to the Compose service `db`. The database is not published to the host.

For Supabase:

- run Alembic through the direct Postgres connection;
- use a persistent/session connection compatible with `asyncpg` for the application;
- apply the provider's current TLS requirements;
- keep the schema private from browser clients and do not expose database/service-role credentials;
- do not enable client-facing REST access to workflow tables;
- test `upgrade head` and restore in a staging project first.

This release stores generated PNG files in a Docker volume, not Supabase Storage. Back up that volume with the database. A database-only restore can leave approved image paths without their approved bytes; the integrity guard will correctly stop those publications.

## Backup

Create a timestamped, encrypted destination outside the repository. A logical database backup can be produced from the bundled PostgreSQL service:

```bash
docker compose exec -T db pg_dump \
  --username=postgres \
  --dbname=linkedin_automation \
  --format=custom \
  --no-owner \
  --file=/tmp/linkedin-automation.dump
docker compose cp db:/tmp/linkedin-automation.dump ./linkedin-automation.dump
```

Back up the named `app_storage` volume with the platform's volume snapshot facility or a read-only backup container. Ensure the backup process preserves image filenames and bytes exactly.

Recommended policy:

- encrypted daily database and image backups;
- retention appropriate to the organization's data policy;
- off-host copy with access logging;
- automated backup-success alert;
- monthly restore test;
- quarterly credential and retention review.

## Restore drill

Restoring overwrites or replaces database state. Perform it in staging first and keep the source backup immutable.

1. Stop the application and migration service so no workflow state changes.
2. Restore PostgreSQL into an empty staging database with `pg_restore --clean --if-exists` only after verifying the exact target database.
3. Restore the matching image-volume snapshot.
4. Run `alembic upgrade head`.
5. start the application with `DRY_RUN=true` and `AUTO_GENERATION_ENABLED=false`.
6. Check database health, topic/settings rows, post counts, pending queues, and several approved image digests.
7. Process no reconciliation-required post until a human has checked the real LinkedIn profile.
8. Perform a Telegram dry-run and then document the recovery time and any data gap.

Never use a production URL, broad filesystem path, or unverified database name as a destructive restore target.

## Monitoring and alert thresholds

Poll `GET /health` from outside the host every minute through HTTPS and alert after three consecutive failures. The endpoint verifies a database query and reports whether dry-run is active; it exposes no secrets.

Alert immediately on:

- `linkedin_publish_requires_reconciliation` or `linkedin_publish_unknown_outcome`;
- `approved_artifact_integrity_failed`;
- repeated HTTP 5xx responses;
- database storage or backup failure;
- an expired LinkedIn token.

Investigate within one hour when:

- a `notification_outbox` row is `dead`;
- a `telegram_update_inbox` row is `dead`;
- a `generation_runs` row is `failed`;
- an approved/scheduled queue age exceeds its expected time by 15 minutes;
- analytics has no successful snapshot for more than two scheduled collection cycles.

Useful read-only queries:

```sql
SELECT status, count(*) FROM posts GROUP BY status ORDER BY status;

SELECT id, post_id, kind, attempts, last_error, created_at
FROM notification_outbox
WHERE status = 'dead'
ORDER BY created_at;

SELECT update_id, attempts, last_error, created_at
FROM telegram_update_inbox
WHERE status = 'dead'
ORDER BY created_at;

SELECT slot, attempts, last_error, created_at
FROM generation_runs
WHERE status = 'failed'
ORDER BY created_at;

SELECT id, topic, publish_attempts, last_error, updated_at
FROM posts
WHERE status = 'publishing' AND requires_reconciliation IS TRUE
ORDER BY updated_at;
```

Application logs are structured JSON. Route them to a protected log service with retention and alerts for `content_generation_failed`, `telegram_update_processing_failed`, `telegram_delivery_failed`, `scheduled_publish_failed`, and `linkedin_analytics_collection_failed`.

## Incident: ambiguous LinkedIn result

This condition means a post request may have succeeded even though the service did not receive a definitive identifier. The system deliberately does not retry.

1. Read the full UUID in the Telegram warning.
2. Open the authenticated LinkedIn profile and check for the exact approved commentary and image.
3. If the post exists, copy its `urn:li:share:…` or `urn:li:ugcPost:…` identifier and send:

   ```text
   /reconcile <full-UUID> published <full-LinkedIn-URN>
   ```

4. If careful inspection confirms it does not exist, send:

   ```text
   /reconcile <full-UUID> not-published
   ```

5. For `not-published`, the unchanged artifact returns to `approved`. Press **HEMEN PAYLAŞ** only if another attempt is intended.

Never choose `not-published` merely because the post is slow to appear; that can produce a duplicate.

## Incident: Telegram delivery failure

Workflow records remain in PostgreSQL even when Telegram is unavailable. Restore network/token access, then send `/retryalerts`. This resets only notification rows that exhausted eight attempts. Inbox rows have their own retry/dead state; inspect a dead row's error before manually returning it to `pending`.

Changing the bot token requires updating `TELEGRAM_BOT_TOKEN` and restarting so the application registers the webhook for the new bot. Re-run the identity bootstrap if the bot or operator account changed.

## Incident: LinkedIn authorization failure

For HTTP 401/403:

1. send `/pause`;
2. inspect the token's active status, expiry, authenticated member, and `w_member_social` scope in Developer Portal;
3. generate/authorize a replacement token if needed;
4. update the token and exact expiry in the secret store;
5. run `make verify-linkedin` and `make preflight`;
6. restart, perform a dry-run, and send `/resume`.

A definitive authorization rejection is not automatically retried. Analytics authorization failure does not use scraping; disable analytics until `r_member_postAnalytics` is approved.

## Incident: OpenAI or research failure

Generation runs use five durable attempts with exponential backoff. A failed slot remains visible in `generation_runs` with a sanitized exception type/message. Confirm API status, quota, model access, DNS, and outbound HTTPS. An image-only failure is audited and can still yield a text draft. Do not lower quality or source gates as an availability workaround.

## Data maintenance

Completed Telegram payloads are replaced with an empty object immediately. Rows may be deleted after the desired operational audit window:

```sql
DELETE FROM telegram_update_inbox
WHERE status = 'completed'
  AND completed_at < now() - interval '30 days';
```

Do not update or delete `audit_events`, `revisions`, or `approvals`; database triggers enforce their immutability. Establish a documented archival/legal retention process instead of disabling the triggers.

Generated images are content-addressed and may be referenced by historical revisions or approvals. Do not garbage-collect an image merely because it is not the current `posts.image_path`. A future storage-maintenance tool should prove that no post, revision, or approval path references the digest before deletion.

## Rollback

Application rollback means deploying the previous known-good image while retaining the upgraded schema. Verify that the older code is compatible with every applied migration. If not, restore the matching pre-release database and image backup into an explicitly verified target.

During rollback:

- keep `AUTO_GENERATION_ENABLED=false`;
- keep `DRY_RUN=true` unless an already scheduled publication has been consciously approved to proceed;
- preserve all `publishing` and reconciliation-required rows;
- do not reset attempts or states in bulk;
- document the release SHA, migration revision, backup identifier, and operator decisions.
