# Work Packages

This file summarizes the first issue batches to create from `docs/BACKLOG.md`.

## Batch 1: Foundation

- Repository structure and project hygiene.
- README, development notes, style guide, agent instructions, and ignore rules.
- Validate existing architecture docs and migration draft.

## Batch 2: Local App Skeleton

- FastAPI backend skeleton with health check.
- Frontend skeleton with Maestro chat, reports feed, sidebar, domain shell, agent shell, and settings shell.
- LAN binding and phone access documentation.

## Batch 3: Postgres Persistence

- Postgres local setup.
- SQLAlchemy models and Alembic wiring.
- Repositories for users, domains, agents, conversations, messages, tasks, reports, artifacts, tool calls, memory, proposals, seed packages, and scheduled runs.
- Default domain seeding.

## Batch 4: Memory Core

- Scoped memory service.
- Context bundle retrieval.
- Memory proposal model and lifecycle.
- Memory Curator auto-approval for low-impact memory.
- High-impact approval queue.

## Batch 5: Seed Packages

- Raw knowledge package format.
- Seed package artifact storage.
- Curator processing path for docs, notes, decks, readouts, and old AI conversations.

## Batch 6: Agent Runtime

- Agent contract.
- Agent registry.
- In-process task queue.
- Report schema and report persistence.
- Recurring run configuration placeholder.

## Batch 7: Maestro Orchestrator

- Request routing.
- Parent task and domain subtask creation.
- Multi-agent report collection.
- Synthesis reports.
- Direct agent chat.

## Batch 8: Daily Standup MVP

- Maestro-level Daily Standup workflow shell.
- Domain brief contract.
- Thin stub agents for each domain.
- User response loop that creates follow-up tasks and memory proposals.

## Batch 9: Maestro Development Domain

- Self-reflection report agent.
- GitHub issue creation agent.
- Codex handoff path for branch, implementation, tests, push, and draft PR.

## Batch 10: First Integrations

- GitHub issue/PR/repo summary integration.
- Google Calendar read integration.
- Gmail read integration.
- Research provider integration.
- ClickUp/CRM read integration.
