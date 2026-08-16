# Behavioral Test 008: Context Mailbox Ingestion

## Goal

Verify that an approved external context email enters Maestro through the Context Gateway, is
curated once, and retains transport provenance without allowing arbitrary email into memory.

## Test Matrix

| ID | Human action | Expected behavior | Evidence to record | Status |
| --- | --- | --- | --- | --- |
| 8.1 | Send a valid ChatGPT context handoff from an allowlisted sender with a unique `source_id`. | Within one polling interval the email receives `Maestro/Processed`, leaves Inbox, and one item appears in the target domain staging/ingestion view. | Gmail label, ingestion record ID, target domain, staged filename. | Not run |
| 8.2 | Wait for automatic memory processing. | The staged item is curated; preview, canonical memory/proposals, and routed objects reflect the content. | Preview counts and created object IDs. | Not run |
| 8.3 | Inspect one resulting memory. | Provenance includes ChatGPT source ID/time, Gmail message/thread ID, sender, transfer method, sensitivity, and content hash. | Provenance payload. | Not run |
| 8.4 | Resend identical content with the same `source_id`. | Email is processed as a transport duplicate; no second extraction or canonical write occurs. | Duplicate count and unchanged memory count. | Not run |
| 8.5 | Send corrected content with the same `source_id`. | A new source version is staged and the curator evaluates whether to update, supersede, reinforce, or ignore existing memory. | Two source versions and curator decision. | Not run |
| 8.6 | Send a valid-looking handoff from a non-allowlisted sender. | Email receives `Maestro/Quarantine`; no ingestion record or staging file is created. | Gmail label and zero new records. | Not run |
| 8.7 | Send a malformed subject/body from an allowlisted sender. | Email receives `Maestro/Quarantine` with no memory processing. | Health count and zero new records. | Not run |
| 8.8 | Attach a supported Markdown, PDF, or DOCX file. | Raw attachment is archived, hashed, text-extracted, and included in the same staged evidence/provenance. | Attachment metadata and extracted preview. | Not run |

## Initial ChatGPT Test Message

```text
Subject: [MAESTRO-CONTEXT][chatgpt][PERTI] Context mailbox acceptance test

source_system: chatgpt
source_id: chatgpt-context-mailbox-acceptance-001
source_timestamp: 2026-08-15T18:00:00-04:00
domain: perti

# Context mailbox acceptance test

Perti Laboratories owns Maestro and is building it as Chris Aliperti's cross-domain assistant.

Decision: External context handoffs must pass through the Context Gateway and Memory Curator.

Open question: Which additional daily ChatGPT summaries are valuable enough to automate?
```

After sending, open **Memory > Memory Manager**, select **Check mailbox**, and record 8.1 through
8.3 before running the duplicate and correction cases.
