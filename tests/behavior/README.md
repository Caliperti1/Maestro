# Maestro Behavioral Test Matrix

This directory tracks end-to-end behavioral tests that are not fully automated yet. Use these
matrices while manually testing Maestro through the UI and backend logs.

Status legend:

- `Not run`: scenario has not been tested against the current build.
- `Pass`: behavior matches the expected action.
- `Partial`: behavior is directionally correct but needs polish or a small fix.
- `Fail`: behavior is missing, wrong, or blocks the scenario.
- `Blocked`: cannot test because setup, credentials, or another dependency is missing.

For each test pass, record:

- date and branch
- exact user messages sent
- observed Maestro response/session/workflow behavior
- defects found
- fixes made or issue links
- remaining retest notes

Keep this matrix honest. If a stub or debug panel is no longer useful after a behavior is hardened,
remove it rather than carrying UI/code bloat forward.
