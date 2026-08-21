# Behavior Test 011: Perti And Calendar Monitors

## Purpose

Prove that Perti email and both company calendars are monitored independently, remain domain scoped,
and update canonical routed records without duplicate runs or events.

## Test Matrix

| ID | Action | Expected behavior | Evidence | Status |
| --- | --- | --- | --- | --- |
| 11.1 | Open Tools > Google > Perti Laboratories and run read-only Gmail and Calendar calls. | Perti credentials resolve through the Perti connection; no Praxis data appears. | Tool call domain/connection IDs and returned account. | Not run |
| 11.2 | Open Tools > GitHub > Perti Laboratories and list repositories. | The Perti token is used and the connection is labeled Perti Laboratories GitHub. | Tool trace and repository list. | Not run |
| 11.3 | Install all missing Perti and Calendar templates paused. | Four canonical templates exist; none silently activates. | Durable workflow cards. | Automated; UI not run |
| 11.4 | Activate Perti Email Triage in shadow mode, enable Gmail watch, and poll once. | The current Gmail cursor initializes and old mail is not processed. | Perti Gmail health says initialized; zero runs. | Automated producer behavior; human OAuth not run |
| 11.5 | Send one controlled Perti action email. | One exact-message shadow run appears under Perti and proposes grounded routes/notification. | Run event payload, report, and decision. | Not run |
| 11.6 | Activate each Calendar monitor in shadow mode, enable Calendar watch, and poll once. | Each domain stores its own sync token and does not import historical events. | Two domain health rows; zero old-event runs. | Automated producer behavior; human OAuth not run |
| 11.7 | Create a controlled future event in Perti Google Calendar. | One exact-event shadow run appears; no routed write occurs yet. | Trigger payload and shadow report. | Not run |
| 11.8 | Switch Perti Calendar Monitor live and edit the controlled event time/title. | The same canonical Maestro event updates with exact provider identity and provenance. | Stable Maestro event ID with new time/title/etag. | Automated upsert identity pending human OAuth test |
| 11.9 | Cancel the controlled Google event. | The same Maestro event becomes cancelled and remains inspectable. | Stable canonical ID and cancelled status. | Not run |
| 11.10 | Repeat 11.7-11.9 for Praxis. | Praxis Calendar stays current independently from email triage. | Praxis workflow run and canonical event. | Not run |
| 11.11 | Re-poll unchanged Gmail/Calendar provider pages. | No duplicate workflow run or canonical routed object appears. | Stable run/event IDs. | Automated |
| 11.12 | Keep Maestro chat open during simultaneous Perti email and Calendar runs. | Chat remains responsive and both background runs progress independently subject to agent locks. | Chat response plus active run cards. | Not run |

## Pass Criteria

- Every producer cursor is domain scoped and restart safe.
- Exact Gmail messages and exact versioned Calendar events are idempotent.
- Perti credentials never service Praxis work, or vice versa.
- Calendar edits and cancellations update one canonical event.
- Email notification rules remain quiet unless Chris personally needs attention.
