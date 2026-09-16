# Maestro Voice GPT-Live gateway

Maestro exposes `POST /maestro/voice/live/session` as an opt-in WebRTC negotiation gateway for
the Maestro Voice iOS client. The endpoint sends the phone's SDP offer to OpenAI and returns the
session ID and SDP answer. The OpenAI project key stays on the Maestro server.

The Live session uses `gpt-live-1` with client delegation. GPT-Live owns natural speech,
turn-taking, and lightweight conversational mechanics. Substantive requests are delegated to the
iOS client, which sends the accumulated transcript through the existing `/maestro/respond` API.
This preserves Maestro's routing, memory, workflow, permissions, and durable conversation ID.
The request is persisted before background reasoning starts. The phone then waits on the existing
`/maestro/channel/ws` conversation feed and correlates Maestro's published response by
`client_turn_id`; GPT-Live does not poll a separate turn-status API. The same message therefore
appears in the Maestro web chat before it is appended to the Live session for speech.

The iOS client may select any supported built-in GPT-Live voice when creating a session. The
gateway validates that selection and applies it under `session.audio.output.voice`. Voice changes
take effect on the next session because GPT-Live voices cannot change after startup.

## Configuration

Set these values in the local `.env` and restart the backend:

```dotenv
OPENAI_API_KEY=your-direct-openai-project-key
OPENAI_LIVE_ENABLED=true
OPENAI_LIVE_MODEL=gpt-live-1
```

Do not place the key in the iOS app, Xcode project, repository, or Maestro frontend. When the flag
is false or the key is absent, the endpoint returns `503`; Maestro Voice automatically uses its
native Apple speech path instead.

## Verification

Run:

```shell
.venv/bin/pytest -q tests/test_live_voice.py
.venv/bin/ruff check app/api/voice_live.py app/maestro/live_voice.py tests/test_live_voice.py
```

Tests use an in-memory HTTP transport and never contact OpenAI or read a developer credential.
Runtime logs record model, session ID, upstream status, and negotiation latency without logging the
API key, SDP, audio, or transcript.
