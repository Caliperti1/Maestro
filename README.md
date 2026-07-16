# Maestro

Maestro is a locally hosted AI chief-of-staff system. It provides one primary conversation channel
where the user can talk to Maestro, ask questions, create or refine workflows, route operational
items, approve tool use, and monitor autonomous work across multiple life and work domains.

The core product bet is persistent context. Maestro combines durable memory, routed operational
objects, domain agents, tool integrations, and a scheduler so the system can move from isolated
chat responses toward persistent execution.

## Current MVP Capabilities

- Single Maestro chat channel with persistent conversation history.
- Plan-first orchestration for complex requests.
- Domain-aware agent registry with editable global/domain/agent prompts.
- Prompt aggregation with scoped memory retrieval.
- Durable memory ingestion from drag-and-drop dropbox folders.
- Routed operational stores for contacts, events, todos, organizations, decisions, RFIs, and ideas.
- Scheduler and queue foundation for manual, recurring, and trigger-shaped workflows.
- Background scheduler worker that can be toggled from the UI.
- Tool runtime with approval gates, domain credential resolution, and agent permissions.
- GitHub, Gmail, Codex, and app reload tool foundations.
- Canonical workflow artifacts staged for memory curation at workflow completion.
- React/Vite frontend for Maestro chat, memory, domains, agents, tools, and queue surfaces.

## System Shape

```mermaid
flowchart LR
    UI["React Web UI"] --> API["FastAPI API"]
    API --> Maestro["Maestro Orchestrator"]
    API --> Memory["Memory + Routed Object APIs"]
    API --> Agents["Agent Registry + Runtime"]
    API --> Scheduler["Scheduler + Queue"]

    Maestro --> Planner["Planner / Work Items"]
    Planner --> Agents
    Maestro --> Scheduler
    Scheduler --> Worker["Background Worker"]
    Worker --> Agents

    Agents --> Prompt["Prompt Aggregator"]
    Prompt --> Retrieval["Memory Retrieval"]
    Agents --> Tools["Tool Runtime"]
    Tools --> External["GitHub / Gmail / Codex / Local App"]

    Maestro --> Artifacts["Workflow Artifacts"]
    Artifacts --> Dropbox["Memory Dropbox"]
    Dropbox --> Curator["Memory Curator"]
    Curator --> DurableMemory["Durable Memory"]
    Curator --> Routed["Routed Operational Stores"]

    DurableMemory --> DB[("Postgres")]
    Routed --> DB
    Scheduler --> DB
    Agents --> DB
```

## Core Concepts

### Maestro Channel

The UI is centered on one broad Maestro conversation rather than many narrow workflow chats. Maestro
can still preserve prior sessions and workflow context, but the main channel is where approvals,
RFIs, notifications, direct answers, and workflow progress surface.

Relevant code:

- `app/api/maestro.py`
- `app/maestro/channel.py`
- `app/maestro/orchestrator.py`

### Orchestration

The orchestrator receives user input, builds planning context, decomposes requests into work items,
selects agents, proposes a plan, runs approved work, handles approvals/RFIs, synthesizes results,
and stages a canonical workflow artifact.

The preferred planning path is LLM structured output. A deterministic planner remains as a fallback
when the LLM provider is unavailable.

Relevant code:

- `app/maestro/orchestrator.py`
- `app/maestro/planner.py`
- `app/maestro/planner_rules.py`
- `docs/MAESTRO_ORCHESTRATOR.md`

### Agents

Agents are domain-scoped workers with role prompts, memory profiles, model profiles, tool
permissions, and current/scheduled action metadata. Agents receive enriched prompts through the
prompt aggregation service and report results back to Maestro.

Relevant code:

- `app/agents/runtime.py`
- `app/api/agents.py`
- `docs/AGENT_RUNTIME.md`

### Tools

Tools are shared capabilities that agents can request through the tool runtime. The runtime resolves
domain credentials, checks agent permissions, records tool calls, enforces approval gates for risky
actions, and normalizes tool outputs.

Current tool families include:

- GitHub repository, issue, pull request, file, checks, and merge operations.
- Gmail read/search/thread/draft/modify foundations.
- Codex task execution in isolated worktrees.
- Local app reload/update operations.

Relevant code:

- `app/tools/runtime.py`
- `docs/AGENT_RUNTIME.md`

### Durable Memory

Durable memory is the RAG-style context layer. Files, workflow artifacts, and interaction packages
enter a dropbox, get parsed into candidate memories, are evaluated/deduplicated, and are written to
memory or held as proposals depending on impact and confidence.

Relevant code:

- `app/memory/dropbox.py`
- `app/memory/llm_curator.py`
- `app/memory/service.py`
- `app/memory/retrieval.py`
- `app/memory/embeddings.py`
- `docs/MEMORY_SERVICE.md`
- `docs/MEMORY_DROPBOX.md`
- `docs/MEMORY_CURATOR.md`

### Routed Operational Objects

Routed objects are operational records extracted from messages, artifacts, and workflow outputs.
They are separate from durable memory because they need to be editable, queryable, and visible in
task/calendar/CRM-style surfaces.

Current routed object types include:

- Events
- Todos
- Contacts
- Organizations
- Decisions
- RFIs / human input needs
- Think-tank ideas

Relevant code:

- `app/memory/routed_service.py`
- `app/memory/routed_resolver.py`
- `app/memory/routed_hygiene.py`
- `app/memory/routed_retrieval.py`
- `app/api/memory.py`

### Scheduler And Queue

The scheduler records recurring or trigger-shaped workflow definitions, creates workflow runs,
manages queue items, tracks resource locks and fairness groups, and lets the worker claim ready
items. The current background worker runs inside the FastAPI process and can be toggled from the UI.

Relevant code:

- `app/maestro/scheduler.py`
- `app/maestro/scheduler_worker.py`
- `app/api/scheduler.py`
- `docs/SCHEDULER_QUEUE.md`

## Repository Layout

```text
app/
  agents/       Agent registry, prompt aggregation, run-once execution, artifacts
  api/          FastAPI routers and application factory
  core/         Settings, logging, time helpers
  db/           SQLAlchemy models, repositories, seed data, session management
  llm/          OpenAI/OpenRouter client and structured extraction prompts
  maestro/      Orchestrator, planner, channel, scheduler, worker
  memory/       Durable memory, dropbox, retrieval, routed objects, embeddings
  tools/        Shared tool runtime and adapters
alembic/        Database migrations
docs/           Architecture notes and operational docs
frontend/       React/Vite frontend
tests/          Backend regression tests
```

## Local Development

### Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
make backend-reload
```

Health check:

```text
http://localhost:8000/health
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open:

```text
http://localhost:5173
```

For phone access, see [docs/PHONE_ACCESS.md](docs/PHONE_ACCESS.md).

## Configuration

Runtime settings load from `.env` through `app/core/config.py`. `.env` is ignored by git.

Common settings:

```bash
DATABASE_URL=postgresql+psycopg://maestro:maestro@localhost:55432/maestro
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
LLM_MODEL=openai/gpt-5.6-sol
LLM_QWEN_MODEL_PROFILE=ollama:qwen3:8b
LLM_LUNA_MODEL_PROFILE=openrouter:openai/gpt-5.6-luna
LLM_TERRA_MODEL_PROFILE=openrouter:openai/gpt-5.6-terra
LLM_SOL_MODEL_PROFILE=openrouter:openai/gpt-5.6-sol
MEMORY_DROPBOX_ROOT=maestro_dropbox
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text
ROUTED_RESOLVER_LLM_PROVIDER=ollama
MAESTRO_INTENT_CLASSIFIER_PROVIDER=ollama
SCHEDULER_WORKER_AUTORUN=false
USER_DISPLAY_NAME=Chris
```

Domain-specific tool credentials are stored as tool connections in the database. Connection config
can reference environment variable names for secrets, so secrets do not need to be stored directly
in the database.

## Memory Dropbox

The default dropbox root is:

```text
maestro_dropbox/
```

Domain inboxes live under that root, for example:

```text
maestro_dropbox/praxis/inbox/
maestro_dropbox/maestro-development/inbox/
maestro_dropbox/personal/inbox/
```

The frontend Memory Manager can upload files into these inboxes. The backend can also process them
through the dropbox service and write memory/routed candidates.

## Testing

Run the backend suite:

```bash
source .venv/bin/activate
pytest -q
```

Run the frontend build:

```bash
cd frontend
npm run build
```

Useful focused backend suites:

```bash
pytest tests/test_maestro_orchestrator.py -q
pytest tests/test_agent_runtime.py -q
pytest tests/test_memory_api.py -q
pytest tests/test_scheduler_api.py -q
```

## Key Docs

- [Maestro orchestrator](docs/MAESTRO_ORCHESTRATOR.md)
- [Agent runtime](docs/AGENT_RUNTIME.md)
- [Scheduler and queue](docs/SCHEDULER_QUEUE.md)
- [Memory service](docs/MEMORY_SERVICE.md)
- [Memory curator](docs/MEMORY_CURATOR.md)
- [Memory dropbox](docs/MEMORY_DROPBOX.md)
- [Postgres setup](docs/POSTGRES.md)
- [Phone access](docs/PHONE_ACCESS.md)
- [Codebase cleanup register](docs/CODEBASE_CLEANUP.md)
- [MVP backlog](docs/BACKLOG.md)

## Current Cleanup Priorities

The MVP backbone is functional, but a few files are intentionally still larger than ideal. The
cleanup register tracks the next refactors:

- Split `frontend/src/App.tsx` into component folders.
- Extract routed-object parsing from `app/memory/routed_service.py`.
- Split `app/tools/runtime.py` by tool family.
- Split orchestration execution, synthesis, routing, and artifact staging out of
  `app/maestro/orchestrator.py`.

See [docs/CODEBASE_CLEANUP.md](docs/CODEBASE_CLEANUP.md) before making broad structural changes.
