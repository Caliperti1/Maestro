# Maestro Voice notifications

Maestro's primary chat message remains the canonical update. Background services post through
`record_channel_message`; global Maestro messages are marked as mobile updates and queued once for
each enabled iOS installation. The web UI and Maestro Voice write the same message receipt, so the
small check in chat reflects a read on either surface.

## Privacy and handoff

APNs receives only the generic alert `Maestro has an update` and the message and conversation IDs.
The private update text is never embedded in the push payload. When the user selects **Read Update**
or invokes the **Read Maestro Update** App Intent, Maestro Voice opens, fetches the message from the
private Maestro API, reads it with the selected local voice, marks it seen, plays the listening cue,
and continues in the linked conversation.

Priority is selected by Maestro and maps to Apple's interruption levels:

| Maestro priority | APNs interruption level | Sound |
| --- | --- | --- |
| `quiet` | `passive` | none |
| `important` | `active` | none |
| `voice_worthy` | `time-sensitive` | none |

Workflow-completion and attention events currently default to `voice_worthy`; other background
channel updates default to `important`. Producers can set `notification_priority` explicitly.

## APNs provider setup

1. Use an Apple Developer Program team that supports the Push Notifications capability for the
   Maestro Voice bundle identifier.
2. Create an APNs signing key and store its `.p8` file outside this repository.
3. Set `APNS_TEAM_ID`, `APNS_KEY_ID`, `APNS_PRIVATE_KEY_PATH`, and `APNS_TOPIC` in the local `.env`.
4. Run `alembic upgrade head` and restart the Maestro API.
5. In Maestro Voice, open Settings and select **Enable Maestro notifications**.

The provider worker leaves deliveries queued when credentials are absent, uses APNs token
authentication over HTTP/2, retries transient failures with backoff, and disables a device endpoint
when APNs reports that its token is no longer valid.
