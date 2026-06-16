# Memory Curator

The Memory Curator is the component that reviews staged sources, extracts candidate memories,
and submits them to the Memory Manager service. The current implementation is deterministic by
design so the project can test the plumbing, queue behavior, and provenance before adding LLM
calls.

## Current Deterministic Curator

`MemoryCurator` accepts `StagedMemorySource` inputs with:

- `source_type`: `message`, `artifact`, `tool_call`, `report`, `seed_package`, or `raw_note`
- optional source, domain, agent, task, and report identifiers
- raw text content
- optional URI and metadata

It extracts only explicitly marked lines. Unmarked text is ignored.

## Marker Format

```text
MARKER [optional_scope]: Optional title - Durable memory content
```

Supported markers:

- `MEMORY`: low-impact fact
- `FACT`: low-impact fact
- `PREFERENCE`: low-impact preference
- `DECISION`: medium-impact decision
- `SUMMARY`: medium-impact summary
- `INSTRUCTION`: high-impact standing instruction
- `VERY_HIGH`: very-high-impact standing instruction requiring approval

Supported scopes:

- `[global]`
- `[session]` or `[maestro_session]`
- `[domain]`
- `[agent]`

If no scope is provided, the curator defaults to `[domain]`.

Example:

```text
PREFERENCE: Standup format - Chris wants thin daily standup output first.
DECISION [global]: Product bet - Memory is the core Maestro product bet.
INSTRUCTION [agent]: Curator behavior - Preserve source refs on every candidate.
VERY_HIGH: Approval policy - Let Maestro change external commitments without approval.
```

## Processing Behavior

For every extracted candidate, the curator calls `MemoryService.write_candidate`.

- Exact normalized duplicates are skipped before any LLM evaluation.
- When configured, semantic evaluation compares candidates against nearby existing memories.
- Candidates may become new canonical memory, reinforce existing memory, supersede old memory,
  become conflict proposals for review, or be rejected.
- Low-impact accepted candidates become canonical memory immediately.
- Medium/high-impact accepted candidates become approved audit proposals and canonical memory.
- Very-high-impact candidates and conflicts become `pending_user_approval` proposals.

The curator also exposes helpers to list, approve, and reject pending approval proposals. These
helpers delegate to `MemoryService`; the curator does not bypass the service policy boundary.

## Semantic Dedupe And Merge

The live LLM dropbox path wires a `LLMMemoryEvaluator` into `MemoryService`. For each candidate,
the service first looks for deterministic exact duplicates in the same memory lane. If none is
found, the evaluator can return:

- `write_new`: persist the candidate through the normal impact gates
- `duplicate`: skip the candidate
- `reinforce`: append provenance/evidence to the existing memory metadata
- `supersede`: write a new memory and link it to the old memory with `supersedes`
- `conflict`: create a pending approval proposal tied to the related memory
- `reject`: drop vague or non-durable candidates

These decisions are written into preview results so the Memory tab can show what happened to
each candidate.

## Provenance

Every extracted candidate includes a `source_refs` entry with:

- source type
- source id, when provided
- source URI, when provided
- line number in the staged source

This preserves the link from canonical memory or proposals back to the staged source that caused
the memory write.

## Next Story: LLM Curator

Issue #16 adds the reusable LLM integration all agents should share, then uses it to build the
LLM-enabled Memory Curator. It preserves the same output contract:

1. staged source in
2. validated `MemoryCandidate` objects out
3. all writes routed through `MemoryService`
4. mocked model responses in tests

## LLM Dropbox Path

The LLM curator is used by the memory dropbox pipeline documented in
[MEMORY_DROPBOX.md](MEMORY_DROPBOX.md). That pipeline reads files from domain inboxes, calls the
LLM curator, writes a debug preview, routes candidates through `MemoryService`, and moves raw
files into `processed` or `failed`.
