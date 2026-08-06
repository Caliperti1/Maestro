# Behavioral Test 006: Historical Contact And Organization Hydration

## Goal

Seed trustworthy Praxis contact and organization intelligence from a bounded historical Gmail sample
without creating a Maestro workflow or writing unreviewed candidates to canonical routed stores.

## Preconditions

- Backend and frontend are running from current `main`.
- Praxis Google connection is active.
- A Praxis agent has `gmail.message.search` and `gmail.thread.get` permission.
- Local Ollama Qwen is available if enrichment is enabled.

## Test A: Shadow Scan

1. Open **Memory > Contacts** and expand **Historical Gmail import**.
2. Select Praxis, enter `in:sent newer_than:30d`, set 50 messages and 25 contacts.
3. Keep local enrichment on and Terra fallback off, then start the import.

Expected:

- The job advances through scanning and enriching without occupying Maestro's workflow queue.
- Progress updates every few seconds and reaches `review`.
- No candidate becomes a canonical contact before approval.
- Chris Aliperti, the configured self email, no-reply senders, and automated accounts are absent.
- Header-derived candidate names and emails remain unchanged after LLM enrichment; another thread
  participant, including Chris, cannot replace the candidate identity.
- Address-only participants receive a readable email-derived name, with the address retained in the
  email field. Direct signature evidence may refine only that placeholder.
- Contact and organization aliases are retained only when observed or unambiguously derived from
  supplied interaction evidence. Confirmed duplicate merges preserve the retired canonical name.
- Enrichment receives compact global and Praxis context and describes relationships from Chris's
  perspective without creating Chris as a CRM record.
- Every candidate shows identity, action, status, confidence, and evidence-backed summary.

## Test B: Organization Parity

1. Open **Memory > Organizations** and expand the same import.
2. Inspect inferred organization candidates.

Expected:

- The same job is visible; no second scan is required.
- Non-personal participant domains create organization candidates.
- Personal email domains do not create organizations.
- Existing organizations are proposed as updates and new organizations as creates.

## Test C: Review And Promotion

1. Reject one irrelevant candidate.
2. Approve one contact and its organization individually.
3. Optionally approve the remaining candidates at 80% confidence.

Expected:

- Approved rows advance through promotion in the background.
- Rejected rows remain in the audit ledger and never become canonical records.
- The contact appears in Contacts with Gmail provenance, interaction history, and organization link.
- The organization appears in Organizations with alias, domain context, linked person, and interaction.
- Same-name/different-email candidates are not silently merged.

## Test D: Pause And Resume

1. Start a larger bounded import.
2. Pause it while scanning or enriching, wait 15 seconds, then resume.

Expected:

- Counters stop while paused.
- Resume continues from the stored Gmail page token or next enrichment candidate.
- Previously discovered candidates are not duplicated.
