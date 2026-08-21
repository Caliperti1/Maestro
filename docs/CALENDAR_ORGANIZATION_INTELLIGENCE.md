# Calendar and Organization Intelligence

## Product Intent

Maestro's calendar is the cross-domain schedule Chris opens each day. Events remain domain-owned,
but Maestro and the calendar UI can aggregate every visible domain to expose one coherent schedule,
cross-domain conflicts, linked people, and linked organizations.

Google Calendar is an integration source and destination, not the canonical UI or the only source of
truth. The local calendar must remain useful for events extracted from email, agent reports, user
messages, and future calendar providers.

## Calendar Item Model

`calendar_events` owns the shared time geometry and human-facing fields for three item kinds:

- `event`: a meeting or commitment that normally blocks availability
- `scheduled_todo`: a task assigned a working window
- `context_window`: household, childcare, routine, energy, location, or availability context that
  affects scheduling judgment without claiming Chris is unavailable

Every row carries `blocks_time` and a `scheduling_effect`. Existing events migrate as blocking,
`hard` constraints. Context windows are always nonblocking and use `informational`, `prefer`,
`prefer_avoid`, or `strongly_avoid` effects. This gives schedule reasoning an explicit distinction
between conflicts and tradeoffs instead of relying on event titles.

Shared fields include:

- domain, title, summary, start/end, timezone, and all-day state
- recurrence rule, location, and conferencing URL
- organizer identity and lifecycle status
- provenance, supporting references, and external synchronization identity

`calendar_event_attendees` links participants to canonical contacts when identity is known. It can
also retain a source-provided name/email safely when a contact is unresolved. Chris's own identity is
marked as the Maestro user and is never turned into a contact.

`calendar_event_organizations` links organizations to events with a role such as `partner`, `host`,
or `related`.

Future events appear in each linked contact's upcoming meetings. When an ordinary event occurs or is marked
complete, Maestro materializes one provenance-backed contact interaction per linked contact. This
avoids polluting contact history with meetings that have not happened and prevents duplicate meeting
interactions. Context windows never create contact interactions and do not participate in hard
conflict detection.

## Calendar Service

`CalendarIntelligenceService` owns:

- legacy attendee backfill into first-class links
- contact and organization resolution
- attendee and organization replacement during edits
- cross-domain overlap conflict detection
- completed-event contact interaction materialization
- the event payload consumed by APIs and the frontend

The calendar API supports aggregated or domain-filtered listing, time-window queries, manual event
creation, and edits. The frontend uses FullCalendar for month, week, day, and agenda views, selectable
time ranges, drag/reschedule, resize, domain colors, conflict signals, and touch interaction. Context
windows render as translucent dashed overlays and can be hidden without removing them from Maestro's
retrieval context. Recurring items pass their explicit duration to FullCalendar so a 30-minute series
occupies 30 minutes rather than the library's one-hour default.

## Organization Intelligence

Organizations are global identities with domain-scoped context. Canonical matching uses durable
identifiers before names:

1. exact website or email/web domain
2. canonical name
3. evidence-backed alias
4. affiliated contacts, interactions, relationships, and semantic profile context

`organization_identifiers` stores exact identity evidence. `organization_relationships` stores
directional, optionally domain-scoped links such as parent/subsidiary, partner, customer, or vendor.
Organization profiles expose aliases, identifiers, domain notes, affiliated people, interaction
history, related organizations, and linked events. Hygiene merges exact-identifier duplicates while
preserving aliases, links, provenance, and relationships.

## External Calendar Boundary

The schema includes provider, external calendar/event IDs, ETag, sync status, and last-sync time.
These fields are the foundation for provider adapters but no bidirectional synchronization policy is
enabled in this slice.

A future adapter should:

- map each external account/calendar to a Maestro domain
- upsert by provider plus external event ID
- retain provider ETags and tombstones for idempotency
- avoid feedback loops with a local change token or version
- surface conflicts instead of silently overwriting concurrent edits
- keep provider-specific payloads in metadata while canonical fields remain provider-neutral

This boundary lets Praxis, Personal, Perti Laboratories, and other accounts coexist in one Maestro
calendar without requiring them to share a Google account.

## Migration

Migration `0016_perti_calendar_intelligence` moves all former Personal IRAD domain-owned records into
the former Ophi domain, renames that domain to `perti-laboratories`, and removes the obsolete Personal
IRAD domain. Memory dropbox startup also moves both legacy domain directories into the Perti
Laboratories directory while preserving conflicting filenames.
