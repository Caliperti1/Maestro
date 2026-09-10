# Identity Grounding

Identity grounding keeps a few user-confirmed facts available before probabilistic retrieval. It
prevents ordinary RAG ranking from omitting foundational context such as who the user is, which
organizations belong to him, and what first-person references mean.

## Storage

`identity_nodes` stores people, organizations, domains, and the Maestro system. Nodes can point to
the canonical domain or organization record. `identity_relationships` stores current, sourced
relationships such as `assistant_to`, `owns`, `founded`, `professional_context`, and `represents`.

The initial seed records:

- Chris Aliperti is Maestro's user and principal.
- Chris's configured names and email addresses are aliases of that one principal, never separate
  CRM contacts.
- Maestro is Chris's system-level assistant.
- Praxis Defense is Chris's company.
- Chris started Perti Laboratories.
- USMA is a professional and academic context connected to Chris, without inventing a job title.

Seeding is additive. It creates missing records and canonical organization links but does not
overwrite later user edits. Verified owner aliases come from `USER_NAME_ALIASES` and
`USER_EMAIL_ALIASES`; startup adds newly configured aliases to the authoritative node without
discarding aliases already stored there.

## Prompt Use

`IdentityGroundingService` renders a compact packet. Maestro receives the full cross-domain packet.
A subordinate agent receives the user/Maestro anchors plus relationships for its own domain. The
packet is placed before retrieved memory in:

- Maestro context bundles used for chat and planning.
- Subordinate agent prompt packages and tool-planning prompts.
- Memory extraction prompts.

Packets are cached briefly per process and can be inspected through:

```text
GET /maestro/identity-graph
GET /maestro/identity-graph?domain_key=praxis
```

This graph is authoritative grounding, not a replacement for durable memory. Detailed project and
relationship knowledge still belongs in memory, routed objects, reports, and source evidence.
Contact ingestion, event attendee linking, and routed hygiene all use the same owner identity.
Hygiene archives any legacy owner contact and preserves the underlying attendee/source evidence as
an explicit Maestro-user identity instead.
