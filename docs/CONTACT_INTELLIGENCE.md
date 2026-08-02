# Contact Intelligence

Contact intelligence is Maestro's canonical, queryable representation of people and the evidence
behind what the system knows about them. It is routed operational data, not durable RAG memory.

## Data Model

- `contacts`: global identity and stable profile fields.
- `contact_aliases`: names, nicknames, and source/manual aliases used for identity resolution.
- `contact_domain_notes`: durable domain-specific context about a person.
- `contact_interactions`: timestamped email, meeting, call, chat, or mention evidence with provenance.
- `contact_organization_affiliations`: a person's role and relationship with an organization,
  optionally scoped to a domain.
- `contact_relationships`: typed person-to-person links with confidence and supporting sources.
- `contact_embeddings`: local semantic representation of the assembled profile for contextual search.

Postgres remains the source of truth. Relationships are represented relationally so they can later
be projected into a graph view without introducing a second canonical database.

## Write Path

1. An agent uses the Contact Manager skill and emits a provenance-rich `contact` candidate through
   `routed.item.create`.
2. Exact email, phone, and LinkedIn matches are evaluated before aliases, name/organization context,
   lexical evidence, and the ambiguity resolver.
3. The canonical contact is created or updated.
4. The source becomes an idempotent `contact_interaction`; organization and relationship evidence
   becomes first-class linked records.
5. The assembled contact profile is embedded best-effort through the configured embedding gateway.

Agents do not manually choose create versus update. Ambiguous identity must remain unresolved rather
than silently merging two people. Canonical contact merges are user-approved operations.

## Retrieval

`ContactIntelligenceService` combines:

- exact identity and alias matches;
- organization, role, and person-relationship matches;
- recent interaction and domain context;
- lexical overlap across the profile and evidence timeline;
- semantic similarity when a current contact embedding exists.

Every result includes a score, match reasons, affiliations, relevant interactions, relationships,
and source provenance. `contacts.search` and `contacts.get` enforce the calling agent's domain.
Maestro receives the same richer contact payload through the routed-context assembler and can see
all domains.

## Tools And UI

- `contacts.search`: safe domain-scoped contextual retrieval.
- `contacts.get`: safe domain-scoped contact detail.
- `contacts.update`: low-impact canonical field correction.
- `contacts.merge`: destructive duplicate merge; always approval-gated.

The Contacts UI supports contextual search, domain filtering, profile edits, aliases, organization
affiliations, interaction history, person relationships, provenance inspection, and manual merge.
Existing profiles can be embedded with `POST /memory/routed-objects/contacts/embeddings/backfill`;
unchanged profiles are skipped by source hash.

## Remaining Work

- Run staged historical Gmail/contact imports in shadow mode and review resolver decisions before
  promoting the full corpus.
- Add a dedicated graph visualization over existing affiliation and relationship records.
- Add review queues for ambiguous identity decisions and stale/conflicting profile fields.
- Add periodic profile-conflict review and embedding freshness reporting to routed hygiene.
