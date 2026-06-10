# Development Notes

## Operating Principles

- Treat `Maestro Design Thoughts.md` as the product and architecture north star.
- Treat `docs/BACKLOG.md` as the implementation backlog until GitHub issues become the source of execution.
- Keep C Suite workflows at the Maestro level, not as a separate domain.
- Use Postgres from the beginning.
- Build phone access early so the user can test on the move.
- Build memory early because later workflow quality depends on durable context.

## Implementation Order

1. Repository foundation and local app skeleton.
2. Phone-accessible web app.
3. Postgres persistence and initial schema.
4. Memory service, memory proposals, and Memory Curator.
5. Seed package ingestion.
6. Thin Daily Standup workflow.
7. Maestro Development domain GitHub/Codex loop.

## Memory Rules

- Agents can write logs.
- Agents can create artifacts.
- Agents can propose memory.
- Only the Memory Curator writes canonical memory.
- Low-impact memory can be auto-approved.
- Very high-impact memory requires user approval.
- Every important answer should eventually be explainable through reports, memory, artifacts, tool calls, or external sources.

## GitHub Workflow

- Create issues from the backlog before coding substantial features.
- Work from a branch named for the issue or milestone.
- Open draft PRs until the user has tested the behavior.
- Keep PRs focused on one work package.
- Include docs updates when architectural behavior changes.

## Local Hygiene

- Do not commit secrets or local credentials.
- Do not commit generated cache files such as `__pycache__` or `.DS_Store`.
- Add new environment variables to `.env.example` when introduced.
- Prefer small, testable increments over broad rewrites.
