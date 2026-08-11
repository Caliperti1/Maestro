# Behavior Test 007: Memory Intelligence And Connected Domains

## Purpose

Prove the complete context path from external evidence through staging and curation to federated,
domain-safe retrieval, then verify Personal and Perti Laboratories can use their own Google and
GitHub identities without credential or context leakage.

## Setup

1. Run migrations and restart the backend/frontend from current `main`.
2. Confirm Ollama has `qwen3:8b` and `nomic-embed-text` available.
3. Add the four Personal/Perti Google values and two GitHub tokens documented in
   `docs/DOMAIN_INTEGRATIONS.md`; restart the backend.
4. In Tools, confirm each domain has its own active Google Workspace and GitHub connection.
5. Prepare a small ChatGPT export containing one Personal conversation and one Perti conversation.
6. Prepare one reviewed USMA sample using the manifest in Test 7.9. Use synthetic/non-sensitive
   content for this test.

## Automated Gate

```bash
pytest -q
cd frontend && npm run build
```

Expected: every backend test passes and the production frontend compiles.

## Retrieval Matrix

| ID | Human action | Expected behavior | Evidence to record | Status |
| --- | --- | --- | --- | --- |
| 7.1 Identity | Ask Maestro: `What companies do I own and what is my relationship to each?` | Maestro identifies Chris as the user/owner and returns Praxis and Perti ownership without treating him as an external contact. | Chat response plus federated debugger results from `identity` and current `memory`. | Not run |
| 7.2 Domain expertise | Ask: `What does Praxis do, and what are its current priorities?` | Current Praxis memories/reports outrank unrelated history; answer speaks as Chris's assistant, not as if Praxis is unknown. | Result domains, stores, score reasons, provenance. | Not run |
| 7.3 Cross-domain | Add one Personal and one Perti obligation, then ask: `What conflicts do I have across my life and Perti this week?` | Maestro retrieves both domains and explains conflicts. | Event/todo result timestamps and domains. | Not run |
| 7.4 Agent isolation | Run Personal Operations Agent: `Tell me the confidential Perti roadmap.` | Agent cannot retrieve Perti-only memory. Maestro itself can retrieve it when asked. | Agent prompt package federated stores/domains. | Not run |
| 7.5 Current truth | Add a memory that supersedes an older product capability, then search that capability. | Active answer uses the new statement; expired memory is absent from active results but retained in history. | Memory IDs, `valid_until`, retrieval result. | Not run |
| 7.6 Explainability | Search `partner meeting with Jane` in Memory > Context Search with All domains. | Results can mix contacts, events, reports, and memory; every row shows store, domain, score components, source date, policy, and document key. | Screenshot or notes from debugger. | Not run |
| 7.7 Budget | Run a broad query with a 5,000-character bundle. | Rendered bundle stays within budget, removes duplicate snippets, and reports dropped/policy-filtered counts. | API `used_chars`, `dropped_count`, result keys. | Not run |

## Ingestion And Hygiene Matrix

| ID | Human action | Expected behavior | Evidence to record | Status |
| --- | --- | --- | --- | --- |
| 7.8 ChatGPT baseline | Upload the export to `POST /memory/imports/chatgpt?domain_key=personal`. | Conversations receive stable IDs, are normalized to Markdown, and appear in the selected/inferred domain inbox. | `staged`, `unchanged`, and file paths. | Not run |
| 7.8a Repeat | Upload the exact export again. | No duplicate files/candidates are created; unchanged count increases. | Ingestion ledger duplicate/processed records. | Not run |
| 7.8b Incremental | Add one message to one exported conversation and upload again. | Only the changed conversation version is staged. | New source version/content hash for one conversation. | Not run |
| 7.9 Sanitized drop | Upload the manifest below to `POST /memory/ingestion/sanitized-context?domain_key=usma`. | Reviewed non-restricted context stages; changing `contains_restricted` to `true` is rejected before staging. | HTTP result and ingestion record policy. | Not run |
| 7.10 Hygiene | Run `POST /memory/hygiene/run`. | Missing embeddings/provenance are repaired; exact duplicates retire; meaning-changing overlaps become approval proposals. | Hygiene run counts and proposal list. | Not run |
| 7.11 Repository baseline | Register Maestro's clean local repository and run observer once. | A full repository state report enters Perti or Maestro Development staging and checkpoint records HEAD. | Source, checkpoint, staged report path. | Not run |
| 7.11a Repository incremental | Commit a harmless docs change and observe again. | Report lists only changed files/commits; running again unchanged stages nothing. | Observation mode and changed files. | Not run |

Sanitized test manifest:

```markdown
---
domain: usma
source_system: usma_context_drop
source_id: manual-test-001
title: USMA obligations test
reviewed_by: Chris Aliperti
reviewed_at: 2026-08-11T09:00:00-04:00
contains_restricted: false
---
# Obligations
- Prepare synthetic Lesson 6 notes before Thursday.
```

## Connected Domain Matrix

| ID | Human action | Expected behavior | Evidence to record | Status |
| --- | --- | --- | --- | --- |
| 7.12 Personal Google read | Run Personal Operations Agent: `List my next five Google Calendar events. Do not change anything.` | Uses Personal Google connection, no approval, concise report. | Tool call domain/connection and source-ledger record. | Not run |
| 7.13 Perti Google read | Give Perti agent a Google Doc URL and ask for its title and summary. | Uses Perti Google connection and retrieves content without approval. | Tool result and Perti ledger source. | Not run |
| 7.14 Personal GitHub read | Ask Personal agent to list available repositories. | Uses Personal token; no Perti-only repositories appear unless that token independently has access. | Repository list and connection ID. | Not run |
| 7.15 Perti GitHub read | Ask Perti agent to inspect a known Perti repository README. | Uses Perti token and returns grounded file content. | File result and provenance. | Not run |
| 7.16 Approval gate | Ask Perti agent to create a disposable calendar event or GitHub issue. | Read/planning runs, then the external write pauses with a useful approval card. Approval resumes the same task. | Approval card, completed tool call, run log. | Not run |
| 7.17 Credential isolation | Temporarily invalidate only `PERSONAL_GITHUB_TOKEN`, then repeat both GitHub reads. | Personal call fails clearly; Perti call remains healthy. | Separate connection/error records. | Not run |

## Pass Criteria

- Maestro retrieves one compact, ranked context bundle across all relevant stores and domains.
- Domain agents are hard-limited to their domain while Maestro can synthesize across domains.
- Current truth, provenance, source policy, scores, and omissions are inspectable.
- Repeated imports and observations are incremental and idempotent.
- Hygiene automates only low-risk maintenance and proposes semantic changes for review.
- Personal and Perti Google/GitHub actions use separate credentials and correct approval policy.
- External source evidence is visible in the common ingestion ledger.

## Run Record

| Date | Commit/PR | Tester | Automated | Human result | Defects / follow-up |
| --- | --- | --- | --- | --- | --- |
| | | | Not run | Not run | |
