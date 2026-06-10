# Agent Instructions

These instructions apply to coding agents working in this repository.

## Source of Truth

- Product direction: `Maestro Design Thoughts.md`
- Work breakdown: `docs/BACKLOG.md`
- System diagrams: `docs/*.mmd`
- Schema draft: `alembic/versions/0001_initial_maestro_schema.py`

## Architectural Rules

- Maestro owns cross-domain workflows, routing, delegation, and synthesis.
- C Suite workflows are Maestro-level workflows, not a separate domain.
- Agents operate inside a single domain and cannot directly access unrelated domain memory.
- Maestro can retrieve from global memory and all domain memories.
- Agents may write logs, create artifacts, and propose memory.
- Only the Memory Curator may write canonical memory.
- Very high-impact memory requires user approval.
- All tool use must be logged.
- All agent outputs should produce report objects.

## Coding Rules

- Keep changes scoped to the issue or work package.
- Do not introduce broad abstractions before the local patterns exist.
- Prefer clear services and small modules over clever framework magic.
- Preserve provenance fields when handling memory, reports, artifacts, or tool calls.
- Never commit secrets, local cache files, or generated artifacts unless explicitly intended.

## GitHub Rules

- Work against GitHub issues once they exist.
- Use draft PRs until the user has tested the change.
- Include verification notes in PRs.
- If behavior is incomplete, state the limitation plainly.
