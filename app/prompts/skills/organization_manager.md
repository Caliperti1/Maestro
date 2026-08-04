## Purpose
Maintain trustworthy, queryable intelligence about organizations and Maestro's history with them.
Organizations are domain-agnostic identities with domain-scoped context, affiliated people, and interaction evidence.

## Required Tools
- `organizations.search`: resolve an organization by name, alias, website, person, interaction, or contextual similarity.
- `organizations.get`: inspect the selected organization's full profile and evidence.
- `routed.item.create`: create a provenance-backed organization candidate for canonical promotion.
- `organizations.update`: correct low-impact canonical fields only after identity is clear.
- `organizations.merge`: propose a duplicate merge only when separate records clearly represent one organization.

## Use When
- A company, agency, military unit, vendor, partner, school, lab, or institution has durable relevance.
- A contact affiliation, interaction, opportunity, or relationship adds useful organizational context.
- Chris asks who Maestro knows at an organization or what prior work and conversations involved it.

## Identity Resolution
1. Search before answering or updating when the organization may already exist.
2. Prefer exact evidence in this order: verified website/email domain, canonical name plus known contacts, then aliases.
3. Treat abbreviations and similarly named units as ambiguous unless evidence connects them.
4. If two plausible matches remain, do not guess or merge. Request clarification.
5. Preserve former names and common abbreviations as aliases.

## Candidate Construction
Use the canonical organization name as `title`. Include `entity_name`, `website`, `email_domain`,
`aliases`, `summary`, `relationship_context`, and known contact affiliations when supported. Content
should explain why the organization matters in the current domain. Preserve message, thread, report,
artifact, or event source references and timestamps. Never invent a legal name, website, or relationship.

## Historical Hydration
- Historical Gmail hydration creates organization candidates alongside contacts in the same background job.
- A non-personal email domain is identity evidence, not proof of a polished organization name or relationship.
- Keep inferred organizations in shadow review until Chris approves them.
- Link promoted contacts and their email interactions to the approved organization; do not create a second import run.

## Output Contract
For new evidence, call `routed.item.create` with route_type `entity`, the organization name as `title`,
a human-readable relevance summary as `content`, structured metadata, and source refs. The routed
resolver owns canonical create-versus-update adjudication. Use `organizations.update` only for an
explicit correction to an already resolved organization.

## Validation
- The title is an organization name, not an action phrase or generic noun.
- At least one identity or contextual resolution signal is present.
- People, domain context, interactions, aliases, and provenance remain distinct fields.
- Ambiguous records are not silently merged.
