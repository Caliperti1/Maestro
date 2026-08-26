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
- `organization_aliases` and `organization_embeddings`: organization identity and semantic retrieval.
- `contact_hydration_jobs` and `contact_hydration_candidates`: resumable historical import state and
  its shadow-review ledger for both people and organizations.

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

Calendar attendance is supporting evidence, not automatically a relationship. Events with no more
than `CALENDAR_CONTACT_AUTO_PROMOTE_ATTENDEE_LIMIT` attendees may promote unknown people because a
small working session is meaningful evidence of likely engagement. Larger rosters retain every
source attendee on the event as `roster_only`, link people who are already known, and create neither
new contacts nor contact interactions. Direct correspondence, explicit extraction, or a later small
meeting can still promote an attendee through the normal contact resolver.

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

Organizations use the same operating pattern: contextual and semantic search, aliases, domain notes,
linked people, interaction history, provenance, profile edits, and approval-gated duplicate merge.
Agents receive `organizations.search`, `organizations.get`, `organizations.update`, and
`organizations.merge` through the same domain-scoped tool contract as contacts.

Contact and organization aliases are evidence records, not generated search shortcuts. Message
headers, signatures, explicit manual edits, domain identifiers, and confirmed duplicate merges may
create aliases; speculative initials, nicknames, legal suffixes, and acronyms do not persist. A
merge retains the duplicate record's prior canonical name as an alias of the survivor.

## Historical Hydration

Historical Gmail hydration is a dedicated background job rather than a Maestro workflow. One run:

1. pages Gmail metadata using a bounded Gmail query;
2. groups participants by exact email and organizations by non-personal email domain;
3. filters Chris and automated senders;
4. optionally enriches representative threads with local Qwen and a capped Terra fallback;
5. pauses in shadow review without changing canonical records;
6. promotes approved contact and organization candidates through the existing routed write service;
7. runs hygiene and embedding backfill after promotion.

See `docs/CONTACT_HYDRATION.md` for controls, lifecycle, and testing.

## Remaining Work

- Add a dedicated graph visualization over existing affiliation and relationship records.
- Add review queues for ambiguous identity decisions and stale/conflicting profile fields.
- Add periodic profile-conflict review and embedding freshness reporting to routed hygiene.
