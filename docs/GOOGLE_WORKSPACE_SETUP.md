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

## OAuth Client

Use a Web application OAuth client so Google OAuth Playground can mint a durable refresh token.

- Authorized redirect URI: `https://developers.google.com/oauthplayground`
- Authorized JavaScript origin: leave blank unless Google requires it. If required, use
  `https://developers.google.com`.

## Scopes

Request write-capable scopes now so the refresh token can support future approved write tools
without re-running OAuth. Maestro should still require approval before external writes are executed.

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/drive.meet.readonly
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/presentations
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/meetings.space.readonly
```

Scope intent:

- `gmail.readonly`: read messages and threads.
- `gmail.modify`: mark messages read, apply labels, and perform future approved mailbox updates.
- `gmail.compose`: create future approved drafts/sends without broader Gmail mailbox write scope.
- `drive.file`: create, open, and modify files Maestro creates or files opened with the app.
- `drive.meet.readonly`: read Meet-created Drive artifacts such as transcripts, notes, recordings,
  and meeting notes.
- `documents`: create and edit Google Docs.
- `presentations`: create and edit Google Slides.
- `spreadsheets`: create and edit Google Sheets.
- `meetings.space.readonly`: read Google Meet conference records.

## Environment Variables

Use domain-prefixed env vars so each domain can have separate credentials:

```env
PRAXIS_GOOGLE_CLIENT_ID=
PRAXIS_GOOGLE_CLIENT_SECRET=
PRAXIS_GOOGLE_CLIENT_REFRESH_TOKEN=
```

## Maestro Tool Connection

In the Tools tab, select `Google Workspace`, choose the domain, set auth type to `oauth`, and use:

```json
{
  "user_id": "me",
  "client_id_env": "PRAXIS_GOOGLE_CLIENT_ID",
  "client_secret_env": "PRAXIS_GOOGLE_CLIENT_SECRET",
  "refresh_token_env": "PRAXIS_GOOGLE_CLIENT_REFRESH_TOKEN",
  "default_query": ""
}
```

Restart the backend after changing `.env`.

## Current Tools

Current tools are read-first except approved Gmail mutations:

- `gmail.message.search`
- `gmail.message.list_recent`
- `gmail.message.get`
- `gmail.thread.get`
- `gmail.draft.create`
- `gmail.message.modify`
- `google.drive.file.get`
- `google.drive.file.export`
- `google.docs.get`
- `google.slides.get`
- `google.sheets.get`
- `google.sheets.values.get`
- `google.meet.conference_records.list`
- `google.meet.conference_records.get`

## Future Write Tools

These should be implemented as approval-gated external writes:

- Create/update Google Docs.
- Create/update Google Sheets and append rows.
- Create/update Google Slides decks.
- Create Google Calendar events when Calendar is added to the Google family.
- Create/send Gmail drafts once email approval policy is hardened.
