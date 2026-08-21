# Behavior Test 010: Calendar Context Windows

## Purpose

Prove that Maestro can retain household and personal schedule context without misrepresenting it as
a meeting or hard availability conflict, while preserving accurate event duration in the Calendar.

## Setup

1. Run migration `0022_calendar_context_windows` and restart the backend and frontend.
2. Open Memory > Calendar with `Show context` enabled.
3. Navigate to a week containing the recurring Collaborative Autonomy Standup.

## Automated Gate

```bash
pytest -q
cd frontend && npm run build
```

## Human Matrix

| ID | Human action | Expected behavior | Evidence | Status |
| --- | --- | --- | --- | --- |
| 10.1 Duration | Inspect Collaborative Autonomy Standup in Week view. | Each 11:00-11:30 occurrence occupies one 30-minute slot rather than one hour. | Week view. | Not run |
| 10.2 Direct creation | Click `New context`, create `Kids nap` from 1:00-3:00 PM as Childcare / Prefer to avoid, and save. | A translucent dashed block appears; it is labeled as context and remains editable. | Calendar and detail panel. | Not run |
| 10.3 Visibility | Toggle `Show context` off and on. | Context windows disappear and return without being archived or deleted; normal events remain visible. | Calendar week view. | Not run |
| 10.4 Nonblocking | Create a normal 30-minute event overlapping Kids nap. | Neither item is reported as a hard scheduling conflict solely because of the overlap. | Event detail conflict section. | Not run |
| 10.5 Knowledge creation | In Knowledge mode say: `My wife works every Tuesday from 7 AM to 7 PM. Keep that as household context and strongly prefer to avoid optional evening commitments.` | Maestro creates a recurring Personal context window, not a workflow or meeting. It is nonblocking and uses Household / Strongly avoid. | Chat, Calendar detail, unchanged workflow list. | Not run |
| 10.6 Schedule reasoning | Ask: `I need 90 minutes of focused work Tuesday. What time do you recommend?` | Maestro retrieves ordinary events and context windows, explains the tradeoff, treats meetings as hard constraints, and uses context as a soft scheduling preference. | Conversational answer. | Not run |
| 10.7 No interaction pollution | Inspect contacts after a context window passes. | No contact interaction is generated from the context window. | Contact interaction timeline. | Not run |

## Pass Criteria

- Context windows are stored with provenance in the calendar store and remain domain-scoped.
- Context windows never block availability, create contact interactions, or imply an external meeting.
- Maestro retrieves them during schedule reasoning and distinguishes tradeoffs from conflicts.
- Recurring event duration matches the stored start/end interval.
- The Calendar remains usable across Week, Day, Month, and Agenda views.

## Run Record

| Date | Commit/PR | Tester | Automated | Human result | Defects / follow-up |
| --- | --- | --- | --- | --- | --- |
| | | | Not run | Not run | |
