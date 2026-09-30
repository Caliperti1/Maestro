# Google Workspace Setup

Maestro uses one domain-level `google` tool connection for Gmail, Drive, Docs, Slides, Sheets, and
Meet. Individual tools keep specific names such as `gmail.message.get` or `google.docs.get`, but
they inherit credentials from the shared Google Workspace connection for the domain.

## Enabled APIs

Enable these APIs in the Google Cloud project:

- Gmail API
- Google Drive API
- Google Docs API
- Google Slides API
- Google Sheets API
- Google Meet API
- Google Calendar API

## OAuth Client

Use a Web application OAuth client for Maestro's one-click Tools flow.

- Authorized redirect URI: `https://<maestro-api-host>/integrations/google/callback`
- Authorized JavaScript origin: the Maestro frontend origin.

## Scopes

Request write-capable scopes now so the refresh token can support future approved write tools
without re-running OAuth. Maestro should still require approval before external writes are executed.

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/drive.meet.readonly
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/presentations
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/meetings.space.readonly
https://www.googleapis.com/auth/calendar
```

Scope intent:

- `gmail.readonly`: read messages and threads.
- `gmail.modify`: mark messages read, apply labels, and perform future approved mailbox updates.
- `gmail.compose`: create future approved drafts/sends without broader Gmail mailbox write scope.
- `drive`: read and manage files visible to the connected domain account, including arbitrary
  linked folders and files shared with that account. Maestro still gates external writes through
  tool policy even though the OAuth token is write-capable.
- `drive.meet.readonly`: read Meet-created Drive artifacts such as transcripts, notes, recordings,
  and meeting notes.
- `documents`: create and edit Google Docs.
- `presentations`: create and edit Google Slides.
- `spreadsheets`: create and edit Google Sheets.
- `meetings.space.readonly`: read Google Meet conference records.

## Deployment Environment Variables

Configure the OAuth app once. Maestro creates a separate encrypted account connection for each
domain from the Tools page:

```env
INTEGRATION_CREDENTIAL_ENCRYPTION_KEY=
INTEGRATION_GOOGLE_CLIENT_ID=
INTEGRATION_GOOGLE_CLIENT_SECRET=
INTEGRATION_GOOGLE_REDIRECT_URI=https://<maestro-api-host>/integrations/google/callback
```

## Maestro Tool Connection

In Tools, select Google Workspace, select the domain, and choose **Connect Google Workspace**.
Sign into the account for that domain and approve the consent screen. The page returns with the
account email and `connected` status. No token copying or credential JSON is required.

The older domain-prefixed environment variables remain supported for rollback and show as
`legacy` connections until they are replaced through this flow.

## Current Tools

Current tools are read-first except approved Gmail mutations:

- `gmail.message.search`
- `gmail.message.list_recent`
- `gmail.message.get`
- `gmail.thread.get`
- `gmail.draft.create`
- `gmail.message.modify`
- `google.drive.file.get`
- `google.drive.folder.list`
- `google.drive.file.export`
- `google.docs.get`
- `google.slides.get`
- `google.sheets.get`
- `google.sheets.values.get`
- `google.meet.conference_records.list`
- `google.meet.conference_records.get`
- `google.calendar.events.list`
- `google.calendar.event.get`
- `google.calendar.event.create` (approval required)
- `google.calendar.event.update` (approval required)
- `google.calendar.event.delete` (approval required)

`drive.file` alone is not sufficient for email triage over arbitrary shared links. Google limits
that scope to files Maestro created or files explicitly opened through the OAuth application. If a
linked file opens in a browser but Drive API calls return `404 File not found`, regenerate the
refresh token with the `drive` scope above and confirm the browser and Maestro OAuth identities are
the same account.

## Future Write Tools

These should be implemented as approval-gated external writes:

- Create/update Google Docs.
- Create/update Google Sheets and append rows.
- Create/update Google Slides decks.
- Create Google Calendar events when Calendar is added to the Google family.
- Create/send Gmail drafts once email approval policy is hardened.
