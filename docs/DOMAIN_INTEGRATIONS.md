# Personal And Perti Integrations

Maestro seeds one domain operator and shared Google/GitHub connection for both `personal` and
`perti-laboratories`. Child tools inherit the domain connection; credentials never travel between
domains and are never committed.

## Google

Follow [GOOGLE_WORKSPACE_SETUP.md](GOOGLE_WORKSPACE_SETUP.md) once per Google account. You may
reuse one Google Cloud OAuth client ID and secret, but create a distinct refresh token while signed
into each account.

Add these values to `.env`:

```env
PERSONAL_GOOGLE_CLIENT_ID=
PERSONAL_GOOGLE_CLIENT_SECRET=
PERSONAL_GOOGLE_CLIENT_REFRESH_TOKEN=
PERTI_GOOGLE_CLIENT_ID=
PERTI_GOOGLE_CLIENT_SECRET=
PERTI_GOOGLE_CLIENT_REFRESH_TOKEN=
```

## GitHub

Create a fine-grained token for each GitHub identity. Grant only the repositories that domain
should access. Read-only repository metadata/contents/issues/PRs need Contents, Issues, Pull
requests, and Metadata read access. Repository creation, issue changes, comments, and PR merges
need the corresponding write permissions.

```env
PERSONAL_GITHUB_TOKEN=
PERTI_GITHUB_TOKEN=
```

The seeded shared GitHub connection intentionally has no default repository. Select a repository
in the tool request or set the domain's default owner/repository from Tools > GitHub. Restart the
backend after `.env` changes.

## Smoke Test

1. Open Tools and confirm Personal and Perti Laboratories each show Google Workspace and GitHub.
2. Run the Personal Operations Agent once: `List my next five Google Calendar events and report their titles and times. Do not change anything.`
3. Run the Perti Operations Agent once: `List the repositories available to the Perti GitHub identity. Do not change anything.`
4. Confirm both reads run without approval and the Run Log shows the expected domain connection.
5. Ask either agent to create a disposable calendar event or GitHub issue; confirm Maestro pauses
   for approval before the external write.
