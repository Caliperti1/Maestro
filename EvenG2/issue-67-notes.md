# Issue 67 Notes: Maestro x Even G2

## Scope from Issue #67

Goal: prove the Even G2 can act as a thin Maestro client via the simulator first, before buying hardware.

Target deliverables from the issue:
- SDK evaluation
- Simulator running locally
- Prototype client application
- Bridge from Maestro backend to the client
- Minimal UI components for the G2 display
- Architecture and risks documentation

## Confirmed Even Hub Facts

Source: public Even Hub docs as of 2026-07-01.

### Platform model
- Even Hub apps are web apps running in a phone-hosted WebView.
- The phone app relays display and input to the glasses over Bluetooth.
- The glasses are display plus input, not an application runtime.
- This matches the issue's thin-client assumption.

### Supported stack
- React and Vite are supported.
- Local dev flow is documented as Vite dev server plus simulator or QR sideload.
- Simulator command is documented as `evenhub-simulator http://localhost:5173`.

### Simulator limits
- The simulator is not a hardware emulator.
- It is suitable for layout, event logic, screenshots, and automation.
- It does not reproduce BLE timing, real device quirks, or some status events.
- Real performance, locked-phone behavior, and production parity still require hardware.

### Rendering constraints
- Display is 576x288 per eye.
- Rendering is not arbitrary HTML on-glass.
- UI is composed through SDK containers, not normal DOM/CSS layout.
- No font selection, no size control, no bold/italic, no background colors, no arbitrary pixel drawing.
- Exactly one container per page captures input.

### Input and media
- G2 and optional R1 ring expose press, double press, swipe up, swipe down.
- Audio input is available from either glasses mics or phone mic.
- Audio output is not available on the glasses.
- IMU is available.

### Networking model
- The client can use fetch, XHR, and WebSockets from the phone WebView.
- Outbound networking requires both:
  - app.json network whitelist entries
  - server-side CORS compatibility
- For local dev, plain HTTP to LAN hosts is acceptable.

## Confirmed Maestro Facts

### Existing architecture alignment
- Maestro already has approval workflows, task/report state, and orchestration concepts that fit a thin notification client.
- The existing frontend already surfaces approval and workflow state that can be reduced into a G2-specific UI.

### Current gap versus issue assumptions
- The current FastAPI app does not expose a WebSocket endpoint yet.
- Issue #67 assumes WebSocket-style bidirectional event flow.
- That means the first prototype likely needs one of these:
  - add a new isolated event endpoint later with permission, or
  - start with HTTP polling/SSE inside the EvenG2 prototype while we validate the UI model.

## Questions Requiring Chris Input

1. Do you want the EvenG2 work to stay entirely inside this repo under `EvenG2/`, or should I also create an isolated standalone prototype repo later as the issue suggests?
2. Is the no-edits rule intended to block backend additions too, even if a minimal event endpoint becomes necessary to prove the bridge?
3. Do you already have an Even Hub developer account, or should I assume I need to work only from public docs until you provide access?
4. Do you want the first proof of utility to target simulator-only mode, or should I also prepare the path for eventual real-device QR sideload without buying hardware yet?
5. Should the first bridge use plain HTTP plus mocked events, or do you want me to design specifically around a future Maestro WebSocket contract even though the backend does not currently expose one?
6. Is the desired first demo centered on approvals, notifications, or a single workflow progress view?

## Current Blockers / Things I Cannot Fully Complete Yet

### Blocked by repo constraints or permission
- I cannot wire the prototype into the existing Maestro backend event flow without your permission to modify code outside `EvenG2/`.
- I cannot truthfully prove end-to-end bidirectional Maestro-to-simulator behavior against the current backend until we choose and implement a transport boundary.

### Blocked by external access or hardware state
- I have not yet verified npm install and local execution of the Even Hub SDK and simulator in this environment.
- I have not yet verified whether Even Hub account sign-in or developer-mode phone setup is required for the simulator-only path.
- I cannot validate real-device latency, backgrounding, BLE timing, or locked-phone behavior without hardware.

### Public-doc limitation
- Public docs answer a lot, but not everything in the issue. The remaining unknowns likely require actual SDK package inspection or simulator execution:
  - exact packaging and manifest shape we will use for Maestro Companion
  - exact API ergonomics of the SDK TypeScript types in practice
  - practical local debugging workflow on macOS
  - whether there are any hidden constraints around long-lived WebSocket connections in the hosted WebView

## Recommended First Implementation Slice

Inside `EvenG2/` only:
- create an isolated Vite + React prototype app
- install `@evenrealities/even_hub_sdk`
- install `@evenrealities/evenhub-simulator`
- build a "Hello Maestro" screen
- mock Maestro events locally first
- structure the app around a transport adapter so we can switch from mock to real backend later
- document the contract the backend will need once you authorize broader integration work

## Working conclusion

Issue #67 is feasible.

The main technical risk is not whether Even G2 can render a Maestro client. The docs strongly support that.

The main risks are:
- the glasses UI is much more constrained than a normal React app
- the simulator is useful but not production-faithful
- Maestro does not yet expose the real-time boundary assumed by the issue
- your current instruction to avoid editing other code means the first useful milestone should be an isolated prototype plus an explicit backend contract proposal
