# Memory Service

The Memory Manager service is the code boundary for canonical memory retrieval and writes.
Agents and workflows should not write directly to `memory_items`; they should submit
candidate memories to this service so scope, approval policy, and provenance are handled
consistently.

Flow diagram: [04_memory_service_flow.mmd](04_memory_service_flow.mmd)
Curator details: [MEMORY_CURATOR.md](MEMORY_CURATOR.md)

## Normal Storage Flow

1. An action completes: message sent, artifact generated, report written, or tool called.
2. The raw action output is stored in domain-specific staging as an artifact or log source.
3. A Memory Curator agent parses the raw source and generates candidate memories.
4. The curator calls the Memory Manager service.
5. The service validates scope, source references, and impact level.
6. The service either writes canonical memory, creates an auditable auto-approved proposal,
   or queues a very-high-impact proposal for user approval.

There is also a raw staging inbox for files, links, notes, document exports, and old AI
conversations. Items dropped there should trigger the same curator-to-service workflow.
That path will support initial seeding once seed package ingestion is implemented.

## Curation Routing

The curator has two output lanes:

- durable memory candidates, which flow through `MemoryService` into `memory_items` or
  `memory_proposals`
- routed operational items, which flow into `routed_items`

This keeps agent run history, RFIs, due-outs, contacts, and calendar-like details from polluting
prompt memory retrieval.

Routed item categories:

- `task`: due-outs, action items, follow-ups, or work requests
- `human_input`: RFIs, approvals, missing answers, or decisions needed from Chris
- `event`: meetings, deadlines, reminders, or scheduled blocks
- `contact`: people, organizations, roles, relationship details, or contact facts
- `think_tank`: brainstorms and immature ideas
- `decision_log`: approvals, denials, decision records, and rationale
- `project`: initiatives that group work and artifacts
- `artifact_history`: raw run output, transcripts, reports, or tool results kept as provenance
- `integration_note`: non-secret notes about tool/integration setup
- `ignore`: duplicates or transient content that should not be written

All routed items are domain-scoped when the source comes from a domain dropbox. The Maestro
orchestrator can query across all domains; domain agents should only see operational items for
their own domain unless Maestro explicitly delegates cross-domain work.

Endpoint:

```text
GET /memory/routed-items?domain_key=praxis&route_type=human_input&status=open
```

The extractor is aware of this downstream router. It should split source material into atomic
durable memories and separate routed items instead of embedding RFIs, tasks, contacts, or events
inside broad memory summaries.

## Retrieval Flow

The retrieval side is the service other agents and Maestro call when they need context.

- Domain agents can retrieve global memory, their own domain memory, and their own agent memory.
- Domain agents do not retrieve unrelated domain or unrelated agent memory.
- Maestro can retrieve global memory, Maestro session memory, and cross-domain memory.
- Retrieval supports query text, importance, memory-type, domain, and audience filters.
- Returned memories include score reasons, source provenance, artifact references, and visible
  one-hop memory links.

The MVP retrieval service stays in Postgres. It ranks with deterministic structured signals:
importance, recency, impact level, domain/agent match, global context, and lightweight query
relevance from lexical overlap with the task query. This is intentionally explainable so we can
debug early agent behavior. A later semantic retrieval story should add pgvector embeddings and
blend vector similarity into the same result payload.

Importance and query relevance are separate:

- `importance`: durable value assigned when memory is written.
- `query_relevance`: task-specific lexical match for this retrieval request.
- `score`: blended rank used to order returned context.

When `query_text` is present, retrieval has three modes:

- `balanced` default: filters zero-match noise unless the memory is exceptional global/session
  context.
- `strict`: requires stronger query relevance for focused task prompts.
- `broad`: keeps all visible memories for exploratory/debug retrieval while still ranking
  query matches first.

Semantic retrieval adds embeddings to the same pipeline. Each canonical memory gets a vector
representation in `memory_embeddings`; query text gets embedded at retrieval time; semantic
similarity is blended into the final score after visibility filtering. This lets Maestro find
memories that are meaningfully related even when they do not share exact words.

Local MVP default:

- `EMBEDDING_PROVIDER=ollama`
- `EMBEDDING_MODEL=nomic-embed-text`
- `EMBEDDING_BASE_URL=http://localhost:11434`

Install the local embedding model:

```bash
ollama pull nomic-embed-text
```

Backfill existing canonical memory:

```bash
python -m app.memory.embed backfill
```

Check embedding coverage:

```bash
python -m app.memory.embed status
```

API:

```text
GET /memory/retrieve?audience=maestro&domain_key=praxis&query_text=tactical+innovation&mode=balanced&use_semantic=true&limit=8
```

Primary response shape:

- `total_visible`: count before ranking/limit.
- `filtered_count`: count removed by the retrieval mode.
- `semantic_status`: whether semantic retrieval was enabled, disabled, unavailable, or failed.
- `results[].score`: deterministic ranking score.
- `results[].query_relevance`: query-specific lexical relevance before semantic retrieval exists.
- `results[].semantic_similarity`: vector similarity when embeddings are available.
- `results[].score_reasons`: explainable factors behind the score.
- `results[].provenance`: source refs, seed package, artifact, and processed path.
- `results[].links`: visible one-hop linked memories and relation types.

## Context Bundles

Agents should prefer context bundles over raw retrieval rows. A context bundle is the clean,
schematized retrieval artifact that a future prompt aggregator can combine with global system
context, domain context, role prompts, and tool access.

The bundle builder uses the same `MemoryRetrievalService` visibility, semantic scoring,
provenance, and link logic, then packages selected memories into stable sections:

- `global`: shared Maestro memory.
- `maestro_session`: current session memory for Maestro-level workflows.
- `domain`: memory for the requested operating domain.
- `agent`: memory for the requested domain agent.

The current profiles are:

- `agent_prompt`: default context package for a domain agent task.
- `daily_standup`: broader package for cross-domain standup synthesis.
- `direct_user_question`: Maestro/user-facing retrieval without agent-private memory by default.
- `curator_context`: memory curator support for duplicate checks and source interpretation.
- `memory_debug`: broad developer-facing bundle for inspection.

API:

```text
GET /memory/context-bundle?profile=agent_prompt&audience=agent&domain_key=praxis&query_text=partner+call&max_items=12&max_chars=4000
```

Primary response shape:

- `sections[]`: grouped memory snippets with IDs, scores, provenance, links, and excerpts.
- `rendered_text`: prompt-ready text block for early agents and debugging.
- `total_visible`, `filtered_count`, `retrieved_count`, `included_count`, `dropped_count`:
  explain how much memory was available and how much fit inside the bundle budget.
- `used_chars` and `max_chars`: approximate prompt budget accounting.
- `retrieval_query`: the profile-expanded retrieval settings used to produce the bundle.

The bundle is intentionally a boundary object, not a final prompt. The future prompt aggregator
should treat it as one input alongside global operating instructions, domain instructions, role
instructions, tool manifests, task payloads, and response-format requirements.

## Impact Policy

- `low`: write directly to canonical memory.
- `medium`: create an approved proposal for audit and write canonical memory.
- `high`: create an approved proposal for audit and write canonical memory.
- `very_high`: create a `pending_user_approval` proposal and do not write canonical memory
  until approved.

This keeps routine memory friction low while preserving review for changes that could
materially alter Maestro behavior, permissions, user preferences, external commitments, or
durable strategy.

## Scopes

- `global`: shared memory available across Maestro.
- `maestro_session`: session-level memory used by Maestro orchestration.
- `domain`: memory scoped to one operating domain.
- `agent`: memory scoped to one agent inside one domain.

Global memory cannot be tied to a domain or agent. Domain memory requires a domain.
Agent memory requires both domain and agent.

## Session Memory

Most work in this sprint has focused on durable memory: facts, preferences, decisions, source
summaries, workflows, and domain knowledge that should survive across sessions. Maestro also
needs session memory, but it has a different job.

Session memory should capture short-lived orchestration context such as:

- what the user asked Maestro to do in the current session
- which agents were tasked and why
- tool calls, artifacts, and external actions completed during the session
- temporary assumptions, open questions, blockers, and next steps
- brief summaries of what changed since the last user-visible response

The intended flow is that an interaction artifact packager summarizes session activity into a
structured artifact, stages that artifact, and lets the same curator/service pipeline decide
what becomes durable memory. Transient context can remain `maestro_session`; durable lessons,
preferences, decisions, or domain facts should be promoted into `global`, `domain`, or `agent`
memory through normal candidate evaluation.

The prompt aggregator should retrieve session memory through context bundles when the caller is
Maestro-level or when an agent task needs recent orchestration context. Domain agents should
still only receive session memory that the aggregator intentionally includes for their task.

## What This Issue Implements

- `MemoryService.write_candidate`
- direct low-impact canonical writes
- auto-approved auditable medium/high-impact writes
- very-high-impact approval queue
- proposal approval and rejection
- proposal listing filters
- scoped retrieval for agents
- cross-domain retrieval for Maestro
- `MemoryRetrievalService`
- `/memory/retrieve` debug/API endpoint
- `/memory/context-bundle` agent context endpoint
- `/memory/routed-items` operational routing endpoint
- Memory tab retrieval debugger
- semantic retrieval with local-first embeddings
- provenance and one-hop link context in retrieval payloads

## Follow-On Stories

### Memory Curator Agent

The deterministic Memory Curator currently reads staged text with explicit memory markers,
extracts durable candidate memories, classifies impact from those markers, and calls the
Memory Manager service.

Issue #16 adds the reusable LLM client and replaces marker-only extraction with an LLM-enabled
curator path while preserving the same `MemoryCandidate` and `MemoryService` contract.

### Staging Ingestion

Add domain staging and raw inbox ingestion so files, links, notes, and exports can become
artifacts, then curator inputs.

### Seed Package Processing

Implement batch processing over existing documents and old AI conversations. Seed packages
should enter raw staging, produce artifacts, then run through the same curator workflow as
live action output.

### Memory Hygiene

Memory hygiene should run through the same proposal lifecycle rather than editing canonical
memory silently. Hygiene jobs should eventually:

- detect duplicates and near-duplicates
- suggest merges
- identify stale or contradicted memory
- lower importance for decayed items
- archive obsolete items
- propose corrections when new evidence conflicts with old memory
- surface very-high-impact changes for user approval

### Approval UI

Add a user-facing queue for `pending_user_approval` proposals with source references,
rationale, approve/reject actions, and later edit-before-approve behavior.
