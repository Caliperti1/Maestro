# Praxis Email Agent

The Praxis Email Agent is the first reusable domain email-triage pattern. It is designed to run as
either a manual test over the latest Praxis Gmail message or, later, as a trigger-driven recurring
workflow when a new message arrives.

## Current MVP Flow

1. Read recent Praxis Gmail through the domain Gmail connection.
2. Fetch the selected message or thread.
3. Inspect relevant linked Google artifacts. Drive folder links are listed first, then only the
   relevant child Docs, Slides, or Sheets are read.
4. Classify the email as spam/noise, response needed, useful information, or action required.
5. Create internal routed candidates for useful extracted objects:
   - contacts via `contact_manager`
   - Chris-owned todos/reminders via `to_do_manager`
   - events via `calendar_manager`
   - organizations via `organization_manager`
6. Promote routed candidates immediately into Maestro routed stores.
7. Notify Chris only when a response, decision, material deadline, or meaningful risk requires his
   attention.
8. Generate an agent report and stage the interaction artifact for memory curation.

## Safety

Gmail read tools are auto-executable. Gmail writes, including marking a message read, still require
Chris approval. Internal routed candidate creation is auto-executable because it writes only to
Maestro stores and preserves source provenance.

## Model Routing

The seeded agent uses `model_profile = "openrouter:openai/gpt-5.6-luna"`. Full email triage needs
reliable multi-step tool planning, ownership and temporal reasoning, routed-item extraction, and
notification judgment; Luna is the cost-efficient cloud default for that workload. Narrow,
well-bounded email operations may still be explicitly assigned to local Qwen. Maestro can also
attach `model_profile` to an individual work item. Runtime precedence is:

1. work-item `model_profile`
2. workflow definition `model_profile`
3. assigned agent `model_profile`
4. global default

Maestro now sets the work-item override from an explicit routing tier and stores the decision on the
queue item: `luna`, `terra`, or `sol`. Routine email triage defaults to `luna`; a request that
needs broader reasoning, drafting, or external research defaults to `terra`. `sol` is reserved for
work where Chris explicitly requests the strongest model.

- `default`: configured global provider/model
- `ollama:<model>`: local Ollama chat model
- `openrouter:<model>`: OpenRouter model override
- `openai:<model>`: OpenAI model override

## Skills

Skills are reusable playbooks. Maestro sees compact skill metadata during planning, then attaches
specific `required_skills` to each work item. The prompt aggregator injects only those required
skills into the assigned agent's prompt so planning stays registry-aware while agent execution stays
scoped.

The Praxis Email Agent currently uses:

- `email_triage`
- `contact_manager`
- `to_do_manager`
- `calendar_manager`
- `organization_manager`

The agent also has `workflow.notification.create`. This is an internal, auto-executable Maestro
tool, not an external side effect. It writes a provenance-linked notification and posts it to the
main Maestro channel. Informational email should remain quiet.

## Human Test

Run the Praxis Email Agent once with auto tool loop enabled:

> Review the latest Praxis email. Classify it, tell me if I need to respond, route any contacts,
> organizations, events, or Chris-owned todos, then produce a concise report.

Expected behavior:

- The agent uses `gmail.message.list_recent`, then `gmail.message.get` or `gmail.thread.get`.
- It uses `routed.item.create` for any extracted contacts/events/todos/organizations.
- New routed objects appear in Memory dropdown views.
- A report is created for the run.
- An interaction artifact is staged for memory curation.
- If the agent wants to mark the email read or create a draft, Maestro asks for approval first.

## Durable Trigger Foundation

The one-time workflow is the behavior kernel for the durable version. The scheduler now provides:

1. Poll Gmail History from a persisted per-domain cursor and emit `gmail.message.received` events.
   Start from the current cursor so first enablement does not unexpectedly process the old inbox.
2. Freeze the Gmail message ID into each workflow input. A queued run must never reinterpret
   "latest email" after another message arrives.
3. Deduplicate trigger deliveries by domain, workflow definition, and Gmail message ID so polling,
   retries, and restarts cannot process one email twice.
4. Filter for eligible inbox messages and exclude drafts/sent mail before enqueueing work.
5. Retry transient Gmail, Google Workspace, LLM, and routing failures with bounded backoff; surface
   terminal failures to Needs Attention and support manual replay.
6. Expose trigger health, cursor freshness, last detected message, and recent runs in the workflow
   UI. Keep independent messages parallel subject to scheduler resource limits.
7. Unit and API coverage for cursor bootstrap/reset, duplicate history pages, exact-message event
   payloads, filtering, worker toggles, and replay with the original message payload.

Gmail History polling is the recommended local-runtime MVP. Gmail push notifications can replace
the producer later, but require Google Cloud Pub/Sub plus periodic watch renewal; the workflow event
contract and idempotency rules should remain the same.

The producer remains disabled until the Praxis durable workflow definition is created and reviewed.
That next behavior slice must test multiple real messages, quiet completion, routed objects,
Chris-action notifications, workflow retry, and restart recovery before unattended activation.
