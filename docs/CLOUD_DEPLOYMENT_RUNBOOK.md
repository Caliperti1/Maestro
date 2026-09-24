# Maestro Cloud Deployment Runbook

Status: deployment scaffold only. Do not expose the current API publicly until every release gate
below is complete.

## Intended Topology

```text
Vercel static Vite UI
        |
        | HTTPS / WSS
        v
Render FastAPI service ---- Render Postgres + pgvector
                                  ^
                                  |
                         Render background worker
```

The repository contains:

- `Dockerfile.backend`: one non-root image for the API and worker.
- `app.operations.api`: production Uvicorn entry point.
- `app.operations.worker`: signal-aware scheduler, Gmail trigger, and local-dropbox loops.
- `render.yaml`: one API instance, one worker instance, and private Render Postgres.
- `frontend/vercel.json`: Vite build and single-page-app routing.
- `deploy/render.env.example`: variable inventory without credentials.
- `app.operations.deployment_check`: fail-fast checks for cloud database, origin, and model settings.
- `app.operations.readiness`: the Postgres connectivity check used by `/health/ready`.

The Render Postgres connection string is normalized to SQLAlchemy's `psycopg` driver automatically.

## Local Packaging Check

Build and run the same backend image Render will use:

```bash
docker build -f Dockerfile.backend -t maestro-backend:local .
docker run --rm \
  -p 8000:8000 \
  --env-file .env \
  maestro-backend:local
```

Run the standalone worker against the local database in a second terminal:

```bash
docker run --rm \
  --env-file .env \
  --network host \
  -e SCHEDULER_WORKER_AUTORUN=true \
  -e MEMORY_DROPBOX_AUTORUN=false \
  maestro-backend:local python -m app.operations.worker
```

`--network host` behaves differently outside Linux. On macOS, give `DATABASE_URL` a
`host.docker.internal` host instead.

Validate a proposed production environment before migration:

```bash
python -m app.operations.deployment_check
python -m app.operations.readiness
alembic upgrade head
```

The deployment check treats owner authentication as a fatal release gate. The Render pre-deploy
command will refuse to promote the service until that gate is replaced by the implemented auth
check.

## Render Staging Setup

1. Complete the release gates below first.
2. Create a Render Blueprint from the repository's `render.yaml`.
3. Supply the prompted values from `deploy/render.env.example`; never paste them into the Blueprint.
4. Replace the placeholder `OPENROUTER_HTTP_REFERER` value in the Render environment group with the
   deployed frontend origin.
5. Keep both services at one instance. Queue claims are not yet safe for horizontal workers.
6. Confirm the migration pre-deploy command succeeds before starting the worker.
7. Use only synthetic or sanitized staging data until artifact storage, auth, and retention are
   verified.

The Blueprint deliberately disables the filesystem memory dropbox. Render instance files are
ephemeral and are not shared between the API and worker. Do not re-enable it until ingestion uses a
private object store.

## Vercel Staging Setup

Create the Vercel project with `frontend/` as its project root. Configure this non-secret build
variable separately for Preview and Production:

```text
VITE_API_BASE_URL=https://the-corresponding-render-api.example.com
```

The API must list the exact deployed Vercel origin in both `FRONTEND_ORIGIN` and
`CORS_ALLOW_ORIGINS`. Do not put API keys, session secrets, or node credentials in a `VITE_*`
variable; Vite embeds those values in the public browser bundle.

## Single-Owner Authentication Release Gate

One human account reduces account-management work, but it does not make an internet-facing API
safe by itself. All of the following are required before public deployment:

- [ ] Choose an OIDC provider and record the one permitted immutable `(issuer, subject)` identity.
- [ ] Implement authorization-code flow on the FastAPI backend; do not trust a browser-provided
      email address or identity claim without issuer, audience, nonce, and signature validation.
- [ ] Issue a short-lived, revocable `Secure`, `HttpOnly` owner session cookie.
- [ ] Add server-side session storage or a revocation strategy and an explicit logout path.
- [ ] Require authentication on every REST route except liveness, readiness, and OIDC callbacks.
- [ ] Require CSRF protection on every cookie-authenticated state-changing request.
- [ ] Authenticate the Maestro WebSocket before `accept()`; preferably exchange the owner session
      for a short-lived, single-use WebSocket ticket.
- [ ] Use an exact frontend-origin allowlist for HTTP and WebSocket origin validation.
- [ ] Make the frontend send the intended credentials and handle login/session expiration.
- [ ] Keep node credentials completely separate from owner sessions. A node token must never log in
      to the human API and an owner cookie must never lease node jobs.
- [ ] Rate-limit login, callback, enrollment, token refresh, and WebSocket-ticket endpoints.
- [ ] Encrypt provider refresh tokens and session secrets at rest using a key not stored in the
      database.
- [ ] Add tests proving unauthenticated REST and WebSocket requests are denied.
- [ ] Protect Vercel preview deployments as defense in depth; do not treat Vercel protection as
      protection for the separately hosted Render API.

The placeholder auth variable names in `deploy/render.env.example` are an inventory for this work,
not an implemented interface.

## Other Release Gates

- [x] Make `app.api.main` honor `MAESTRO_PROCESS_ROLE=web` and skip its in-process scheduler,
      Gmail, and dropbox loops.
- [x] Ensure runtime database settings cannot turn a worker loop back on inside the API process.
- [ ] Replace FastAPI `BackgroundTasks` used for durable orchestration with persisted queue work.
- [x] Integrate `check_readiness()` as `/health/ready`; keep `/health` as process liveness.
- [ ] Move uploads, workflow artifacts, and memory ingestion to private object storage.
- [ ] Make scheduler claims atomic and add expired-lease recovery before using more than one worker.
- [ ] Add database pool sizing, `pool_pre_ping`, timeouts, and production observability.
- [ ] Decide whether the classifiers disabled in `render.yaml` should use deterministic fallback,
      hosted providers, or node execution.
- [ ] Confirm hosted embedding dimensions against the existing database before importing memory.
- [ ] Add secure-cookie and trusted-proxy configuration as part of auth integration.
- [ ] Rehearse backup restoration and deployment rollback with synthetic staging data.

## Staging Smoke Test

After the gates are complete:

1. Confirm an unauthenticated REST request is rejected while `/health` remains reachable.
2. Confirm an unauthenticated WebSocket cannot connect.
3. Log in with the sole allowlisted identity and open the UI.
4. Verify `/health/ready` reports the migrated database as ready.
5. Create a cloud-only test workflow and watch the worker complete it exactly once.
6. Restart the API during the workflow and confirm no durable state is lost.
7. Restart the worker and confirm leases are recovered without duplicated side effects.
8. Disconnect and reconnect the Mac node and confirm node-dependent work waits and resumes.
9. Verify logs contain no credentials, sensitive tool payloads, or unreleased airlock content.

## Rollback

Use Render's previous image for the API and worker only when its schema remains compatible. Database
changes should follow expand/migrate/contract sequencing so the prior application version can run
during rollback. Do not roll back by restoring the database unless there is confirmed data
corruption and a tested restore procedure. Keep the prior local Maestro system read-only during the
first cloud cutover as a temporary recovery source.
