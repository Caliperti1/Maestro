## Purpose
Maintain trustworthy, queryable intelligence about real people and Maestro's history with them.
Create evidence-rich contact candidates; use contact retrieval before relying on a name from memory.

## Required Tools
- `contacts.search`: resolve a person by name, alias, organization, relationship, or prior interaction.
- `contacts.get`: inspect the selected person's full profile and evidence.
- `routed.item.create`: create a provenance-backed contact candidate for canonical promotion.
- `contacts.update`: correct low-impact canonical fields only after identity is clear.
- `contacts.merge`: propose a duplicate merge only when separate records clearly represent one person.

## Use When
- A real person appears with useful identity, role, affiliation, relationship, preference, or contact information.
- An interaction changes what Maestro knows about a person or creates useful relationship history.
- Chris asks who someone is, who works at an organization, who knows another person, or who discussed a subject.

## Do Not Use When
- The subject is an organization, team, project, or unassigned role rather than a person.
- The only statement is an instruction for an agent to do work; that is not contact intelligence.
- The person is Chris Aliperti. Chris is Maestro's owner, not a CRM contact.
- The source provides no person and no resolvable identity evidence.

## Identity Resolution
1. Search before answering or updating when the person may already exist.
2. Prefer exact evidence in this order: email, phone, LinkedIn URL, full name plus organization.
3. Treat first names, initials, nicknames, and role-only references as ambiguous. Use organization,
   domain, nearby participants, and recent interactions to disambiguate.
4. If two plausible matches remain, do not guess. Explain the candidates and request clarification.
5. Never merge records solely because names are similar. A merge requires shared strong identity
   evidence or explicit confirmation from Chris.

## Candidate Construction
Use the person's best supported display name as `title`. Put a concise, durable description in
`content`, not an instruction such as "record this contact." Include these metadata fields when known:

- `name`, `email`, `phone`, `linkedin`, `aliases`
- `organization`, `role`, `affiliation_type`
- `summary`, `origination`, `relationship_context`
- `interaction_type`, `channel`, `direction`, `occurred_at` or `last_contact_at`
- related people and the evidence-backed relationship description

Always include source references with message, thread, report, artifact, or event identifiers. Preserve
the source's timestamp. Do not invent missing identity, title, employer, relationship, or interaction data.

## Interaction Rules
- A new email, meeting, call, introduction, or material mention is an interaction even when the contact exists.
- Summarize what was discussed, promised, learned, or changed in one evidence-grounded paragraph.
- Record domain context so an agent sees only interactions relevant to its own domain.
- Contact summaries describe the person; interaction records describe what happened. Do not conflate them.

## Historical Hydration
- Historical Gmail hydration is a dedicated background import, not an agent workflow and not a sequence of `routed.item.create` calls.
- Import participants by exact email identity first. A matching display name with a different email is ambiguous and must remain in review.
- Exclude Chris Aliperti, automated senders, notification accounts, and role inboxes.
- Infer organizations from non-personal email domains, then refine them only from representative source threads.
- Keep the import in shadow mode until Chris approves candidates. Do not claim candidates are canonical contacts before promotion completes.
- One hydration run creates both contact and organization candidates; do not schedule a second organization-only pass over the same corpus.

## Output Contract
For new evidence, call `routed.item.create` with `route_type: contact`, the person name as `title`, a
human-readable contact/interaction summary as `content`, the structured metadata above, and source refs.
The routed resolver owns canonical create-versus-update adjudication. Use `contacts.update` only for an
explicit correction to an already resolved contact.

## Validation
- The title is a person's name, never "record contact," "partner lead," or another action phrase.
- At least one identity or contextual resolution signal is present.
- The interaction timestamp and provenance are retained when available.
- Organization and role are separate fields.
- Uncertainty is explicit; ambiguous records are not silently merged.
