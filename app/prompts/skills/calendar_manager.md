## Purpose
Maintain Maestro's cross-domain calendar as Chris's primary daily schedule. Create and update clean,
human-readable events while preserving domain ownership, contact links, organization links, and provenance.

## Use When
- A source contains a meeting, call, appointment, travel window, ceremony, deadline with a time, or event summary.
- Chris asks Maestro to add, move, complete, cancel, or inspect an event.
- New evidence changes an existing event's time, location, attendees, or purpose.

## Do Not Use When
- The item is an undated action or reminder; use the To Do Manager.
- The source only describes an agent's internal task.
- Identity or timing ambiguity would create the wrong event; ask a focused RFI instead.

## Resolution
1. Search existing events before creating a candidate when the message refers to "that meeting", a known
   attendee, or a previously discussed event.
2. Use date/time, attendees, title/purpose, location, and provenance together to resolve updates.
3. Never create a second event merely because a follow-up message adds a missing time or attendee.
4. Preserve an explicit source timezone. Otherwise use `America/New_York`.

## Event Construction
1. Use a natural calendar title such as `Partner review with Jane Smith`, never system language like
   `recorded meeting metadata`.
2. Populate `event_title`, `summary`, `start_at`, `end_at`, `timezone`, `all_day`, `recurrence_rule`,
   `location`, `conferencing_url`, `organizer_name`, `organizer_email`, `attendees`, and `organizations`
   when supported by the source.
3. Attendees should include known `contact_id`, name, email, organizer status, and response status. Include
   an unresolved name/email when no contact exists; the routed service will resolve or retain it safely.
4. Organizations should include a known organization ID or an exact supported name and its role in the event.
5. Infer a conventional one-hour duration only when a meeting is clear and no duration is supplied. Do not
   invent dates, participants, locations, recurrence, or relationships.
6. Include source refs for the message, report, artifact, or user turn that created or changed the event.

## Contact History
- Future scheduled events are shown as upcoming meetings on linked contact profiles.
- Once an event occurs or is marked complete, Maestro creates one meeting interaction for each linked contact.
- Do not separately create duplicate contact interactions for the same calendar event.

## Output Contract
Call `routed.item.create` with route type `event`, a calendar-ready title, concise content summary,
structured metadata, and provenance-backed source refs.

## Validation
- The title is useful in month, week, and day views.
- Start precedes end, and timezone/all-day state is explicit.
- Attendees and organizations are linked when evidence supports them.
- Updates resolve to an existing event when identity is sufficiently clear.
