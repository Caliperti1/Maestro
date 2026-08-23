# Domain Email And Calendar Monitors

Maestro uses durable workflow templates to connect domain-owned Google accounts without sharing
credentials across domains. Email and Calendar have separate producers because Calendar must remain
correct even when an invitation or later edit never appears in an inbox.

## Installed Patterns

The Workflows screen exposes four templates:

- Praxis Email Triage
- Perti Email Triage
- Praxis Calendar Monitor
- Perti Calendar Monitor

Installation is always paused. Each definition also starts in shadow mode and with its provider
watch off. Activating the definition, switching to live mode, and enabling its watch are deliberate,
independent actions.

## Email Flow

The shared Gmail History producer keeps one cursor per watched domain. It emits the exact Gmail
message ID and versioned source metadata to the matching domain workflow. The dedicated email agent
reads that message, applies the manager skills, routes supported objects, and notifies Chris only
when he personally needs to act.

## Calendar Flow

The shared Calendar producer keeps one Google incremental sync token per watched domain. First
enablement records the current token without importing past events, then deterministically seeds a
bounded window of upcoming event instances. Expanding future instances is important for recurring
series whose master record may not have changed recently. This seed bypasses agent reasoning and
therefore adds no LLM cost. Later changes emit the exact calendar ID, event ID, provider version,
and Google event payload; changes to a recurring master refresh its upcoming instances.

The calendar workflow performs a deterministic routed write before its reasoning pass. Canonical
events are keyed by domain plus external provider/calendar/event IDs. A retry of one provider
version is idempotent; a later edit updates the same event. Time changes, cancellations, attendees,
organizer, recurrence, location, and conferencing links remain provider-grounded.

Each expanded occurrence retains its provider series ID and original start time. Google-originated
occurrences can therefore be adjusted independently in Maestro. For Maestro-native recurrence
rules, dragging, resizing, or editing an occurrence creates a dated exception and excludes the
original slot; choosing `Edit full series` edits the parent recurrence instead.

Shadow mode records the proposed run without the routed write. Switch a monitor to live only after
one controlled shadow test looks correct.

## Perti Credentials

Add these values to the live runtime `.env` and restart the backend:

```env
PERTI_GOOGLE_CLIENT_ID=
PERTI_GOOGLE_CLIENT_SECRET=
PERTI_GOOGLE_CLIENT_REFRESH_TOKEN=
PERTI_GITHUB_TOKEN=
```

The Google refresh token must include Gmail read/modify, Calendar read/write, Drive, Docs, Slides,
Sheets, and Meet scopes already documented in [GOOGLE_WORKSPACE_SETUP.md](GOOGLE_WORKSPACE_SETUP.md).
The Perti GitHub connection inherits `PERTI_GITHUB_TOKEN`; choose its default repository in Tools >
GitHub when a single repository is appropriate, or leave it unset and pass a repository per task.

## Safe Activation Order

1. Confirm Perti Google and GitHub credentials are present in `.env`; restart the backend.
2. In Tools, run read-only Perti smoke tests for Gmail profile/recent messages, Calendar events, and
   GitHub repositories.
3. In Workflows, install Perti Email Triage and Perti Calendar Monitor paused.
4. Activate both while leaving shadow mode on.
5. Enable each watch. The first poll only establishes the current provider cursor.
6. Send one controlled email and create or edit one controlled calendar event.
7. Inspect both shadow runs, then switch each definition to live mode.
8. Repeat with new source objects and verify canonical routed outputs and quiet/notification rules.

The Praxis Calendar Monitor follows the same steps and uses the existing Praxis Google connection.
