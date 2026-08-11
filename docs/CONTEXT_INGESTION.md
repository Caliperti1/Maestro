# Context Ingestion Architecture

## Purpose

The Context Gateway is the control plane between evidence sources and Maestro's existing memory
pipeline. It normalizes source identity, applies policy, prevents duplicate processing, tracks
checkpoints and health, and stages evidence. It does not decide what is true or durable.

The governing rule is:

> Sources provide evidence. Agents interpret evidence. The Memory Curator determines durable
> context. Reports preserve history. Memory represents Maestro's best understanding of current
> truth.

## Processing Flow

```text
Source adapter
  -> normalized context envelope
  -> ingestion ledger claim
  -> raw SeedPackage and Artifact
  -> extraction preview
  -> Memory Curator
  -> canonical memory / proposal / routed object
  -> processed source archive
```

The current filesystem dropbox is the first adapter. Future ChatGPT, Gmail, Google Drive, GitHub,
repository observer, and sanitized work-context adapters should terminate at the same envelope and
ledger boundary.

## Normalized Context Envelope

Every source object is normalized with:

- `source_registration_key`: stable configured source, such as `dropbox:usma`.
- `source_system`: producer, such as `manual_dropbox`, `chatgpt`, `gmail`, or `github`.
- `external_id`: stable object identity within that source.
- `source_version`: version, revision, or content hash.
- `content_hash`: SHA-256 of the normalized evidence.
- `content_type`: MIME-like content classification.
- `domain_key`: intended Maestro domain.
- `source_timestamp`: when the source evidence was produced or changed.
- `artifact_uri`: location of the staged evidence.
- `policy`: sensitivity, trust, transfer, retention, and egress rules.

Provenance fields are copied into the `SeedPackage`, `Artifact`, candidate metadata, memory source
references, proposals, and routed objects. This allows later retrieval and hygiene to reason about
source, time, trust, and policy.

## Idempotency And Checkpoints

The ingestion ledger uniquely identifies an object by:

```text
source registration + external object id + source version
```

An identical object version is not extracted twice. A changed version is processed again so the
curator can reinforce, supersede, conflict with, or ignore existing memory. Semantic deduplication
remains a Memory Manager responsibility; the gateway only prevents transport-level duplicates.

Pull-style adapters store cursors in `source_checkpoints`. Examples include Gmail history IDs,
GitHub commit SHAs, export timestamps, and pagination tokens.

## Policy And Egress

The initial policy profiles are:

| Domain | Sensitivity | Trust | Default egress |
| --- | --- | --- | --- |
| Personal | personal | user provided | external allowed |
| Praxis | business confidential | user provided | external allowed |
| Perti Laboratories | business confidential | user provided | external allowed |
| Maestro Development | business confidential | user provided | external allowed |
| USMA | sanitized work context | user reviewed | local only |
| L3 | sanitized work context | user reviewed | local only |

`local_only` is enforced twice:

1. The dropbox curator uses the configured local Ollama model and never falls back to a cloud model.
2. Memory and routed-object retrieval exclude the derived context from cloud-bound prompt bundles.

Human audit and local-model retrieval can still inspect it. A future policy engine can add finer
rules for summarization, artifact creation, specific agents, or approved external providers.

## Durable Truth And History

When a candidate supersedes an existing memory, the replacement is linked to the old item and the
old item receives `valid_until`. Active retrieval therefore returns current truth. Reports, run
logs, artifacts, and memory links retain the historical trail.

## Health And Recovery

The Memory UI shows registered sources, tracked records, duplicates, and failures. The API exposes:

- `GET /memory/ingestion/status`
- `GET /memory/ingestion/records`
- `POST /memory/ingestion/recover`

The background dropbox worker recovers ingestion records and source files left in `processing` for
more than 30 minutes. Failed records can be retried by placing the same source version back in its
inbox.

## USMA And L3 Sanitized Context Drops

The safe initial pattern is to automate collection on each work machine but retain a human review
gate before transfer.

```text
Authorized work sources
  -> local collector
  -> allowlist and redaction
  -> maestro_context.md plus manifest
  -> human review
  -> approved transfer
  -> Maestro USMA or L3 inbox
```

The exported context should describe obligations and user-relevant state, not reproduce source
repositories. Good initial sections are:

- Calendar obligations and preparation deadlines
- Chris's action items and due-outs
- Decisions Chris is allowed to retain
- Relationship notes that are useful and authorized
- Non-sensitive project status
- Open questions and blocked work
- Source timestamps and stable source IDs

Do not transfer classified information, CUI, export-controlled data, corporate proprietary data,
or full source documents unless the relevant authority explicitly permits it.

Start from `docs/SANITIZED_CONTEXT_DROP_TEMPLATE.md`. The first adapter can accept that reviewed
Markdown directly; a later exporter can generate the same fields plus a signed hash manifest.

See the transfer recommendations below before adding automated email or network delivery.

## Planned Adapters

1. ChatGPT export importer with stable conversation/message IDs and incremental exports.
2. Repository observer that produces current-state reports from commits, issues, PRs, tests, and
   documentation changes.
3. USMA and L3 sanitized Markdown/JSON context-drop adapters.
4. Existing Gmail, Calendar, Drive, and GitHub sources routed through this ledger rather than
   maintaining independent memory terminal paths.

## Retrieval And Grounding Follow-On

Ingestion alone does not ensure that Maestro understands Chris's world. The next memory program
should add:

1. An authoritative identity and ownership graph for Chris, aliases, roles, companies, employment,
   and domain relationships. Praxis and Perti Laboratories must be represented as Chris's companies,
   not generic organizations.
2. A small, cached grounding packet injected into every Maestro and agent prompt. It should identify
   Chris as the user, define Maestro's role, and include only stable high-value relationships.
3. A query and domain router that selects relevant domains and stores before retrieval.
4. Federated retrieval across durable memory, routed objects, reports, run logs, and source evidence,
   with temporal and relationship expansion.
5. Source-aware ranking using semantic similarity, current validity, domain fit, trust, source time,
   and relationship distance.
6. Optional local or cloud reranking and context compression, subject to the source egress policy.
7. Retrieval evaluations that verify identity grounding, domain expertise, cross-domain questions,
   current-truth selection, provenance, and restricted-data isolation.

The grounding packet should not replace retrieval. It establishes identity and role invariants;
retrieval supplies the changing domain expertise needed for the current message or work item.
