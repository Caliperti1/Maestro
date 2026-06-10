# Maestro

Maestro is a locally hosted chief-of-staff system for coordinating work across personal, company, teaching, research, and software-development domains.

The core product bet is memory. Maestro should retain structured, provenance-linked knowledge across domains, delegate scoped work to domain agents, and synthesize cross-domain recommendations back to the user.

## Current MVP Direction

The first MVP should prove four things:

1. A local web app can be opened from a phone over LAN or Tailscale.
2. Maestro can persist conversations, tasks, reports, artifacts, tool calls, and memory in Postgres.
3. Domain agents can produce thin reports and propose memory while the Memory Curator owns canonical memory writes.
4. A thin Daily Standup workflow can task domain agents, collect reports, and synthesize cross-domain priorities.

## Architecture Docs

- [Architecture notes](Maestro%20Design%20Thoughts.md)
- [MVP backlog](docs/BACKLOG.md)
- [Main workflow sequence](docs/01_main_workflow_sequence.mmd)
- [System components](docs/02_system_components.mmd)
- [Database design](docs/03_database_design.mmd)

## Planned Stack

- Backend: FastAPI
- Database: Postgres
- ORM/migrations: SQLAlchemy + Alembic
- Frontend: React + Vite
- Local access: LAN first, Tailscale for secure phone/remote access

## Initial Repo Layout

```text
app/
  agents/
  api/
  core/
  db/
  domains/
  memory/
  tools/
  workflows/
alembic/
docs/
tests/
```

## Local Development

Create a virtual environment and install the backend:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Start the backend:

```bash
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
```

Install and start the frontend:

```bash
cd frontend
npm install
npm run dev
```

Open the local UI:

```text
http://localhost:5173
```

Backend health check:

```text
http://localhost:8000/health
```

Phone access instructions live in [docs/PHONE_ACCESS.md](docs/PHONE_ACCESS.md).

## Development Status

This repo is currently implementing the local app skeleton and phone-accessible Maestro shell.
