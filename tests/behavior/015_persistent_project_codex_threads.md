# Behavior 015: Persistent Project Codex Threads

## Objective

Verify that every registered code project can maintain a persistent, visible Codex Steward and
Worker while each coding change remains isolated in its own git worktree and PR.

## Setup

- Maestro backend and frontend are running from current `main`.
- The project has a valid repository profile and local checkout.
- The project's coding agent has access to `codex.task.run` and the required GitHub tools.
- The Codex desktop app is signed in on the same Mac as Maestro.

## 15.1 Initialize Ophi Threads

1. Open **Memory > Product Issues**.
2. Select an Ophi issue with the Ophi repository attached.
3. In **Codex project threads**, select **Initialize**.

- [ ] The UI creates `Ophi Maestro Steward` and `Ophi Maestro Worker`.
- [ ] Both rows show `ready` and a short session identifier.
- [ ] Both named tasks appear in the Codex desktop app.
- [ ] Opening either task shows its short initialization turn.
- [ ] Repeating initialization does not create duplicates or consume another model turn.

## 15.2 Reuse The Worker

1. Mark one well-scoped Ophi issue as an agent task.
2. Approve the generated workflow and allow it to open a PR.
3. Record the Worker session ID shown in Product Issues and in the run output.
4. Send review feedback through Maestro that requires a revision to the same project.

- [ ] Both coding turns use `Ophi Maestro Worker`.
- [ ] The Worker session ID remains unchanged.
- [ ] The Codex task contains both turns in chronological order.
- [ ] The new coding turn re-reads the current worktree/repository state.
- [ ] The issue execution, branch, commit, and PR remain linked in Maestro.

## 15.3 Isolation And Recovery

1. Start a coding task for a different registered repository.
2. Confirm it uses that project's Worker, not Ophi's.
3. Archive or remove a disposable test Worker in Codex, then run another task for that repository.

- [ ] The second repository uses its own named Worker.
- [ ] Ophi context does not appear in the other project's task.
- [ ] A missing Worker is recreated under the same stable project name.
- [ ] Maestro records the replaced session ID in repository metadata.
- [ ] Git changes still occur only in Maestro-managed feature worktrees.

## Run Record

- Date: 2026-09-15
- Branch/commit: `codex/persistent-project-codex-threads`
- Project: Ophi protocol proof; full UI test pending
- Steward session ID: `01a0a5fd-4153-75c0-98c2-f6b9e508f071`
- Worker session ID:
- Issue and PR:
- Result: Automated suite passed; live create/resume lifecycle passed
- Notes: The named Ophi Steward is visible in Codex and retained its ID across resumed turns.
