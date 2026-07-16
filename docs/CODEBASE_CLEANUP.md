# Codebase Cleanup Register

This register tracks cleanup work that should happen as Maestro moves from MVP backbone to
behavior hardening. It is intentionally practical: each item should make the system easier to test,
extend, or debug without changing product behavior.

## Completed In Current Cleanup Pass

- Extracted Maestro planner rule tables and token helpers into `app/maestro/planner_rules.py`.
- Removed the stale orchestrator fallback intent classifier path.
- Moved operational user display references to `Settings.user_display_name` where they affect
  stored state or approval messages.
- Renamed manual agent run scheduler status from `stubbed` to `manual_run`.
- Extracted frontend shared API/types/constants/helpers into:
  - `frontend/src/types.ts`
  - `frontend/src/api.ts`
  - `frontend/src/constants.ts`
  - `frontend/src/uiHelpers.ts`
- Deleted obsolete `.gitkeep` placeholders and unused empty `app/domains` / `app/workflows`
  packages.
- Added module-level documentation to the core API, channel, scheduler worker, orchestrator, agent
  runtime, tool runtime, and routed-memory service entry points.

## High-Value Next Refactors

### Frontend App Split

`frontend/src/App.tsx` is still the largest single file in the repo, but shared types and pure
helpers now live outside it. Continue splitting component sections into:

- `frontend/src/components/maestro/*`: chat, workflow plan, scheduler queue, and needs-attention
  panels.
- `frontend/src/components/memory/*`: memory manager, routed calendar, contacts, todos, and
  organizations views.
- `frontend/src/components/admin/*`: domains, agents, and tools workspaces.

Target: `App.tsx` should become a shell/router under 500 lines.

### Orchestrator Service Split

`app/maestro/orchestrator.py` still owns planning, routing, scheduling, execution, synthesis, and
artifact staging. Split once current scheduler behavior stabilizes:

- `maestro/plan_builder.py`: create/refine plans and workflow graph payloads.
- `maestro/work_item_router.py`: direct routed-item promotion and routing-only responses.
- `maestro/execution.py`: phase execution, retries, approvals, and dependency context.
- `maestro/synthesis.py`: report synthesis and chat summary shaping.
- `maestro/artifacts.py`: canonical workflow interaction artifact packaging.

Target: the orchestration service should coordinate these collaborators instead of containing all
behavior directly.

### Tool Runtime Split

`app/tools/runtime.py` combines registry metadata, credential resolution, GitHub, Gmail, Codex,
app reload, approval handling, and payload rendering. Split by tool family:

- `tools/service.py`: approval state machine and dispatch.
- `tools/registry.py`: tool definitions, permission metadata, and family grouping.
- `tools/github.py`
- `tools/gmail.py`
- `tools/codex.py`
- `tools/app_runtime.py`

Target: each tool family can be tested without loading unrelated tool code.

### Routed Memory Service Split

`app/memory/routed_service.py` now handles enrichment, resolution, promotion, relationship linking,
payload rendering, and context bundles. The enrichment functions share parsing primitives with
promotion, so first extract shared parsing helpers, then split into:

- `routed_parsing.py`: route-title cleanup, contact/event/entity parsing, time/location parsing.
- `routed_enrichment.py`: deterministic/LLM field enrichment.
- `routed_promotion.py`: canonical object writes.
- `routed_payloads.py`: API payload rendering.
- `routed_relationships.py`: aliases and contact/entity relationships.

Target: extraction quality changes should not risk canonical-store write behavior.
