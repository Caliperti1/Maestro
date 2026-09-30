# Maestro Node

The Maestro Node is a small local capability runner for computers that need to perform work on
behalf of the cloud Maestro control plane. This initial client runs in a terminal. Its runtime,
configuration, and stop behavior are designed so the same process can later be managed by
`launchd` and controlled by a macOS menu-bar application.

The initial release supports only `diagnostic.echo`. It deliberately provides no remote shell,
arbitrary command execution, filesystem browsing, or dynamic plugin loading.

## Install for development

From this directory, use Python 3.11 or newer:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Enroll

Create a one-time enrollment code in Maestro, then run:

```bash
.venv/bin/maestro-node enroll \
  --url https://your-maestro.example.com \
  --name "Personal Mac"
```

The code is prompted without echoing it. For local development only, it can be supplied with
`--code`. The client generates an Ed25519 device key, sends only the public key to Maestro, and
stores its device credential locally.

On macOS, local state defaults to:

```text
~/Library/Application Support/Maestro Node/
```

The directory is mode `0700`; configuration, the private key, runtime state, and SQLite journal
are mode `0600`. The credential is file-protected in this first CLI implementation. Moving it to
macOS Keychain is required before production packaging.

## Run

```bash
.venv/bin/maestro-node run
```

The node sends health heartbeats, long-polls for compatible jobs, renews active leases, and
reconnects with bounded exponential backoff. Stop it with Control-C. To perform one heartbeat and
one non-blocking poll during setup:

```bash
.venv/bin/maestro-node run --once
```

View local status with:

```bash
.venv/bin/maestro-node status
.venv/bin/maestro-node status --json
```

For isolated development or tests, override the data directory with `--data-dir` or the
`MAESTRO_NODE_HOME` environment variable.

## Idempotency and safety

Before executing a lease, the node records its idempotency key in a local SQLite journal. A
completed key returns the recorded result rather than repeating local work. An interrupted,
uncertain execution is not repeated automatically. Completion envelopes include a result digest
and an Ed25519 signature.

The control plane is authoritative for job state. Long polling is transport only, so the node can
disconnect and resume without holding in-memory workflow state.

## Tests

```bash
.venv/bin/pytest
```

## Next packaging steps

1. Store the credential and private key in macOS Keychain.
2. Add a `launchd` plist that runs the same `maestro-node run` entry point.
3. Add a menu-bar UI for connection health, recent activity, pause, and revocation.
4. Add `coding.codex.run` as an explicitly configured handler with repository allowlists and its
   own approval policy. Do not add a generic shell handler.

