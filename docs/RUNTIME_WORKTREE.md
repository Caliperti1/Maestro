# Dedicated Runtime Worktree

## Layout

Maestro uses separate Git working areas for distinct responsibilities:

| Location | Purpose | Expected branch/state |
| --- | --- | --- |
| `/Users/christopheraliperti/Maestro` | Development workspace and this Codex collaboration | Any reviewed work branch; may be dirty while work is in progress. |
| `/Users/christopheraliperti/Maestro-runtime` | The running Maestro application | Clean `main`, tracking `origin/main`. |
| `/Users/christopheraliperti/.maestro_worktrees` | Temporary Codex implementation worktrees | Isolated feature branches; created and removed per coding task. |

Codex now creates feature branches from the dedicated runtime checkout's up-to-date `origin/main` reference. It does not need the development workspace to be clean.

## Runtime Commands

From the development workspace:

```bash
make runtime-setup
make runtime-backend-reload
make runtime-frontend-tailscale
```

`runtime-setup` creates the `main` worktree when needed and shares the existing local `.env`, Python virtual environment, and frontend dependencies. It never copies API keys into Git.

To switch the currently running local application to the dedicated runtime after this PR is merged, stop the old backend and frontend, then start the two runtime commands above. The backend runs with Uvicorn autoreload and the frontend uses Vite HMR, so later approved deployments only need Maestro's runtime update tool.

## Delivery Checkpoint

For a coding workflow:

1. Maestro delegates implementation to Codex in an isolated feature worktree.
2. Codex commits, pushes, and opens a PR.
3. The original workflow remains active but blocked on Chris reviewing the PR.
4. The approval card performs one intentional delivery action: merge the reviewed PR and fast-forward the dedicated runtime checkout.
5. The workflow resumes, writes its final report/run log/artifact, and reports completion in the main Maestro channel.

No code is merged, stashed, or reloaded without Chris's explicit approval.

## Runtime Recovery

Before reload, Maestro checks the runtime branch, upstream state, and local changes.

- A clean runtime can pull `main` and reload normally.
- A dirty runtime is blocked before Git checkout or pull runs.
- Maestro can inspect the exact changed paths with `local.app.inspect`.
- The only automated recovery is an approved Git stash via `local.app.recover`; it preserves both tracked and untracked changes. Maestro never discards or silently commits unknown runtime changes.

After recovery, the original delivery approval remains available to retry; Maestro does not create an unrelated replacement workflow.
