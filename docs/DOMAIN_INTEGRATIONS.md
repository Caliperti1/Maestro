# Personal And Perti Integrations

Maestro seeds one domain operator and shared Google/GitHub connection for both `personal` and
`perti-laboratories`. Child tools inherit the domain connection; credentials never travel between
domains and are never committed.

## One-click account connections

The Tools page owns account setup. Select Google Workspace or GitHub, select a domain, and choose
**Connect**. Maestro redirects to the provider, stores the resulting token encrypted, and shares
that domain connection with the provider's child tools. Reauthorize and Disconnect are available
in the same panel.

The deployment itself needs one OAuth application per provider plus one stable encryption key:

```env
INTEGRATION_CREDENTIAL_ENCRYPTION_KEY=
INTEGRATION_GOOGLE_CLIENT_ID=
INTEGRATION_GOOGLE_CLIENT_SECRET=
INTEGRATION_GOOGLE_REDIRECT_URI=https://<api-host>/integrations/google/callback
INTEGRATION_GITHUB_CLIENT_ID=
INTEGRATION_GITHUB_CLIENT_SECRET=
INTEGRATION_GITHUB_REDIRECT_URI=https://<api-host>/integrations/github/callback
```

The callback URLs are also displayed on the Tools page so they can be copied into Google Cloud and
GitHub. Provider client secrets and account tokens are never sent to the browser.

## Legacy environment connections

Existing per-domain environment credentials remain supported during migration:

```env
PERSONAL_GOOGLE_CLIENT_ID=
PERSONAL_GOOGLE_CLIENT_SECRET=
PERSONAL_GOOGLE_CLIENT_REFRESH_TOKEN=
PERSONAL_GITHUB_TOKEN=
PERTI_GOOGLE_CLIENT_ID=
PERTI_GOOGLE_CLIENT_SECRET=
PERTI_GOOGLE_CLIENT_REFRESH_TOKEN=
PERTI_GITHUB_TOKEN=
```

These show as `legacy` connections in Tools and can be replaced by choosing Connect. The seeded
shared GitHub connection intentionally has no default repository; its structured repository field
remains available in Tools without exposing credential JSON.

## Smoke Test

1. Open Tools and confirm Personal and Perti Laboratories each show Google Workspace and GitHub.
2. Run the Personal Operations Agent once: `List my next five Google Calendar events and report their titles and times. Do not change anything.`
3. Run the Perti Operations Agent once: `List the repositories available to the Perti GitHub identity. Do not change anything.`
4. Confirm both reads run without approval and the Run Log shows the expected domain connection.
5. Ask either agent to create a disposable calendar event or GitHub issue; confirm Maestro pauses
   for approval before the external write.

For autonomous domain email and calendar monitoring, continue with
[DOMAIN_MONITORS.md](DOMAIN_MONITORS.md).
