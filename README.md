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
- Frontend: React or a lightweight server-rendered UI, to be decided during implementation
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

## Development Status

This repo is currently in planning and foundation setup. The next step is to turn [docs/BACKLOG.md](docs/BACKLOG.md) into GitHub issues and begin implementation from Milestone 0.
