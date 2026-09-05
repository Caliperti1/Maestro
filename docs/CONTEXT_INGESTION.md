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

The filesystem dropbox, ChatGPT export importer, repository observer, sanitized work-context
adapter, and Google/GitHub tool evidence now terminate at the same envelope and ledger boundary.

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
| USMA | sanitized work context | user reviewed | external allowed |

USMA evidence is sanitized before it reaches Maestro, so it follows the same configured
curation and retrieval path as other domains. Its source classification remains attached for
provenance and future policy decisions. The generic `local_only` policy remains available for an
individual source that must never enter a cloud-bound prompt; when selected it is enforced during
curation and retrieval.

The standard dropbox path currently uses the configured cloud memory model (Terra by default) for
extraction and candidate evaluation. Canonical memories use the configured Ollama embedding model
(`nomic-embed-text` by default) for semantic retrieval. Model reasoning and embeddings are separate
steps: the model extracts and evaluates meaning, while the embedding encodes the final memory for
similarity search.

## Durable Truth And History

When a candidate supersedes an existing memory, the replacement is linked to the old item and the
old item receives `valid_until`. Active retrieval therefore returns current truth. Reports, run
logs, artifacts, and memory links retain the historical trail.

## Health And Recovery

The Memory UI shows registered sources, tracked records, duplicates, and failures. The API exposes:

- `GET /memory/ingestion/status`
- `GET /memory/ingestion/records`
- `POST /memory/ingestion/recover`
- `GET /memory/ingestion/context-mailbox/status`
- `POST /memory/ingestion/context-mailbox/poll`
- `POST /memory/imports/chatgpt`
- `POST /memory/ingestion/sanitized-context`
- `POST /memory/ingestion/sources/repositories`
- `POST /memory/ingestion/sources/repositories/{source_key}/observe`
- `POST /memory/hygiene/run`

The background dropbox worker recovers ingestion records and source files left in `processing` for
more than 30 minutes. Failed records can be retried by placing the same source version back in its
inbox.

## USMA Sanitized Context Drops

The safe initial pattern is to automate collection on each work machine but retain a human review
gate before transfer.

```text
Authorized work sources
  -> local collector
  -> allowlist and redaction
  -> maestro_context.md plus manifest
  -> human review
  -> approved transfer
  -> Maestro USMA inbox
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

## Implemented Adapters

1. ChatGPT export importer with stable conversation IDs, content versions, and incremental exports.
2. Local repository observer with full-baseline and commit-aware incremental state reports.
3. Reviewed USMA sanitized Markdown context manifests.
4. Gmail, Calendar, Drive, and GitHub tool-result evidence accounting.
5. Dedicated Gmail context mailbox intake for ChatGPT and approved cross-environment handoffs.

## Dedicated Context Mailbox

The dedicated mailbox is a transport adapter, not a second memory system:

```text
allowlisted sender -> Gmail intake -> Context Gateway -> domain inbox -> Memory Curator
```

Messages must use `[MAESTRO-CONTEXT][SOURCE][DOMAIN]` at the start of the subject and declare
`source_system`, `source_id`, `source_timestamp`, and `domain` near the top of the body. The stable
`source_id` identifies the source object; a hash of normalized content identifies its version.
Resending unchanged content is therefore harmless, while a corrected handoff is reconsidered.

The adapter preserves Gmail message/thread IDs, sender, source timestamps, raw message evidence,
attachment hashes, policy, and transfer method through canonical memory provenance. Supported
attachments are archived and text-extracted into the staged evidence. Gmail labels expose terminal
transport state:

- `Maestro/Processed`: staged successfully or recognized as an unchanged duplicate.
- `Maestro/Quarantine`: sender or handoff contract was not trusted.
- `Maestro/Failed`: a trusted handoff could not be staged and may be retried after repair.

Processed mail is archived and marked read. Quarantined or failed mail stays out of memory. The
background worker polls automatically, and the Memory Manager provides a manual **Check mailbox**
control plus health and counts. See `docs/CONTEXT_MAILBOX_SETUP.md` for configuration and format.

## Retrieval And Grounding

Ingestion alone does not ensure that Maestro understands Chris's world. Maestro now adds:

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
6. Policy-aware ranking and bounded context compression. LLM reranking remains optional if
   evaluation data shows it materially improves results.
7. Retrieval evaluations that verify identity grounding, domain expertise, cross-domain questions,
   current-truth selection, provenance, and restricted-data isolation.

The grounding packet should not replace retrieval. It establishes identity and role invariants;
retrieval supplies the changing domain expertise needed for the current message or work item.
