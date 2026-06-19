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

## Retrieval Flow

The retrieval side is the service other agents and Maestro call when they need context.

- Domain agents can retrieve global memory, their own domain memory, and their own agent memory.
- Domain agents do not retrieve unrelated domain or unrelated agent memory.
- Maestro can retrieve global memory, Maestro session memory, and cross-domain memory.
- Retrieval supports query text, importance, memory-type, domain, and audience filters.
- Returned memories include score reasons, source provenance, artifact references, and visible
  one-hop memory links.

The MVP retrieval service stays in Postgres. It ranks with deterministic structured signals:
importance, recency, impact level, domain/agent match, global context, and lightweight lexical
overlap with the task query. This is intentionally explainable so we can debug early agent
behavior. A later semantic retrieval story should add pgvector embeddings and blend vector
similarity into the same result payload.

API:

```text
GET /memory/retrieve?audience=maestro&domain_key=praxis&query_text=tactical+innovation&limit=8
```

Primary response shape:

- `total_visible`: count before ranking/limit.
- `results[].score`: deterministic ranking score.
- `results[].score_reasons`: explainable factors behind the score.
- `results[].provenance`: source refs, seed package, artifact, and processed path.
- `results[].links`: visible one-hop linked memories and relation types.

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
- Memory tab retrieval debugger
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
