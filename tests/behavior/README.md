# Maestro Behavioral Test Matrix

## Active Tests

- [001 Code Feature Design Discussion](001_code_feature_design_discussion.md)
- [002 Maestro Self-Improving Coding Loop](002_maestro_self_improving_coding_loop.md)
- [003 Single Praxis Email Triage](003_single_email_triage.md)
- [004 Durable Praxis Email Triage](004_durable_praxis_email_triage.md)

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
- workflow outputs created: report, routed items, artifact, notification, run-log entry
- whether completed work left the active workflow queue and appeared in Run Log / Reports
- which skills/tools were visible to the selected agents
- defects found
- fixes made or issue links
- remaining retest notes

Keep this matrix honest. If a stub or debug panel is no longer useful after a behavior is hardened,
remove it rather than carrying UI/code bloat forward.

Current operating-model checks:

- Main chat remains the primary place Chris talks to Maestro.
- Active/durable workflows are inspected from the Workflows surface.
- Completed workflow history is inspected from Run Log.
- Human/agent-readable workflow output is inspected from Reports.
- The main chat dashboard shows a right-side artifact/report renderer, not queue/debug panels.
- Blocked workflows, approvals, and RFIs surface as compact attention items beside the renderer and
  can be inspected in Workflows.
- Reusable task instructions live in Skills and are assigned per agent.
- Agent prompts include assigned skills only; agents retrieve memory and reports through tools.
