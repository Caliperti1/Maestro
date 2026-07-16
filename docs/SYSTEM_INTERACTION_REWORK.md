# Maestro System Interaction Rework

This PR moves Maestro from a debug-first orchestration UI toward the intended daily operating
model: Chris interacts with one Maestro chat, while workflows, reports, routed items, artifacts,
notifications, and run logs become durable system outputs that Maestro can retrieve and inspect.

## Target Operating Model

Maestro is the orchestration agent Chris talks to. The broader application is the system. Chris
should not need to directly manage queue internals during normal use.

Maestro can answer conversationally by retrieving from:

- durable memory
- workflow reports
- workflow run logs
- routed objects: contacts, events, organizations, todos, ideas
- artifact metadata
- web search

Maestro creates workflows only when the message calls for agent/tool execution or durable scheduled
work. Workflows may run immediately, on a schedule, or from a trigger.

## Workflow Output Contract

Every workflow run should produce some or all of these outputs:

- **Report**: human/agent-readable Markdown with meaningful information gathered or produced.
- **Routed Items**: contacts, events, organizations, todos, ideas created or updated.
- **Tangible Artifacts**: files, code, generated assets, PRs, documents, etc.
- **Notification**: urgent or relevant message surfaced through the main Maestro channel.
- **Run Log Entry**: durable inspection record of what ran, what agents did, what changed, and
  which outputs were created.

## Ten-Step Implementation Plan

### 1. Data Model Cleanup

- Add/normalize first-class tables for workflow run logs, notifications, skills, and agent-skill
  assignments.
- Connect existing reports, artifacts, routed items, workflow runs, tasks, and queue items into the
  new output model.

### 2. Workflow Output Contract

- Define a canonical workflow completion payload.
- Update orchestrator and scheduler worker completion paths to write run-log entries and link
  reports, routed items, artifacts, and notifications.

### 3. Maestro Context Assembler

- Build a retrieval service for Maestro chat that can pull memory, reports, run logs, routed
  objects, artifact metadata, and web search into a bounded context bundle.

### 4. Main UI Simplification

- Make the main page primarily a Maestro chat on the left and an artifact/report renderer on the
  right.
- Move queue/scheduler/debug surfaces out of the primary conversation path.

### 5. Reports UI

- Add a reports shelf/list.
- Add a Markdown report renderer.
- Support filters by domain, workflow, agent, date, and report type.

### 6. Run Log UI

- Add chronological workflow run log.
- Expand entries to inspect agents, work items, reports, artifacts, routed changes, notifications,
  and status.

### 7. Workflows UI

- Show only active and durable workflows.
- Completed one-time workflows leave the workflow tab and appear in the run log.
- Durable workflows show schedule/trigger config and recent runs.

### 8. Skills System

- Add skill registry and skill assignment to agents.
- Include only assigned skills in prompt aggregation.
- Add a skill editor/viewer in the UI.

### 9. Behavioral Test Redesign

- Replace debug-era tests with new behavior suites covering direct chat, one-time workflow,
  recurring workflow, report generation, run-log inspection, routed item updates, notifications,
  and skill-scoped agent execution.

### 10. Migration and Deprecation

- Hide or remove old debug panes from the main surface.
- Keep scheduler/queue internals inspectable but no longer central to daily use.
- Delete stale stubs as they are replaced.

## PR Completion Checklist

- [x] Step 1: Data model cleanup
  - Added workflow run-log entries, workflow notifications, skill registry, and agent skill
    permissions.
- [x] Step 2: Workflow output contract
  - Scheduler completion now records canonical run-log entries, delivered notifications, linked
    reports, staged workflow artifacts, and agent work summaries.
- [x] Step 3: Maestro context assembler
  - Added report retrieval tools (`reports.search`, `reports.get`) so agents can retrieve workflow
    reports separately from durable memory.
  - Added `MaestroContextAssembler` and `/maestro/context-bundle` to combine durable memory, routed
    objects, recent reports, run logs, artifact metadata, and web-search availability into one
    bounded prompt-ready bundle.
  - Maestro direct chat and planning now use the unified bundle, with metrics stored on proposed
    workflow tasks for debugging token/context size.
- [x] Step 4: Main UI simplification
  - Added Maestro dropdown surfaces for Chat, Run Log, Workflows, and Reports. The primary chat
    dashboard now focuses on the Maestro conversation plus a right-side artifact/report renderer
    with compact attention items for blocked workflows, approvals, and RFIs.
- [x] Step 5: Reports UI
  - Added report shelf and Markdown renderer backed by `/workflow-outputs/reports`.
- [x] Step 6: Run Log UI
  - Added chronological run-log surface backed by `/workflow-outputs/run-log`.
- [x] Step 7: Workflows UI
  - Added dedicated Workflows surface for active runs, schedules, triggers, and run inspection.
- [x] Step 8: Skills system
  - Added skill registry API/UI, agent skill assignment UI, and prompt aggregation of assigned
    skills only.
- [x] Step 9: Behavioral test redesign
  - Updated the behavior matrix to test the new output contract, Run Log, Reports, Workflows, and
    Skills surfaces.
- [x] Step 10: Migration and deprecation
  - Removed the old hidden dashboard Queue panel in favor of the dedicated Workflows surface.
  - Updated stale Queue-facing copy and kept scheduler internals available through Workflows rather
    than the primary chat dashboard.
