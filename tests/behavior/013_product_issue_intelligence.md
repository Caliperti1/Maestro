# Behavior 013: Product Issue Intelligence

## Goal

Capture product ideas once, reconcile them against prior work, keep Maestro and GitHub synchronized,
and let selected issues become inspectable background coding workflows.

## Setup

- Confirm Maestro, Praxis/GroundTruth, Perti/Deeper Learning, Perti/AAce, and Perti/Ophi appear as Product Issue projects/repositories.
- Confirm each repository has visible `Repository Intelligence` and `Issue Hygiene` durable workflows.
- Leave the Product Issues page open in another tab for inspection.

## A. Clarified Capture

1. In Knowledge mode say: `Add an issue to improve how Maestro selects relevant tools.`
2. If project, repository, or desired behavior is genuinely unclear, answer Maestro's single focused question.
3. Open Memory > Product Issues.

Expected:

- [ ] Maestro does not create a weak placeholder before the clarification is answered.
- [ ] The accepted issue appears at the same level as GitHub-originated issues.
- [ ] It includes domain, project, repository, problem, desired outcome, criteria, and provenance.
- [ ] Brainstorming without `save`, `capture`, `log`, `create`, or `action` remains conversation.

## B. Immediate Reconciliation

1. Propose the same issue again with a slightly different title and one new acceptance criterion.
2. Ask Maestro what changed.

Expected:

- [ ] Maestro searches existing issues before capture.
- [ ] A true duplicate enriches the canonical issue instead of creating another card.
- [ ] The original submission remains visible in merge provenance.
- [ ] Related but distinct work remains separate and gets a relationship.
- [ ] Contradictory work is related as a conflict or superseding change rather than silently overwritten.

## C. GitHub Sync

1. Create one issue in GitHub and one local issue assigned to the Maestro repository.
2. Click `Sync GitHub` from the local issue.

Expected:

- [ ] The GitHub issue imports once with its number and URL.
- [ ] The local issue publishes once and gains its GitHub identity.
- [ ] Repeating sync creates no duplicates.
- [ ] Closing a GitHub issue closes the canonical Maestro issue on the next sync.
- [ ] Concurrent local and remote edits produce a visible sync conflict, not last-write-wins data loss.

## D. Visible Hygiene and Observation

1. Merge a small repository change or create an intentionally similar issue pair.
2. Wait for the repository worker or invoke its test endpoint during development.

Expected:

- [ ] Cheap unchanged polling creates no report or notification.
- [ ] A changed repository produces a visible workflow run, report, run-log entry, and staged memory evidence.
- [ ] Issue hygiene produces a visible run only when it imports, publishes, closes, relates, conflicts, or merges something.
- [ ] High-confidence local duplicates may merge; two GitHub issues are never silently collapsed.

## E. Agent Task

1. Mark a well-scoped repository issue as `Agent task` and save it.

Expected:

- [ ] Maestro announces that background planning began.
- [ ] Missing critical scope produces one RFI in chat; the answer resumes this same issue.
- [ ] The coding workflow uses an isolated branch and links the PR to the GitHub issue with `Closes #N`.
- [ ] The issue records its workflow run, PR, and persistent Codex session.
- [ ] Completion updates the run log/report and notifies Chris conversationally.

## F. Cross-Project Knowledge Retrieval

1. In Knowledge mode say: `Show me the most relevant open product issues across Maestro, GroundTruth, Deeper Learning, AAce, and Ophi related to memory, reporting, or integrations. Do not create or edit anything.`
2. Inspect the answer and the latest `maestro.knowledge` telemetry entries.

Expected:

- [ ] Maestro recognizes GroundTruth as a Praxis project and Deeper Learning, AAce, and Ophi as Perti Laboratories projects without asking for ownership clarification.
- [ ] Maestro uses one portfolio search with the canonical project keys instead of one search per project.
- [ ] Projects with matching issues are represented before remaining result slots are filled by global relevance.
- [ ] The answer names the project, issue title, status, and why each result is relevant.
- [ ] No issue, organization, memory, workflow, or other record is changed.
- [ ] The normal path uses two Knowledge calls: search planning followed by grounded synthesis.
- [ ] No identical read action is executed more than once in the turn.
- [ ] The persisted action-result metadata remains compact enough to inspect without carrying full issue bodies.
