# Behavior Test 005: Contact Intelligence

## Purpose

Prove that Maestro can identify a person from partial context, show the supporting evidence, update
an existing contact without duplication, and preserve domain boundaries.

## Setup

Use two Praxis emails or routed test messages about the same person. The first should include a full
name, email, organization, and role. The second should use a short alias and refer to a distinctive
discussion topic. Include another contact at the same organization as a negative control.

## Human Tests

1. Ask Maestro: `Who at Example Corp did we discuss invoice automation with last week?`
2. Ask: `Show me why you think that is Jane and the last interaction we had with her.`
3. Add another routed observation using `Jane S` plus the same email or phone.
4. Search in the Contacts tab for `invoice automation Example Corp`.
5. Repeat the same source message through the route pipeline.
6. As an agent in another domain, search for the Praxis-only interaction.

## Test Matrix

| Step | Expected behavior | Evidence |
| --- | --- | --- |
| 5.1 Resolve | Maestro returns the correct contact rather than only matching literal name text. | Match reasons include organization, interaction, lexical, or semantic evidence. |
| 5.2 Explain | The response cites the relevant interaction summary, timestamp, organization, and source. | Contact detail and provenance. |
| 5.3 Update | Exact email/phone/LinkedIn evidence updates the existing canonical contact despite a name variant. | Stable contact ID and a new interaction row. |
| 5.4 Inspect | The Contacts UI shows aliases, organization/role, interaction timeline, and relationships. | Contact detail panel. |
| 5.5 Idempotency | Reprocessing one routed source does not create a duplicate interaction or contact. | Stable counts and IDs. |
| 5.6 Scope | A domain agent cannot retrieve an interaction that is only visible in another domain. | Empty or unrelated tool result. |
| 5.7 Ambiguity | A first name shared by multiple contacts does not trigger an automatic merge. | Clarification/review instead of corrupted contact. |
| 5.8 Manual merge | Selecting a confirmed duplicate requires approval and preserves aliases, interactions, affiliations, and provenance on the survivor. | One canonical record after merge. |

## Pass Criteria

- Contextual questions return the right person with inspectable evidence.
- Identity updates use strong evidence and do not over-merge.
- Interactions and affiliations remain domain-aware while the identity is global.
- Agent retrieval is domain-scoped; Maestro can aggregate across domains.
- Every learned fact remains traceable to a source interaction.
