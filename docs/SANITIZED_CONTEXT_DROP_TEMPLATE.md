# Sanitized Context Drop Template

Use this template on an authorized USMA or L3 machine only after confirming the local rules for
collection, sanitization, and transfer. Delete empty sections. Keep entries concise and describe
Chris's obligations and allowed context rather than copying source documents.

```markdown
---
schema_version: 1
source_system: usma_sanitized_context_drop
source_id: usma-2026-08-11-daily
source_timestamp: 2026-08-11T17:00:00-04:00
domain: usma
sensitivity: sanitized_work_context
transfer_method: manual_approved_transfer
reviewed_by: Chris Aliperti
reviewed_at: 2026-08-11T17:05:00-04:00
review_status: reviewed
contains_restricted: false
contains_cui: false
contains_classified: false
contains_proprietary_technical_data: false
---

# Calendar Obligations

- 2026-08-13 09:00 ET: Lesson 6. Preparation required by Wednesday evening.

# Chris's Action Items

- Finish the Lesson 6 exercise before 2026-08-12 18:00 ET.

# Decisions

- No durable decisions in this drop.

# Relationship Updates

- Person or organization: concise, authorized relationship context.

# Project Status

- Project name: non-sensitive state, next milestone, and Chris's role.

# Open Questions

- Question or dependency that Maestro should surface to Chris.

# Source References

- Stable local reference or record ID only. Do not include an inaccessible document body.
```

For L3, use `source_system: l3_sanitized_context_drop`, `domain: l3`, and the same policy flags.
If any prohibited-content flag would be `true`, stop and do not transfer the drop.
