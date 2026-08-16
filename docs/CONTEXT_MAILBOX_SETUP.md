# Context Mailbox Setup

## Configuration

The dedicated mailbox uses its own Google OAuth identity and never borrows a domain agent's Gmail
connection. Keep these values in `.env`; never commit them:

```env
MAESTRO_INTAKE_EMAIL=maestro@perti.io
MAESTRO_INTAKE_GOOGLE_CLIENT_ID=
MAESTRO_INTAKE_GOOGLE_CLIENT_SECRET=
MAESTRO_INTAKE_GOOGLE_REFRESH_TOKEN=
MAESTRO_INTAKE_ALLOWED_SENDERS=approved-sender@example.com,chris@perti.io
CONTEXT_MAILBOX_AUTORUN=true
CONTEXT_MAILBOX_INTERVAL_SECONDS=30
CONTEXT_MAILBOX_PAGE_SIZE=25
```

The OAuth grant needs Gmail read and modify access. Modify is required to apply terminal labels,
mark handled messages read, and archive successfully staged handoffs.

## Handoff Contract

Use this subject:

```text
[MAESTRO-CONTEXT][chatgpt][PERTI] CAD workflow discussion
```

Place machine-readable identity fields near the top of the message:

```markdown
source_system: chatgpt
source_id: chatgpt-conversation-123
source_timestamp: 2026-08-15T14:00:00-04:00
domain: perti

# CAD workflow discussion

## Durable context

- The mechanical design agent should create STL artifacts for downstream slicing.

## Decisions and open questions

- Decide which CAD execution environment to standardize.
```

Accepted domain labels include `personal`, `praxis`, `perti`, `maestro`, `usma`, and `l3`. The
normalized Maestro domain key is stored in the ingestion record.

For USMA or L3 handoffs, use the sanitized template and include `review_status: reviewed`, reviewer
identity/time, and false restricted-content flags. Sanitization happens before delivery; the intake
adapter rejects a handoff marked as containing restricted material.

## Runtime Behavior

1. The mailbox worker validates that OAuth resolves to the configured mailbox.
2. Only allowlisted senders are accepted.
3. The message and attachments are archived as raw evidence.
4. The Context Gateway claims the stable source object/version and writes one staged Markdown file.
5. The normal dropbox worker invokes the Memory Curator.
6. Canonical memories and routed objects retain original source and Gmail provenance.

Use **Memory > Memory Manager > Check mailbox** for an immediate poll. The same screen reports the
last health state and transport counts. Automatic polling continues while the backend is running.

## Safe Retry Rules

- Resend the same `source_id` and unchanged content: skipped as a duplicate.
- Resend the same `source_id` with corrected content: staged as a new version.
- Repair a `Maestro/Failed` message, remove the failed label, and retry.
- Review `Maestro/Quarantine` before changing either the allowlist or the message contract.
