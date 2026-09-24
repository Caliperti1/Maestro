# Maestro Cloud Deployment Runbook

Status: staging-ready foundation. Owner authentication and private object writes are implemented;
do not import the production database or treat the cloud service as authoritative until the
remaining gates and smoke tests below are complete.

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

- `Dockerfile`: one non-root image for the API and worker (`Dockerfile.backend` remains as the
  original compatibility entry point).
- `app.operations.api`: production Uvicorn entry point.
- `app.operations.worker`: signal-aware scheduler, Gmail trigger, and local-dropbox loops.
- `render.yaml`: one API instance, one worker instance, and private Render Postgres.
- `frontend/vercel.json`: Vite build and single-page-app routing.
- `deploy/render.env.example`: variable inventory without credentials.
- `app.operations.deployment_check`: fail-fast checks for database, origin, owner auth, object
  storage, and model settings.
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

The deployment check refuses promotion unless OIDC, secure owner cookies, exact origins, and a
private S3 artifact store are configured.

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

The Blueprint deliberately disables the filesystem memory-dropbox processor. Uploads and workflow
artifacts are durable in S3, but the current curator still scans a local filesystem. Keep automatic
dropbox processing disabled until the S3 inbox adapter lands; cloud memory already present in
Postgres remains available.

## Vercel Staging Setup

Create the Vercel project with `frontend/` as its project root. Configure this non-secret build
variable separately for Preview and Production:

```text
VITE_API_BASE_URL=https://the-corresponding-render-api.example.com
```

The API must list the exact deployed Vercel origin in both `FRONTEND_ORIGIN` and
`CORS_ALLOW_ORIGINS`. Do not put API keys, session secrets, or node credentials in a `VITE_*`
variable; Vite embeds those values in the public browser bundle.

## Cognito Managed Login Setup

When the Cognito user-pool domain uses managed login version 2, create a branding style for the
Maestro app client after creating the domain and OAuth client. Without this association Cognito
accepts the authorization request but displays `Login pages unavailable` instead of the sign-in
form.

```bash
aws cognito-idp create-managed-login-branding \
  --user-pool-id "$MAESTRO_COGNITO_USER_POOL_ID" \
  --client-id "$MAESTRO_COGNITO_CLIENT_ID" \
  --use-cognito-provided-values \
  --region "$MAESTRO_AWS_REGION"
```

Verify the association before smoke testing the frontend:

```bash
aws cognito-idp describe-managed-login-branding-by-client \
  --user-pool-id "$MAESTRO_COGNITO_USER_POOL_ID" \
  --client-id "$MAESTRO_COGNITO_CLIENT_ID" \
  --region "$MAESTRO_AWS_REGION"
```

## Single-Owner Authentication Release Gate

One human account reduces account-management work, but it does not make an internet-facing API
safe by itself. All of the following are required before public deployment:

- [x] Provision an OIDC provider and record the one permitted immutable `(issuer, subject)` identity.
- [x] Implement authorization-code flow with PKCE on the FastAPI backend; do not trust a browser-provided
      email address or identity claim without issuer, audience, nonce, and signature validation.
- [x] Issue a short-lived, revocable `Secure`, `HttpOnly` owner session cookie.
- [x] Add server-side session storage, revocation, expiry, and an explicit logout path.
- [x] Require authentication on every REST route except health, OIDC, and node protocol endpoints.
- [x] Require CSRF protection on every cookie-authenticated state-changing request.
- [x] Authenticate Maestro WebSockets before `accept()` and validate their exact origin.
- [x] Use an exact frontend-origin allowlist for HTTP and WebSocket origin validation.
- [x] Make the frontend send credentials, attach CSRF tokens, and handle session expiration.
- [x] Keep node credentials completely separate from owner sessions. A node token must never log in
      to the human API and an owner cookie must never lease node jobs.
- [ ] Rate-limit login, callback, enrollment, token refresh, and WebSocket-ticket endpoints.
- [x] Keep provider client secrets and session secrets out of Postgres and source control; Render
      stores them as encrypted environment values. Maestro does not store an OIDC refresh token.
- [x] Add tests proving unauthenticated REST and WebSocket requests are denied.
- [ ] Protect Vercel preview deployments as defense in depth; do not treat Vercel protection as
      protection for the separately hosted Render API.

The auth and artifact variables in `deploy/render.env.example` are the implemented interface.

## Other Release Gates

- [x] Make `app.api.main` honor `MAESTRO_PROCESS_ROLE=web` and skip its in-process scheduler,
      Gmail, and dropbox loops.
- [x] Ensure runtime database settings cannot turn a worker loop back on inside the API process.
- [ ] Replace FastAPI `BackgroundTasks` used for durable orchestration with persisted queue work.
- [x] Integrate `check_readiness()` as `/health/ready`; keep `/health` as process liveness.
- [~] Uploads and workflow artifacts write to private object storage; S3-backed curator ingestion
      remains to be implemented before enabling `MEMORY_DROPBOX_AUTORUN` in Render.
- [ ] Make scheduler claims atomic and add expired-lease recovery before using more than one worker.
- [ ] Add database pool sizing, `pool_pre_ping`, timeouts, and production observability.
- [ ] Decide whether the classifiers disabled in `render.yaml` should use deterministic fallback,
      hosted providers, or node execution.
- [ ] Confirm hosted embedding dimensions against the existing database before importing memory.
- [x] Add secure-cookie configuration and exact cross-origin checks as part of auth integration.
- [x] Rehearse backup restoration and forward migration against a disposable local Postgres clone.

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
