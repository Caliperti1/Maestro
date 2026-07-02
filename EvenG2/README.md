# EvenG2 Workspace

This directory contains all work for Issue #67 and is intentionally isolated from the rest of Maestro unless explicitly approved.

## Contents

- `issue-67-notes.md`: Investigation notes and open questions.
- `maestro-even-client/`: Simulator-first prototype client.
- `maestro-g2-architecture.md`: Proposed architecture for real Maestro backend integration.
- `sim-vs-hardware-deltas.md`: Tracking known differences and risks between simulator and real hardware.

## Current Development Flow

1. Build and validate UI + interaction model in simulator.
2. Stabilize bridge contract for text events and mocked voice events.
3. Review architecture proposal.
4. Implement Maestro backend integration using existing Maestro APIs where possible.
5. Record all observed simulator-vs-hardware caveats.
