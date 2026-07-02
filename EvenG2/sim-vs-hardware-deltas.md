# Simulator vs Hardware Deltas

This file tracks known differences we should account for while validating utility before purchasing hardware.

## Confirmed From Even Docs

- Simulator is not a full hardware emulator.
- BLE timing and real performance are not faithfully reproduced.
- Rendering is close enough for logic/layout but not pixel-perfect visual QA.
- Some status events are hardcoded or omitted in simulator.
- Input set is available for core actions (up/down/click/double-click), but real behavior should still be validated on glasses.
- Image processing and some constraints can differ versus device limits.

## Practical Risks For Maestro Prototype

- Event latency measured in simulator may understate or overstate production behavior.
- Voice pipeline behavior can diverge when moving from mocked events to real microphone streams.
- Backgrounding/phone lifecycle behavior cannot be fully trusted from simulator-only runs.
- UX readability in simulator can differ from true optical display conditions.

## What We Can Reliably Validate In Simulator

- Message/event-driven UI flow correctness.
- Basic interaction mapping (click/double-click/scroll semantics).
- Bridge message contract shape and resilience behavior.
- Core failure handling states (disconnect/reconnect UI transitions).

## What Must Be Revalidated On Hardware Later

- End-to-end latency targets.
- Rendering quality and readability in real usage conditions.
- Actual microphone behavior and permission prompts.
- Real lifecycle behavior during phone backgrounding/locking.
- Ring-specific event source nuances.
