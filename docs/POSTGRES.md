# Postgres Persistence

Maestro uses Postgres as the local development database from the beginning because memory, provenance, and structured retrieval are core product concerns.
The local service uses the `pgvector/pgvector:pg16` image so semantic memory retrieval can store
and compare embedding vectors inside Postgres.

## Start Local Postgres

The included Docker Compose service maps Postgres to host port `55432` to avoid conflicts with any Postgres already running on the Mac.

```bash
docker compose up -d postgres
```

After pulling changes that switch the image to `pgvector/pgvector:pg16`, recreate the container
so the extension is available. This does not delete the named volume:

```bash
docker compose up -d --force-recreate postgres
```

Check health:

```bash
docker compose ps postgres
```

Expected state:

```text
Up ... (healthy)
```

## Database URL

Default local URL:

```text
postgresql+psycopg://maestro:maestro@localhost:55432/maestro
```

This is also the default in `.env.example`.

## Run Migrations

```bash
source .venv/bin/activate
alembic upgrade head
```

Check current migration:

```bash
alembic current
```

Expected output includes:

```text
0002_memory_embeddings (head)
```

## Verify Seeded Domains

```bash
docker compose exec -T postgres psql -U maestro -d maestro -c "select key, name from domains order by key;"
```

Expected domains:

- `l3`
- `maestro-development`
- `ophi`
- `personal`
- `personal-irad-projects`
- `praxis`
- `usma`

## Reset Local Database

This deletes the local Docker database volume.

```bash
docker compose down -v
docker compose up -d postgres
alembic upgrade head
```

## Scope of This Layer

This persistence layer includes tables and ORM models for:

- users
- domains
- agents
- conversations
- messages
- tasks
- reports
- memory items
- memory proposals
- memory links
- memory embeddings
- tool connections
- tool calls
- artifacts
- seed packages
- scheduled runs

The Memory Curator agent, memory retrieval policy, high-impact approval behavior, and seed package processing workflow are intentionally implemented in later issues.
