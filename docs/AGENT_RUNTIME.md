# Agent Runtime Foundation

This layer is the bridge between Maestro memory and working domain agents. It can run a one-off
agent through the LLM gateway, while recurring/autonomous scheduling is still deliberately
separate. It defines the contracts that future agents will use so every agent does not invent its
own prompt glue, tool permissions, or session artifact format.

## Pieces

## UI

The web app now keeps pace with the runtime foundation:

- Domain tabs show an editable domain context, active agents, agent role/tasking fields, tool
  access selectors, and a prompt-package debug panel.
- The Maestro Development domain exposes the editable global Maestro context used by every
  prompt package.
- Each domain tab can create a new domain-scoped agent with default memory, artifact, and LLM
  gateway permissions.
- The Tools tab is tool-centric: select a shared tool, inspect which domains have credentials
  configured, see which agents have access, and edit the selected domain's credential/config
  payload for that tool.
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
POST /agents
GET /agents/{agent_key}
PATCH /agents/{agent_key}
DELETE /agents/{agent_key}
GET /agents/{agent_key}/tasks
GET /agents/global-context
PATCH /agents/global-context
GET /agents/domains
PATCH /agents/domains/{domain_key}
GET /agents/tools
GET /agents/tools/connections
PUT /agents/tools/connections
```

Tool connections are domain-scoped. Agents receive permission to use a shared tool, then the
runtime resolves the matching domain tool connection when it builds the tool manifest. For example,
the Gmail read tool should be one shared capability, while Praxis, Ophi, and Personal each have
their own Gmail credential/config connection. Secret-like config keys such as `api_key`, `token`,
`secret`, and `password` are redacted in API responses. This is still an MVP scaffold; a hardened
local secret store or keychain integration should replace raw database storage before sensitive
production credentials are added.

Deleting an agent currently archives it by setting `is_active=false` and clearing current tasking.
This removes it from the active registry without destroying historical tasks, reports, artifacts,
or memory provenance.

Agent task lists are available through `GET /agents/{agent_key}/tasks`. Manual runs create task
records immediately. While future orchestration will enqueue work asynchronously, this endpoint is
the first queue/read-model surface for both human-triggered and Maestro-triggered work.

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

### Run Once

Manual run once is the first execution envelope. It selects an agent, assembles the prompt package,
retrieves scoped memory, resolves tool access, calls the LLM gateway, records task/tool/report
provenance, and optionally stages an interaction artifact for memory curation. Set
`execute_llm: false` to prepare/debug the package without making a provider call. Explicit
`tool_requests` can be provided for MVP tool execution; the runtime executes those tools first,
records their `tool_calls`, appends the results to the assembled prompt, and then calls the LLM.
Set `auto_tool_loop: true` to let the agent LLM plan safe read-only tool calls from its tasking and
tool manifest before the final report.

API:

```text
POST /agents/{agent_key}/run-once
```

Example:

```bash
curl -s http://localhost:8000/agents/praxis-planning-agent/run-once \
  -H "Content-Type: application/json" \
  -d '{
    "task_instruction": "Prepare a Praxis partner brief.",
    "query_text": "Praxis partner brief",
    "use_semantic": true,
    "stage_interaction": true,
    "execute_llm": true,
    "tool_requests": [],
    "auto_tool_loop": false
  }'
```

Expected:

- response status is `completed` when the LLM call succeeds, `failed` when the provider call fails,
  or `prepared` when `execute_llm` is false
- `prompt_package` matches the prompt aggregation contract
- `task_id` is set for the manual run
- `tool_calls` includes the `llm.gateway` call when `execute_llm` is true
- `tool_calls` includes any explicit tools requested before `llm.gateway`
- `tool_loop` explains any agent-planned tool iterations when `auto_tool_loop` is true
- `report_id` and `output_text` are set when execution succeeds
- `scheduler.status` is `stubbed`
- if `stage_interaction` is true, a package lands in `maestro_dropbox/<domain>/inbox`

The resulting interaction artifact contains the agent output, task ID, tool call summary, generated
report reference, run ID, and execution status. The memory curator can process that artifact just
like a manually dropped file.

The scheduler is deliberately separate. Maestro will need a master scheduler service that can
coordinate recurring work, resource locks, exclusive tools, queue priority, and user approvals
without individual agents fighting over the same capabilities.

### Agent-Planned Tool Loop

The first autonomous tool loop is intentionally conservative:

1. Maestro or a human sends tasking to a domain agent.
2. Prompt aggregation provides global context, domain context, role prompt, memory, and the allowed
   tool manifest.
3. A planner LLM call (`llm.tool_planner`) returns a small JSON plan of tool calls.
4. The runtime validates each requested tool against the agent's permissions.
5. Only safe read tools are executed automatically in this slice:
   - `github.repo.get`
   - `github.issue.search`
   - `github.issue.get`
   - `github.pr.search`
   - `github.pr.get`
   - `github.pr.diff`
   - `github.pr.checks`
   - `github.file.get`
   - `github.file.search`
   - `github.repo.list`
6. Tool outputs are fed back into the final `llm.gateway` report call.

Write/action tools such as `github.issue.create`, `github.issue.comment`,
`github.issue.update`, and `github.repo.create` are available as explicit tools but are not
executed autonomously. If an agent-planned loop requests one, the runtime records an
`approval_required` tool-call entry with the proposed payload and rationale. This means a task like
"check out the latest PR" or "read the issue template" can plan and execute read tools, while
"create an issue" or "create a repo" becomes a visible proposed action for Chris to approve before
a later execution step runs it.

### Tool Execution Contract

Tool calls use a consistent adapter contract:

- `ToolExecutionRequest`: agent key, tool key, payload, dry-run flag
- `ToolExecutionContext`: session, domain, assigned agent, task, domain tool connection
- `ToolAdapter.execute(context, payload)`: returns a JSON-safe output payload
- `ToolExecutionService`: validates agent permission, resolves the domain connection, persists the
  `tool_calls` row, and stores the adapter result or failure

Persisted tool calls use this envelope:

- `tool_name`: stable shared tool key, such as `llm.gateway` or `gmail.read`
- `input_payload`: redacted/request-safe input summary
- `output_payload`: redacted/result-safe output summary
- `status`: `running`, `complete`, `failed`, or `approval_required`
- `error_message`: failure reason, if any
- `started_at` / `completed_at`: timing/provenance

Domain credentials are resolved through `tool_connections`; agent permissions do not store or own
credentials. A future hardened credential service should provide the actual secret material to tool
adapters at execution time without exposing it to prompts or ordinary API responses.

MVP GitHub tools:

- `github.repo.get`: reads repository metadata.
- `github.repo.list`: lists repositories for an authorized owner or organization.
- `github.repo.create`: creates a new repository after approval.
- `github.file.get`: reads a specific file or directory from the configured repository/ref.
- `github.file.search`: searches for files in the configured repository.
- `github.issue.search`: searches issues in the configured repository.
- `github.issue.get`: reads a specific issue.
- `github.issue.create`: creates an issue.
- `github.issue.comment`: comments on an issue.
- `github.issue.update`: updates issue title, body, labels, assignees, or milestone.
- `github.pr.search`: searches pull requests.
- `github.pr.get`: reads pull request metadata, files, comments, reviews, and status rollups.
- `github.pr.diff`: reads pull request diffs or changed filenames.
- `github.pr.checks`: reads pull request CI/check status.

These use the GitHub CLI (`auth_type: gh_cli`). A domain connection can rely on the active local
`gh` account for quick local testing:

```json
{"repo": "Caliperti1/Maestro"}
```

For real domain-specific accounts, prefer an environment-token reference:

```json
{
  "repo": "Praxis-Defense/groundtruth",
  "env_token_name": "PRAXIS_GITHUB_TOKEN"
}
```

Store this once as the domain connection for the provider key `github`. Every `github.*` tool in
that domain inherits the shared GitHub connection unless a more specific per-tool connection exists.
The adapter reads the configured environment variable and passes it to `gh` as `GH_TOKEN` for only
that tool process. This avoids storing tokens in the database and avoids mutating the globally
active `gh` account. `.env` is ignored by Git and is the right local place for these token variables
until a hardened credential store exists.

GitHub write tools are available as separate explicit tool keys. The current runtime can execute
them when explicitly requested; the next agent-execution layer should add an LLM planning loop,
approval gates for high-impact writes, retry handling, and user-visible proposed tool calls before
autonomous write execution.

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
