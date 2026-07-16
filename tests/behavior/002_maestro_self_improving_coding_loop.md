# Behavior Test 002: Maestro Self-Improving Coding Loop

## Purpose

Verify that Chris can ask Maestro, including from the phone over Tailscale, to improve the Maestro application; Maestro can plan, execute coding work in an isolated worktree, create a reviewable pull request, merge only with approval, and reload the running application without losing the active system conversation.

This test is deliberately small. It proves the loop before we rely on it for larger feature work or background workflow improvements.

## Preconditions

- The Mac and phone are connected to the same Tailscale tailnet.
- Backend is running with `make backend-reload`.
- Frontend is running with `make frontend-tailscale`.
- The deployed Maestro checkout is on a clean, up-to-date `main` branch before an approved reload.
- The dedicated runtime lives at `/Users/christopheraliperti/Maestro-runtime`; the development workspace may remain on another work branch.
- The Maestro coding agent has access to `codex.task.run`, required GitHub tools, and `local.app.reload`.
- GitHub credentials are configured for the Maestro Development repository.
- Auto worker is enabled.

## Test Matrix

| Step | User input or action | Expected system behavior | Evidence to record |
| --- | --- | --- | --- |
| 2.1 | Start from a fresh Maestro topic on the phone. Send: `Change the color of the Send button in the Maestro app to blue.` | The composer clears immediately and the phone receives a working acknowledgement. No Praxis email-triage context, stale RFI, or unrelated workflow is included. | Timestamp and screenshot of the proposed plan. |
| 2.2 | Review the proposed workflow. | Maestro identifies Maestro Development, assigns the coding agent, selects a capable cloud model tier, and describes the intended code change conversationally. The plan is not treated as a scheduled workflow. | Work item, agent, model tier, and tool list. |
| 2.3 | Approve and run the workflow. | The workflow leaves the chat pane and appears under Active Workflows. The coding agent creates an isolated feature worktree and uses Codex to inspect and edit the application. It may iterate with LLM reasoning and tools. | Workflow progress, worktree/branch name, and agent run report. |
| 2.4 | Wait for completion. | Maestro reports completion conversationally. A run-log entry and report are created. A pull request is surfaced in the artifact renderer or workflow detail. The running `main` checkout is unchanged. | Pull request number, report title, and run-log entry. |
| 2.5 | Inspect the pull request and diff. Optionally send: `Make the blue #2563eb instead.` | The user can inspect the generated code before merging. A refinement creates or updates a bounded coding task with the prior report and PR context available. | Screenshot of PR/diff and any follow-up task. |
| 2.6 | Send: `Merge PR #<number> and reload Maestro.` Approve the merge and local reload cards when shown. | Merge and reload are independently approval-gated. After a successful merge, reload switches to clean `main`, fast-forwards from GitHub, and lets Uvicorn reload plus Vite HMR apply the changes. | Approval cards, merge result, reload result, and branch state. |
| 2.7 | On the phone, refresh only if needed and send a normal chat message. | The Send button is blue. The same Maestro channel remains usable and can answer a new message. No manual terminal restart is needed. | Phone screenshot and successful reply. |
| 2.8 | Repeat a small refinement or ask Maestro to explain the change. | Maestro can retrieve the relevant report/run context and continue as a collaborator without rebuilding unrelated email-triage work. | Follow-up response and retrieved context summary. |
| 2.9 | Introduce a harmless uncommitted change in the runtime, then approve delivery. | Maestro blocks before merge/reload, explains the changed path, and offers an approval-gated recovery stash. It never overwrites or discards the local change. | Runtime inspection output, recovery approval, and retry outcome. |

## Guardrail Tests

| Scenario | Expected behavior |
| --- | --- |
| The local checkout has uncommitted changes when reload is requested. | Reload is blocked with an actionable message; it never overwrites local work. |
| A PR exists but Chris has not approved merge. | No merge or reload is attempted. |
| Codex fails or times out. | The workflow records a clear failure, retains useful reports and artifacts, and surfaces an actionable retry or refinement path. |
| An unrelated workflow is active. | The coding workflow and unrelated workflow remain independently inspectable; neither leaks task context into the other. |
| The phone sends a request while the coding workflow runs. | The main Maestro channel remains available. The new request is acknowledged and processed independently. |

## Pass Criteria

- The requested UI change is implemented on an isolated branch and visible in a reviewable pull request.
- No code is merged or reloaded without explicit approval.
- A completed merge can be pulled into the running local application without manually restarting the backend or frontend.
- The remote phone experience stays responsive and preserves the single system-level Maestro channel.
- The completed workflow creates a useful run log and report that Maestro can reference later.

## Execution Trace

```mermaid
sequenceDiagram
    participant Chris as Chris on phone
    participant UI as Maestro UI
    participant Maestro as Maestro orchestrator
    participant Agent as Coding agent
    participant Codex as Codex worktree
    participant GitHub as GitHub
    participant Runtime as Local runtime

    Chris->>UI: Request small Maestro UI change
    UI->>Maestro: Submit message asynchronously
    Maestro-->>UI: Working acknowledgement and proposed plan
    Chris->>UI: Approve plan
    UI->>Maestro: Queue immediate workflow
    Maestro->>Agent: Enriched coding task
    Agent->>Codex: Create isolated worktree and implement
    Codex->>GitHub: Push branch and open PR
    Agent-->>Maestro: Report, PR, and artifacts
    Maestro-->>Chris: Conversational completion and review link
    Chris->>Maestro: Approve merge and reload
    Maestro->>GitHub: Merge approved PR
    Maestro->>Runtime: Checkout clean main and pull fast-forward
    Runtime-->>UI: Uvicorn reload and Vite HMR
    Chris->>UI: Verify blue Send button and continue chat
```

## Run Notes

For each attempt, append a short dated note containing: request text, workflow/run IDs, PR number, result, failures, and follow-up patches needed. Keep completed historical runs; this ledger records behavior and does not replace the system run log.
