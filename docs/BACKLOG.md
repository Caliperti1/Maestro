# Maestro MVP Backlog

## Product Goal

Build a locally hosted Maestro prototype running on a Mac that is reachable from a phone, coordinates work across domain enclaves, persists memory with provenance, and starts with a thin but real Daily Standup workflow.

Maestro is the cross-domain chief-of-staff layer. C Suite workflows are Maestro-level workflows, not a separate domain. Domain agents do scoped work, produce reports/artifacts, and propose memory. The Memory Curator is the only component that writes canonical memory.

## MVP Success Criteria

- Maestro web UI is reachable from a phone while the Mac is running.
- User can chat with Maestro from the web UI.
- System persists users, domains, agents, conversations, tasks, reports, artifacts, tool calls, and memory in Postgres.
- Default domains exist: Personal, Maestro Development, Praxis, Ophi, USMA, Personal IRAD Projects, and L3.
- Domain agents can produce task logs and reports.
- Agents can propose memory, and the Memory Curator can auto-write low-impact canonical memory.
- Very high-impact memory can be queued for user approval.
- Raw logs and artifacts are retained for provenance and future re-processing.
- Maestro can synthesize at least two domain reports into one answer.
- Daily Standup can task subordinate domain agents, collect thin reports, and produce one cross-domain recommendation report.
- Maestro Development domain can create implementation work and support code changes through GitHub/Codex from the beginning.
- Remote access path is documented using Tailscale or equivalent secure tunnel.

---

## Milestone 0: Repository Foundation

### 0.1 Initialize repository structure

- Create `/docs`
- Create `/app`
- Create `/app/api`
- Create `/app/core`
- Create `/app/agents`
- Create `/app/domains`
- Create `/app/memory`
- Create `/app/tools`
- Create `/app/db`
- Create `/app/workflows`
- Create `/alembic`
- Create `/tests`

### 0.2 Add reference docs

- Add architecture notes
- Add main workflow sequence diagram
- Add system component diagram
- Add database ER diagram
- Add initial Alembic migration
- Add backlog
- Add README with local startup instructions

### 0.3 Define architectural rules

- Maestro routes, delegates, synthesizes, and owns cross-domain workflows.
- C Suite workflows live at the Maestro level.
- Agents execute scoped work inside one domain.
- Agents cannot directly access unrelated domain memory.
- Maestro can retrieve from all domain memories and global memory.
- Agents can write logs, create artifacts, and propose memory.
- Only the Memory Curator writes canonical memory.
- All tool use must be logged.
- All agent outputs must produce a report object.
- All major outputs should be explainable through provenance references.

---

## Milestone 1: Local Web App and Phone Access

### 1.1 Backend skeleton

- Add FastAPI app.
- Add `GET /health`.
- Add config management.
- Add structured logging.
- Add CORS for local frontend.
- Bind backend to `0.0.0.0` for LAN testing.

### 1.2 Frontend skeleton

- Add React or server-rendered web UI.
- Create Maestro chat page.
- Create recent reports feed.
- Create sidebar with domains and settings.
- Create domain page shell.
- Create agent list shell.
- Create tool configuration shell.

### 1.3 Phone access

- Bind frontend dev server to LAN interface.
- Document how to find Mac LAN IP.
- Document access pattern: `http://<mac-lan-ip>:<port>`.
- Add macOS firewall notes.
- Verify app from phone on local network.

### 1.4 Secure remote access MVP

- Document Tailscale setup on Mac.
- Document Tailscale setup on phone.
- Enable or document MagicDNS.
- Document remote URL pattern.
- Add basic app-level login before exposing beyond LAN.

---

## Milestone 2: Postgres Persistence

### 2.1 Database setup

- Use Postgres from the beginning.
- Add SQLAlchemy models.
- Add Alembic config.
- Add local Postgres setup instructions.
- Apply initial migration.
- Seed default domains: Personal, Maestro Development, Praxis, Ophi, USMA, Personal IRAD Projects, L3.

### 2.2 Core repositories

- User repository.
- Domain repository.
- Agent repository.
- Conversation repository.
- Message repository.
- Task repository.
- Report repository.
- Artifact repository.
- Tool-call repository.

### 2.3 Agent and task persistence

- Add queued/running/complete/failed/cancelled task statuses.
- Track task parentage for workflows that spawn domain subtasks.
- Track source conversation or scheduled run.
- Persist report references and output payloads.

---

## Milestone 3: Memory Core

### 3.1 Memory model

- Implement global memory.
- Implement Maestro session memory.
- Implement domain memory.
- Implement agent memory.
- Implement raw logs and artifacts as provenance sources.
- Add memory source references to reports, artifacts, tool calls, messages, and seed packages.

### 3.2 Memory service

- Implement scoped memory reads.
- Implement context bundle generation.
- Enforce domain memory isolation for agents.
- Allow Maestro-level retrieval across all domains.
- Add importance and recency filtering.
- Add provenance metadata to retrieved context.
- Make the Memory Manager service the only canonical memory write path.
- Route low-impact writes directly to canonical memory.
- Route medium/high-impact writes through auto-approved audit proposals.
- Queue very-high-impact proposals for user approval before canonical write.

### 3.3 Memory proposals

- Agents can create memory proposals.
- Proposals include scope, domain, agent, memory type, content, rationale, impact level, and source refs.
- Low-impact proposals can be auto-approved by the curator.
- Very high-impact proposals require user approval.
- Rejected proposals remain auditable.

### 3.4 Memory Curator

- Build Memory Curator agent/service.
- Review agent outputs and artifacts.
- Parse domain staging and raw staging inbox items into candidate memories.
- Extract durable memory chunks.
- Write approved canonical memory.
- Queue very-high-impact approval requests.
- Preserve source links for provenance.
- Add reusable LLM integration and an LLM-enabled curator prompt after deterministic plumbing.

### 3.5 Seed package ingestion

- Define raw knowledge package format.
- Ingest docs, decks, readouts, notes, and old AI conversation exports.
- Store seed packages as artifacts.
- Run Memory Curator over seed packages.
- Generate entities, facts, decisions, projects, relationships, preferences, and standing instructions.

### 3.6 Memory hygiene

- Detect duplicate, stale, contradicted, and low-value memories.
- Propose merges, archival, correction, or importance decay.
- Route hygiene changes through the Memory Manager proposal lifecycle.
- Require approval for very-high-impact hygiene changes.

---

## Milestone 4: Agent Runtime

### 4.1 Agent contract

Define a common agent contract:

```python
class Agent:
    key: str
    name: str
    domain: str
    capabilities: list[str]

    async def run(self, task: AgentTask) -> AgentReport:
        ...
```

### 4.2 Agent registry

- Register available agents.
- Store capability metadata.
- Store domain ownership.
- Store allowed tools.
- Store recurring run configuration.
- Support active/inactive agent status.

### 4.3 Task queue MVP

- Start with in-process async task execution.
- Persist task status changes.
- Add cancellation placeholder.
- Add parent/child task relationships.
- Add scheduled task placeholder.

### 4.4 Report schema

- Add title, markdown body, short summary, and structured metadata.
- Add source/tool/memory references.
- Persist every agent output as a report.
- Support synthesis reports generated by Maestro.

---

## Milestone 5: Maestro Orchestrator

### 5.1 Routing MVP

- Accept user request.
- Determine whether request is Maestro-level or domain-specific.
- Determine candidate domain(s) and agent(s).
- Create parent task and domain subtasks when needed.
- Dispatch task(s).
- Return report(s) or task status.

### 5.2 Synthesis MVP

- Combine multiple reports into a single answer.
- Identify cross-domain implications.
- Surface decisions and tradeoffs.
- Save synthesis report.
- Propose global or domain memory updates when appropriate.

### 5.3 Direct agent chat

- Allow user to talk directly to a domain agent.
- Preserve domain and agent memory scoping.
- Still log messages/tasks/reports.
- Still route memory through proposal/curator flow.

---

## Milestone 6: Daily Standup Workflow

### 6.1 Standup workflow shell

- Add Maestro-level Daily Standup workflow.
- Trigger manually from chat or UI button.
- Prepare one parent workflow task.
- Ask each enabled domain for a thin domain brief.
- Collect domain reports.
- Generate one synthesized standup report.

### 6.2 Domain brief contract

Each domain brief should return:

- Schedule items.
- Active tasks.
- Recommended work.
- Blockers.
- Decisions needed.
- Memory or backlog updates suggested by the domain.

### 6.3 Thin MVP domain agents

- Personal Chief of Staff stub.
- Maestro Development CTO/Introspection stub.
- Praxis CGO/CTO stub.
- Ophi Research/Product stub.
- USMA Teaching/Admin stub.
- Personal IRAD Project stub.
- L3 stub.

### 6.4 Standup interaction loop

- User can respond to the standup with progress updates.
- Maestro parses updates into domain-specific follow-up tasks.
- Maestro proposes memory updates through the Memory Curator.
- Maestro can revise the day’s recommended priorities.

---

## Milestone 7: Maestro Development Domain

### 7.1 Self-reflection agent

- Inspect recent reports, user feedback, logs, and open issues.
- Identify system gaps and improvement opportunities.
- Produce recurring Maestro improvement report.

### 7.2 GitHub issue agent

- Convert approved requirements into GitHub issues.
- Read existing Maestro repo issues.
- De-duplicate against existing backlog.
- Apply labels and milestones.

### 7.3 Codex handoff path

- Take approved GitHub issue.
- Create or check out work branch.
- Implement change with Codex.
- Run tests.
- Push branch.
- Open draft PR.
- Report back for user testing and approval.

---

## Milestone 8: First Tool Integrations

### 8.1 GitHub integration

- GitHub App or PAT for MVP.
- Issues read/write.
- PR read summaries.
- Repo status summary.
- Draft PR creation path.

### 8.2 Calendar integration

- Google Calendar OAuth setup.
- Calendar read access.
- Calendar summary tool.
- Use in Daily Standup.

### 8.3 Gmail integration

- OAuth setup.
- Read-only email search.
- Email summarization tool.
- Meeting transcript detection for Praxis Stenographer.

### 8.4 Research integration

- Web search provider.
- arXiv / Semantic Scholar / PubMed as appropriate.
- Source citation capture.
- Research watchlist memory.

### 8.5 ClickUp / CRM integration

- API token setup.
- Read spaces/lists/tasks.
- CRM pipeline summary.
- Update tasks only after explicit approval.

---

## Milestone 9: Praxis Workflows

### 9.1 CGO report

- Research competitors, opportunities, solicitations, news, and sentiment.
- Produce BD opportunity report.
- Ask user to pursue, monitor, or ignore.
- Create tasks from approved actions.

### 9.2 Stenographer

- Detect new meeting recordings or transcript emails.
- Analyze transcript.
- Update CRM candidates.
- Extract tasks.
- Propose memory.
- Generate candidate requirements for CTO review.

### 9.3 New GroundTruth feature

- Detect or receive new GitHub issue.
- Trigger Codex implementation path.
- Generate completion report.
- Ask user to manually test.
- Open PR after approval.

### 9.4 Relationship manager

- Weekly CRM scrub.
- Recommend follow-up meetings.
- Log approved follow-ups as tasks.

### 9.5 Backlog grooming

- Analyze current codebase gaps.
- Analyze requirement candidates from CGO/Stenographer workflows.
- Create and prioritize issues.

---

## Milestone 10: Local Unattended Operation

### 10.1 Run as a service on Mac

- Add startup script.
- Add environment file pattern.
- Add logs directory.
- Add restart instructions.
- Support `launchd`, `tmux`, or Docker Compose path.

### 10.2 Containerization

- Add backend Dockerfile.
- Add frontend Dockerfile.
- Add worker container.
- Add Postgres service.
- Add Redis only when needed for queue maturity.
- Add Docker Compose.

### 10.3 Mini PC deployment path

Recommended order:

1. Prove on Mac.
2. Containerize on Mac.
3. Move to mini PC.
4. Consider Raspberry Pi only if workload is light.

---

## Milestone 11: Safety, Security, and Governance

### 11.1 Access control

- Add user login.
- Add session handling.
- Add domain-level authorization.
- Add admin-only tool configuration.

### 11.2 Tool governance

- Read-only tools by default.
- Human approval required for write actions.
- Log every tool call.
- Store input/output payloads unless sensitive.

### 11.3 Memory governance

- Add memory approval UI for very high-impact memories.
- Add memory edit/delete workflow.
- Add domain separation checks.
- Add provenance display.

### 11.4 Secrets management

- `.env` for local MVP.
- Do not commit secrets.
- Use local keychain or Docker secrets later.
