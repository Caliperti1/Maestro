# Maestro EvenG2 Client (Simulator First)

This app is an isolated prototype client under `EvenG2/` for Issue #67.

Current state:
- React + Vite shell
- Even Hub SDK installed
- Even Hub simulator tooling installed
- Switchable transport mode: Live Maestro API or local mock bridge
- Text and simulated voice messages flow through one persistent Maestro chat channel

## Prerequisites

- Node.js 20+
- npm 10+

## Install

```bash
cd EvenG2/maestro-even-client
npm install
```

## Run The Prototype

From the repository root, quick startup is available via Make:

```bash
make even-up
```

Or run pieces directly:

Start local dev server (hosted for simulator compatibility):

```bash
npm run dev:host
```

In a second terminal, start the Even simulator:

```bash
npm run sim
```

Optional automation port mode:

```bash
npm run sim:auto
```

Build check:

```bash
npm run build
```

## What To Click In The UI

1. Choose `Live API` or `Mock` mode.
2. If using live mode, set backend URL (default: `http://localhost:8000`).
3. Click `Connect API` (live) or `Connect Mock` (mock).
4. Send a text message with `Send`.
5. Click `Simulate Voice` to emit a mocked voice transcript event.
6. Click `Poll Maestro` (live) or `Simulate Maestro Event` (mock).

## Live Mode API Usage

The live bridge uses a websocket-first persistent channel:

- `WS /maestro/channel/ws`

It also uses existing Maestro endpoints for bootstrap and sends:

- `GET /maestro/sessions/active`
- `POST /maestro/respond`

Polling remains as a fallback if websocket is unavailable.

## Port Layout

- Maestro frontend: `http://localhost:5173`
- EvenG2 client: `http://localhost:5174`

This lets both frontends run side-by-side.

## Previous Mock-Only Steps (still supported)

1. Click `Connect`.
2. Send a text message with `Send`.
3. Click `Simulate Voice` to emit a mocked voice transcript event.
4. Click `Simulate Maestro Event` to generate inbound mock workflow/approval updates.

## Notes

- This prototype intentionally mirrors a thin-client model: display state and send simple interaction events.
- Latest status/message text is also pushed to an Even SDK text container so the simulator glasses display is populated.
- Gesture workflow (live mode):
	- Double tap: enter listening mode in the same persistent Maestro chat.
	- Single tap: start listening again in the same session.
	- Swipe up / swipe down: clear display, except in approval mode where up=approve and down=disapprove.
- Listening mode uses typed input for simulator realism: type in the composer and press `Send` to conclude listening and send as voice input.
- Display states on glasses:
	- `LISTEN` with a pulsing indicator while waiting for your typed input
	- `THINK` with a spinner indicator while Maestro is processing
	- Latest Maestro response body when available
	- Approval hint footer when approval-like content is detected
- Real Maestro API bridge and real voice stream contract are the next phase.
- Simulator behavior does not fully match hardware for BLE timing, rendering fidelity, and lifecycle edge cases.
