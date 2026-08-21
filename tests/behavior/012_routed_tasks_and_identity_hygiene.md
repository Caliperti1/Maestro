# Behavioral Test 012: Routed Tasks and Identity Hygiene

## A. Calendar Monitor Ignores History

1. Enable the Praxis calendar monitor with at least one past and one future event in Google Calendar.
2. Reset or start the monitor, then wait through two polling intervals.
3. Open Maestro Calendar and filter to Praxis.

Expected:

- Future new/changed events can be synchronized.
- Past Google Calendar events are not enqueued or imported merely because Google reports a change.
- Existing future cancellations still synchronize.

## B. Contact and Organization Resolution

1. Ingest a contact represented only as `chris.flournoy.mil@army.mil`.
2. Ingest another record for `Chris Flournoy` with overlapping evidence.
3. Ingest legal-name variants such as `Example Corp` and `Example Corporation`.
4. Run routed hygiene or wait for its background pass.

Expected:

- The contact displays as `Chris Flournoy`, with the address in the email field.
- High-confidence duplicates merge automatically and preserve source refs, aliases, and alternate emails.
- Legal-suffix organization variants merge into one canonical organization.
- Ambiguous organization names use semantic/LLM adjudication and remain separate when evidence is weak.

## C. Scheduled Human Task

1. Create a task without a time estimate and give it a scheduled start.
2. Save it and open Calendar.
3. Drag or resize the task block.
4. Let its scheduled time pass without marking it done, then mark it done manually.

Expected:

- Maestro estimates a duration when none is supplied.
- A translucent, labeled Task block appears in the domain color.
- Dragging/resizing updates the todo schedule and estimate.
- Passing time does not complete the task.
- Completing it keeps the calendar block and crosses it out.

## D. Background Agent Task

1. Create a todo, enable **Assign to Maestro agents in the background**, and save.
2. Wait up to one todo-worker interval (30 seconds).
3. Inspect Active Workflows and the task detail.

Expected:

- The todo is planned and enqueued once as a one-time background workflow.
- Its detail shows queued/running status and links it to the workflow.
- Missing material information produces a clear question in the main Maestro channel.
- Normal scheduler approvals and blockers still apply.
- Completion marks the todo done, preserves its calendar projection, and posts one concise completion message with normal report/artifact access.

## E. Phone Inline Details

1. On a phone, open Todos, Contacts, Organizations, and Calendar.
2. Select an item, then another item, then use the close icon.

Expected:

- Details open immediately below the selected row (or immediately below the calendar for an event).
- Opening another item closes the previous detail.
- The close icon collapses the detail.
- Core fields can be edited and saved without scrolling to a separate detail pane.
