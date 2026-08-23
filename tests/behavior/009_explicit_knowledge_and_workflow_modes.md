# Behavior Test 009: Explicit Knowledge And Workflow Modes

## Purpose

Prove that Maestro's main channel has a dependable operating boundary: Knowledge mode can reason
over Chris's world and make bounded canonical updates, while Build workflow is the only mode that
can create and delegate new agent work.

## Setup

1. Restart backend and frontend from the PR branch.
2. Confirm the main composer shows `Knowledge` selected and `Build workflow` unselected.
3. Have one known contact, one active durable workflow, and the Praxis domain available.
4. Note the current Task count or active-workflow list so unexpected delegation is visible.

## Automated Gate

```bash
pytest -q
cd frontend && npm run build
```

## Human Matrix

| ID | Human action | Expected behavior | Evidence | Status |
| --- | --- | --- | --- | --- |
| 9.1 Knowledge query | In Knowledge mode ask: `What Praxis meetings and follow-ups do I have this week?` | Maestro answers conversationally from events, todos, memory, reports, and identity context. No plan preview or Task is created. | Chat answer and unchanged active-workflow list. | Not run |
| 9.1a Iterative routed edit | Say: `Find the Collaborative Autonomy Standup, move it to Room 204, and confirm the saved event.` | Within one chat turn Maestro searches events, resolves the canonical UUID, updates it, searches again to verify Room 204, and answers conversationally. It creates no Task or workflow. | Chat answer, action results in message metadata, and event detail. | Not run |
| 9.1b Immediate web search | Ask a question that explicitly requires current public information, such as: `What is the current published deadline for the SBIR opportunity we discussed?` | Maestro performs a focused web search inside Knowledge mode, reasons over its cited result, and answers without creating a workflow. | Conversational answer with source links and unchanged workflow list. | Not run |
| 9.2 Contact update | Say: `Update Jane Smith's Praxis notes: she owns the partner follow-up. Her phone is 555-0100.` | Existing Jane resolves unambiguously, manual fields and Praxis domain note update, provenance points to the Maestro message, and no workflow is created. | Contact detail and domain note. | Not run |
| 9.3 Recurring event | Say: `Add a Praxis planning block every Monday from 9:00 to 9:30 AM starting next Monday.` | A recurring Praxis event is created with Eastern time and a weekly recurrence rule. It appears on each applicable calendar week. | Calendar event detail and recurrence rule. | Not run |
| 9.3a Recurring-event clarification | Say: `Create an L3 Collaborative Autonomy Standup every Monday through Thursday from now until the end of the year.` When Maestro asks for the missing time, reply only: `11-1130`. | Maestro treats the terse reply as the answer to its pending question, creates one event from 11:00 to 11:30 AM Eastern, and uses December 31 of the first occurrence's year as the recurrence end. The Calendar remains usable. | Chat history, event detail, and Calendar week view. | Not run |
| 9.3b Recurring-event retrieval | After 9.3a ask: `What's on my schedule next Monday?` | Maestro includes the Collaborative Autonomy Standup in its conversational answer without creating a workflow. | Chat answer and unchanged workflow list. | Not run |
| 9.4 Workflow edit | Say: `Pause the Praxis email triage workflow.` | The existing durable definition becomes inactive. No replacement workflow, run, or plan is created. | Durable workflow card state. | Not run |
| 9.5 Ambiguity | Ask to update a contact using a name shared by multiple people. | Maestro asks which record Chris means and writes nothing. | Clarifying chat response and unchanged records. | Not run |
| 9.6 Delegation boundary | In Knowledge mode say: `Have the Praxis agents research three competitors and prepare a report.` | Maestro explains that this needs Build workflow and does not create a draft, Task, run, or queue item. | Chat/status suggestion and unchanged workflow lists. | Not run |
| 9.7 Explicit workflow | Select Build workflow and repeat 9.6. | Maestro proposes a decomposed agent plan for review. It does not execute before approval. | Proposed-plan preview. | Not run |
| 9.8 Draft refinement | While still in Build workflow say: `Limit the research to US companies and add a pricing comparison.` | The existing draft is refined rather than a second draft being created. | One candidate plan with updated work items. | Not run |
| 9.9 Approval reset | Approve/run the draft. | The draft leaves main chat, appears under active workflows, and the mode returns to Knowledge so chat remains usable. | Mode control and Workflows surface. | Not run |
| 9.10 Existing blocker | Answer an RFI or approve a tool from Needs Attention while Knowledge is selected. | The exact blocked workflow resumes, including a background agent-task workflow that has no chat conversation ID; Maestro does not create a new plan. | Existing run ID resumes. | Automated; human run pending |
| 9.11 Agent-task completion quality | Mark a to-do as an agent task and force its agent report to state that a required source/action was unavailable. | The run log and report remain available, but Maestro keeps the originating to-do open, explains the failed quality check in chat, and closes only explicitly linked clarification to-dos after they are answered or the work passes. | To-do state, report status, run log, and channel message. | Automated; human run pending |

## Pass Criteria

- Knowledge is selected after load, workflow approval, and workflow dismissal.
- Knowledge mode never creates a new Task, workflow definition, workflow run, or queue item.
- Knowledge mode can search, act, and verify repeatedly in one bounded turn; dependent reads precede
  writes, completed writes are not repeated, and current web searches retain citations.
- Routed-store writes use canonical resolver/edit services, retain source-message provenance, and ask
  for clarification instead of guessing ambiguous targets.
- Build workflow creates/refines exactly one candidate until it is approved or cleared.
- Approval and RFI control-plane actions still resume the workflow that raised them.

## Run Record

| Date | Commit/PR | Tester | Automated | Human result | Defects / follow-up |
| --- | --- | --- | --- | --- | --- |
| | | | Not run | Not run | |
