# Agent Runtime Foundation

This layer is the bridge between Maestro memory and working domain agents. It does not run
autonomous agents yet. It defines the contracts that future agents will use so every agent does
not invent its own prompt glue, tool permissions, or session artifact format.

## Pieces

## UI

The web app now keeps pace with the runtime foundation:

- Domain tabs show an editable domain context, active agents, agent role/tasking fields, tool
  access JSON, and a prompt-package debug panel.
- The Tools tab shows the shared tool registry, whether tools are shared or exclusive, connected
  domains, and which agents can access each tool.
- The Memory tab remains the staging, approval, retrieval, and source-review surface.

The domain and agent editors are intentionally admin/debug controls for MVP. They write through
the `/agents` API so edits affect future prompt-package assembly.

### Agent Registry

Agents are domain-scoped. An agent spec includes:

- domain key
- role summary
- role prompt
- memory context profile
- model profile
- allowed tool manifest
- active/current/scheduled metadata

The first seeded agents are:

- `praxis-planning-agent`
- `maestro-introspection-agent`

API:

```text
GET /agents
GET /agents/{agent_key}
PATCH /agents/{agent_key}
GET /agents/domains
PATCH /agents/domains/{domain_key}
GET /agents/tools
```

### Prompt Aggregation

Prompt aggregation assembles the package an agent would receive before an LLM call. It includes:

- global Maestro context
- domain context
- agent role prompt
- task instruction
- optional user context
- scoped memory context bundle
- authorized tool manifest
- output contract

The service uses the Memory context bundle endpoint internally. Agents should not query memory
tables directly.

API:

```text
POST /agents/{agent_key}/prompt-package
```

Example:

```bash
curl -s http://localhost:8000/agents/praxis-planning-agent/prompt-package \
  -H "Content-Type: application/json" \
  -d '{
    "task_instruction": "Prepare context for a Praxis partner follow-up call.",
    "query_text": "Praxis partner call",
    "use_semantic": true
  }'
```

Expected:

- agent domain is `praxis`
- assembled prompt includes global Maestro context
- assembled prompt includes Praxis domain context
- assembled prompt includes the Praxis agent role prompt
- memory context is scoped to Praxis
- unrelated Ophi/L3 domain context is absent
- tool manifest only includes tools allowed for the agent

### Interaction Artifact Packager

The packager turns "what just happened" into a structured artifact package that curators can
process later. It is the normal path from session activity to memory, task, event, or contact
curation.

The package can include:

- user input
- Maestro tasking
- agent output
- tool call summaries
- generated artifact references
- open questions
- next steps
- task/conversation IDs
- domain/agent keys
- provenance

API:

```text
POST /agents/interaction-artifacts
```

Example staged package:

```bash
curl -s http://localhost:8000/agents/interaction-artifacts \
  -H "Content-Type: application/json" \
  -d '{
    "domain_key": "praxis",
    "agent_key": "praxis-planning-agent",
    "user_input": "Prep the partner call.",
    "maestro_tasking": "Build a concise partner-call brief.",
    "agent_output": "Focus on training needs, transition risks, and partner follow-up owners.",
    "tool_calls": [{"tool_name": "memory.context_bundle", "status": "complete"}],
    "generated_artifacts": [{"name": "partner-call-brief.md", "uri": "reports/partner-call-brief.md"}],
    "open_questions": ["Who owns the next follow-up?"],
    "next_steps": ["Draft agenda.", "Confirm partner attendee list."],
    "stage": true
  }'
```

Expected:

- response contains `schema_version: maestro.interaction_artifact.v1`
- response contains `staged_path`
- staged file lands in `maestro_dropbox/praxis/inbox`
- the package is ready for the existing curator pipeline

## Session Memory

Session memory is not the same as durable memory. Session activity should first become an
interaction artifact. The curator can later decide what should become durable memory, task,
event, contact, or remain transient.

## Future Notes

- Think Tank should be an idea inbox/incubation surface that can later promote ideas into
  memory, tasks, projects, or docs.
- Tool registry and credentials should build on the agent tool manifest rather than duplicating
  tool implementations per domain.
- Printer-style exclusive tools need queueing, locks, status, and priority override.
- Introspection and tool-research agents should live in the Maestro Development domain once the
  agent runtime can execute scheduled work.
