# USMA Calendar Reconciliation

The event-change Power Automate flow provides low-latency additions, edits, and cancellations. It
cannot discover an unchanged recurring series that predates the flow, so pair it with one daily,
authoritative calendar snapshot. The snapshot sends one email with one JSON attachment instead of
one email per occurrence.

## Operating Pattern

Keep both flows active:

- **Event-change flow:** low-latency additions, edits, and cancellations using the existing
  `[MAESTRO-INGEST][USMA][CALENDAR]` contract.
- **Daily snapshot flow:** backfill, missed-trigger recovery, future recurring occurrences, and
  confirmed deletion reconciliation using the batch contract below.

Run only one active copy of each flow. The daily snapshot should run once each morning. Maestro
requires an event to be absent from two consecutive authoritative snapshots before cancelling it,
which protects the calendar from a partial or failed Power Automate response.

## Power Automate Snapshot Flow

1. Create a **Scheduled cloud flow** named `Maestro - USMA Calendar Snapshot`.
2. Configure **Recurrence** for once per day, using `(UTC-05:00) Eastern Time (US & Canada)` and a
   low-traffic time such as 03:30.
3. Add three **Data Operation - Compose** actions:
   - `WindowStartUtc`: `utcNow()`
   - `WindowEndUtc`: `addDays(utcNow(), 60)`
   - `SnapshotId`: `concat('usma-calendar-', formatDateTime(utcNow(), 'yyyy-MM-dd'))`
4. Add Office 365 Outlook **Get calendar view of events (V3)**:
   - Calendar: the authoritative USMA calendar.
   - Start time: output of `WindowStartUtc`.
   - End time: output of `WindowEndUtc`.
   - In action settings, enable pagination with a threshold of 2500.
5. Add **Data Operation - Select** named `SelectCalendarOccurrences`.
   - From: the `value` array returned by **Get calendar view of events (V3)**.
   - Map each occurrence to the fields in the table below. Prefer the connector's dynamic-content
     tokens; the underlying property names can vary slightly between connector revisions.
6. Add **Data Operation - Compose** named `ComposeSnapshot`. Build one object containing the
   snapshot metadata and the output array from `SelectCalendarOccurrences`.
7. Add Office 365 Outlook **Send an email (V2)**:
   - To: `maestro@perti.io`
   - Subject: `[MAESTRO-INGEST][USMA][CALENDAR_SNAPSHOT] <SnapshotId output>`
   - Body: use the manifest below.
   - Attachment name: `<SnapshotId output>.json`
   - Attachment content: `base64(string(outputs('ComposeSnapshot')))`
8. Save and manually test the flow. Verify it sends one email with one JSON attachment.
9. Disable the old scheduled flow that sends one email per occurrence. Do not disable the separate
   event-change flow.

Microsoft documents **Select** as the Data Operation for reshaping arrays and **Compose** for
building a reusable object. `string()` converts that object to compact JSON before the Outlook
connector receives the attachment bytes.

## Select Mapping

| Output key | Outlook value |
| --- | --- |
| `source_id` | Occurrence event ID |
| `ical_uid` | iCalUId |
| `series_master_id` | Series master ID, when present |
| `action` | `cancelled` when Is cancelled is true; otherwise `upsert` |
| `title` | Subject |
| `start` | Start |
| `end` | End |
| `timezone` | Start time zone, or `UTC` when the connector returns UTC values |
| `location` | Location display name |
| `organizer` | Organizer email address |
| `required_attendees` | Required attendees |
| `optional_attendees` | Optional attendees |
| `all_day` | Is all day |
| `meeting_link` | Online meeting or web link, when present |
| `body_preview` | Body preview, when appropriate for the sanitized handoff |

Use the concrete occurrence ID as `source_id`, not only the series ID. Otherwise every occurrence
in a recurring series would overwrite the same Maestro event.

## Snapshot Object

In `ComposeSnapshot`, produce this shape. Insert Compose outputs and the Select output as dynamic
values rather than as quoted literal text.

```json
{
  "schema_version": 1,
  "snapshot_id": "usma-calendar-2026-09-23",
  "source_system": "usma_outlook",
  "domain": "usma",
  "generated_at": "2026-09-23T07:00:00-04:00",
  "window_start": "2026-09-23T11:00:00Z",
  "window_end": "2026-11-22T11:00:00Z",
  "events": [
    {
      "source_id": "concrete-occurrence-id",
      "ical_uid": "series-ical-uid",
      "series_master_id": "series-master-id",
      "action": "upsert",
      "title": "Department sync",
      "start": "2026-09-24T14:00:00Z",
      "end": "2026-09-24T15:00:00Z",
      "timezone": "UTC",
      "location": "Washington Hall",
      "organizer": "organizer@westpoint.edu",
      "required_attendees": ["christopher.aliperti@westpoint.edu"],
      "optional_attendees": [],
      "all_day": false,
      "meeting_link": ""
    }
  ]
}
```

## Email Manifest

```text
source_system: usma_outlook
domain: USMA
record_type: calendar_snapshot
source_id: <SnapshotId output>
source_timestamp: <utcNow() output>
window_start: <WindowStartUtc output>
window_end: <WindowEndUtc output>
title: USMA calendar snapshot
```

## Legacy Event-Change Contract

The event-change flow continues using this subject:

```text
[MAESTRO-INGEST][USMA][CALENDAR] <event subject>
```

And this body:

```text
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

Maestro treats `added`, `updated`, and `upsert` as the same active state and ignores attendee order.
Real attendee membership, time, title, location, recurrence, or cancellation changes still update
the canonical event. Structured event and snapshot handoffs retain their raw evidence and ingestion
ledger provenance but bypass long-term memory extraction because the calendar is their canonical
store.
