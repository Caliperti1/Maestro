# USMA Calendar Reconciliation

The event-change Power Automate flow handles newly created, changed, and deleted events. It cannot
discover an unchanged recurring series that predates the flow. Pair it with a scheduled calendar
reconciliation flow so Maestro receives a rolling window of concrete occurrences.

## Recommended Flow

1. Create a scheduled cloud flow that runs once each morning.
2. Use Office 365 Outlook **Get calendar view of events (V3)** for the USMA calendar.
3. Set the UTC start to the current time and the UTC end to 60 days later.
4. Iterate over the returned events. Calendar view expands recurring series into occurrences.
5. Send one email per occurrence to `maestro@perti.io` using the contract below.

Repeated snapshots are expected. Maestro uses `source_id` plus a content hash to make unchanged
deliveries idempotent and updates the existing event when an occurrence changes.

## Email Contract

```text
Subject: [MAESTRO-INGEST][USMA][CALENDAR] <event subject>

source_system: usma_outlook
domain: USMA
record_type: calendar_event
action: upsert
source_id: <occurrence event id>
ical_uid: <iCalUId>
series_master_id: <series master id, when available>
title: <subject>
start: <UTC start date-time>
end: <UTC end date-time>
timezone: UTC
location: <location>
organizer: <organizer email>
required_attendees: <semicolon-separated emails>
optional_attendees: <semicolon-separated emails>
all_day: <true or false>
```

Use the concrete occurrence ID as `source_id`, not only the series ID. Otherwise each occurrence
will overwrite the same canonical event. If the connector omits a timezone, Maestro treats timed
`usma_outlook` values as UTC; explicit `UTC`, `Eastern Standard Time`, and IANA timezone values are
also accepted. All-day dates remain anchored to the Maestro home timezone.

## Operating Pattern

Keep both flows active:

- Event-change flow: low-latency additions, edits, and cancellations.
- Daily reconciliation flow: backfill, missed-trigger recovery, and future recurring occurrences.

For cancellations, send the same occurrence `source_id` with `action: cancelled`. A later active
snapshot with `action: upsert` restores the occurrence.
