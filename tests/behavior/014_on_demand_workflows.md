# Behavior Test 014: On-Demand Workflows

## Goal

Prove that Chris can invoke an already-approved repeatable playbook from the normal Knowledge chat
without opening Build workflow mode, while new workflow design remains explicit.

## Preconditions

- The scheduler auto worker is on.
- The Daily Standup template is installed and active under **Workflows > On-demand**.
- Personal, Maestro Development, USMA, USMA, Perti Laboratories, and Praxis domains contain enough current
  calendar, todo, issue, report, or memory context to distinguish their outputs.

## Matrix

| ID | Input / action | Expected behavior | Evidence | Status |
|---|---|---|---|---|
| 14.1 | In Knowledge mode say: `What does my Daily Standup workflow do?` | Maestro explains the current definition, including Personal, Maestro Development, USMA, USMA, Perti Laboratories, and Praxis. It does not borrow rules from the separate Daily Context handoff and does not enqueue a run. | No new Active run or run-log entry. | Not run |
| 14.2 | Say: `Give me my morning operating picture.` | Maestro semantically resolves this as an instruction to run the active Daily Standup, starts it once, and conversationally says it is running in the background. No exact workflow name or invocation alias is required and no proposed-plan card appears. | One `knowledge_on_demand` run appears under Active. | Not run |
| 14.3 | Open the active run. | Personal, Maestro Development, USMA, USMA, Perti, and Praxis input items are Stage 1 and can run in parallel. Standup synthesis is Stage 2 and waits for all six reports. | Workflow map and queue-item detail. | Not run |
| 14.4 | Inspect the domain reports. | Every report distinguishes scheduled commitments from recommendations, covers open todos and Product Issues, proposes role-matched agent handoffs, and asks only material questions for Chris. Empty domains identify missing context rather than inventing work. | Reports and queue-item detail. | Not run |
| 14.5 | Wait for completion. | The run leaves Active. A run-log entry and reports are created. Maestro sends one conversational completion message derived from the final synthesis, not a list of agent snippets. | Main chat, Run Log, Reports. | Not run |
| 14.6 | Reply: `Move the Praxis partner follow-through to 2 PM, leave USMA unchanged, and assign the Maestro issue review to the best development agent.` | Maestro treats the reply as feedback on the active standup, asks only for unresolved details, and applies accepted calendar/todo/issue changes through Knowledge actions. It does not draft a new standup workflow. | Chat, Calendar, Todos, Product Issues. | Not run |
| 14.7 | Ask: `Now give me the synthesized plan for the day.` | Maestro returns the revised cross-domain operating picture using the standup report and canonical changes just made. | Main chat. | Not run |
| 14.8 | Say: `Prepare my daily standup with extra focus on Praxis partner follow-through.` | The same definition runs with `focus` captured in the invocation parameters and available to every queue item. | Run detail input and report emphasis. | Not run |
| 14.9 | Say: `Create a new weekly GroundTruth scrum workflow.` | Knowledge mode does not invent or enqueue it. Maestro recommends switching to Build workflow mode. | No definition or run created. | Not run |
| 14.10 | In Workflows, click **Run now** on Daily Standup. | The same scheduler path runs from the UI without requiring a chat command. | One new on-demand run and normal outputs. | Not run |

## Pass Criteria

- Existing on-demand playbooks are callable from Knowledge chat and the Workflows UI.
- Each explicit request creates at most one run.
- Questions about a playbook never execute it.
- Six parallel domain lanes and dependent synthesis are visible and ordered correctly.
- Immediate follow-up remains grounded in the final synthesis and can update canonical records.
- Runs use the standard scheduler, reports, run log, memory staging, blockers, and channel delivery.
- Knowledge mode still cannot design a new workflow.
