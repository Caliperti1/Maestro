# Behavior Test 014: On-Demand Workflows

## Goal

Prove that Chris can invoke an already-approved repeatable playbook from the normal Knowledge chat
without opening Build workflow mode, while new workflow design remains explicit.

## Preconditions

- The scheduler auto worker is on.
- The Daily Standup template is installed and active under **Workflows > On-demand**.
- Personal, Perti Laboratories, Praxis, and Maestro Development domains contain enough current
  calendar, todo, issue, report, or memory context to distinguish their outputs.

## Matrix

| ID | Input / action | Expected behavior | Evidence | Status |
|---|---|---|---|---|
| 14.1 | In Knowledge mode say: `What does my Daily Standup workflow do?` | Maestro explains the existing playbook. It does not enqueue a run. | No new Active run or run-log entry. | Not run |
| 14.2 | Say: `Prepare my daily standup.` | Maestro resolves the active on-demand definition, starts it once, and conversationally says it is running in the background. No proposed-plan card appears. | One `knowledge_on_demand` run appears under Active. | Not run |
| 14.3 | Open the active run. | Personal, Perti, and Praxis input items are Stage 1 and can run in parallel. Standup synthesis is Stage 2 and waits for all three reports. | Workflow map and queue-item detail. | Not run |
| 14.4 | Wait for completion. | The run leaves Active. A run-log entry and reports are created. Maestro sends one conversational completion message; blockers return through the same channel if needed. | Main chat, Run Log, Reports. | Not run |
| 14.5 | Ask: `Show me the standup report and help me adjust today's priorities.` | Maestro retrieves the completed report and discusses it in Knowledge mode. Requested immediate todo/calendar edits can be applied without rerunning the standup. | Rendered report and canonical record changes. | Not run |
| 14.6 | Say: `Prepare my daily standup with extra focus on Praxis partner follow-through.` | The same definition runs with `focus` captured in the invocation parameters and available to every queue item. | Run detail input and report emphasis. | Not run |
| 14.7 | Say: `Create a new weekly GroundTruth scrum workflow.` | Knowledge mode does not invent or enqueue it. Maestro recommends switching to Build workflow mode. | No definition or run created. | Not run |
| 14.8 | In Workflows, click **Run now** on Daily Standup. | The same scheduler path runs from the UI without requiring a chat command. | One new on-demand run and normal outputs. | Not run |

## Pass Criteria

- Existing on-demand playbooks are callable from Knowledge chat and the Workflows UI.
- Each explicit request creates at most one run.
- Questions about a playbook never execute it.
- Parallel domain work and dependent synthesis are visible and ordered correctly.
- Runs use the standard scheduler, reports, run log, memory staging, blockers, and channel delivery.
- Knowledge mode still cannot design a new workflow.
