import {
  Bot,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  GripVertical,
  HardDriveUpload,
  Inbox,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sparkles,
  Wrench,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

type PlannerItem = {
  id: number;
  time: string;
  title: string;
  domain: string;
  status: "locked" | "flex" | "needs-input";
  priority: "high" | "medium" | "low";
};

type DropboxDomain = {
  key: string;
  inbox: number;
  processed: number;
  failed: number;
  previews: number;
};

type MemoryPreview = {
  domain_key: string;
  filename: string;
  source_file: string | null;
  status: string | null;
  generated_at: string | null;
  candidate_count: number;
  written_count: number;
  pending_approval_count: number;
  payload: {
    candidates?: Array<{
      title?: string;
      content?: string;
      impact_level?: string;
      scope?: string;
      memory_type?: string;
    }>;
    results?: Array<{
      outcome?: string;
      memory_item_id?: string | null;
      proposal_id?: string | null;
      proposal_status?: string | null;
    }>;
  };
};

type PreviewResult = NonNullable<MemoryPreview["payload"]["results"]>[number];

type PendingProposal = {
  id: string;
  scope: string;
  memory_type: string;
  title: string;
  content: string;
  rationale: string | null;
  impact_level: string;
  status: string;
  created_at: string | null;
};

type MemoryItem = {
  id: string;
  scope: string;
  memory_type: string;
  title: string;
  content: string;
  impact_level: string;
  importance: number;
  created_at: string | null;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

const domainLabels: Record<string, string> = {
  global: "Global",
  personal: "Personal",
  "maestro-development": "Maestro Development",
  praxis: "Praxis",
  ophi: "Ophi",
  usma: "USMA",
  "personal-irad-projects": "Personal IRAD",
  l3: "L3",
};

const dropboxDomainDefaults: DropboxDomain[] = Object.keys(domainLabels).map((key) => ({
  key,
  inbox: 0,
  processed: 0,
  failed: 0,
  previews: 0,
}));

const domains = [
  "Personal",
  "Maestro Development",
  "Praxis",
  "Ophi",
  "USMA",
  "Personal IRAD",
  "L3",
];

const initialPlannerItems: PlannerItem[] = [
  {
    id: 1,
    time: "08:00",
    title: "Daily standup synthesis",
    domain: "Maestro",
    status: "locked",
    priority: "high",
  },
  {
    id: 2,
    time: "09:30",
    title: "Review Praxis follow-up candidates",
    domain: "Praxis",
    status: "needs-input",
    priority: "high",
  },
  {
    id: 3,
    time: "11:00",
    title: "USMA prep block",
    domain: "USMA",
    status: "flex",
    priority: "medium",
  },
  {
    id: 4,
    time: "14:00",
    title: "Maestro implementation window",
    domain: "Maestro Development",
    status: "flex",
    priority: "high",
  },
];

const reports = [
  {
    title: "Morning standup",
    summary: "Awaiting first workflow run.",
    meta: "Maestro / Today",
  },
  {
    title: "Memory curator",
    summary: "Seed package ingestion not configured yet.",
    meta: "Admin / Pending",
  },
  {
    title: "Praxis brief",
    summary: "Domain agent stub will populate this panel.",
    meta: "Praxis / Stub",
  },
];

const agents = [
  "Personal Chief of Staff",
  "Maestro CTO",
  "Praxis CGO",
  "Ophi Research",
  "USMA Teaching",
  "IRAD Project Planner",
];

function statusLabel(status: PlannerItem["status"]) {
  if (status === "locked") return "Locked";
  if (status === "needs-input") return "Needs input";
  return "Flexible";
}

function nextStatus(status: PlannerItem["status"]): PlannerItem["status"] {
  if (status === "locked") return "flex";
  if (status === "flex") return "needs-input";
  return "locked";
}

function resultLabel(result?: PreviewResult) {
  if (!result) return "Preview only";
  if (result.memory_item_id) return "Written to memory";
  if (result.outcome === "pending_user_approval") return "Needs approval";
  if (result.proposal_status) return `Proposal ${result.proposal_status}`;
  return result.outcome ?? "Processed";
}

function resultClass(result?: PreviewResult) {
  if (!result) return "preview-only";
  if (result.memory_item_id) return "written";
  if (result.outcome === "pending_user_approval") return "pending";
  return "processed";
}

function previewTime(preview: MemoryPreview) {
  const time = preview.generated_at ? Date.parse(preview.generated_at) : 0;
  return Number.isFinite(time) ? time : 0;
}

async function apiJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activeDomain, setActiveDomain] = useState("Maestro");
  const [activeSurface, setActiveSurface] = useState<"dashboard" | "memory">("dashboard");
  const [plannerItems, setPlannerItems] = useState(initialPlannerItems);
  const [draftMessage, setDraftMessage] = useState("");

  const highPriorityCount = useMemo(
    () => plannerItems.filter((item) => item.priority === "high").length,
    [plannerItems],
  );

  const moveItem = (id: number, direction: -1 | 1) => {
    setPlannerItems((items) => {
      const index = items.findIndex((item) => item.id === id);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= items.length) return items;
      const next = [...items];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const cycleItemStatus = (id: number) => {
    setPlannerItems((items) =>
      items.map((item) => (item.id === id ? { ...item, status: nextStatus(item.status) } : item)),
    );
  };

  return (
    <main className="app-shell">
      <aside className={sidebarOpen ? "sidebar" : "sidebar sidebar-closed"}>
        <div className="brand-row">
          <div>
            <p className="eyebrow">Local command</p>
            <h1>Maestro</h1>
          </div>
          <button
            className="icon-button"
            aria-label={sidebarOpen ? "Collapse sidebar" : "Open sidebar"}
            title={sidebarOpen ? "Collapse sidebar" : "Open sidebar"}
            onClick={() => setSidebarOpen((value) => !value)}
          >
            {sidebarOpen ? <PanelLeftClose size={18} /> : <Menu size={18} />}
          </button>
        </div>

        <nav className="domain-nav" aria-label="Domains">
          <button
            className={activeDomain === "Maestro" ? "domain-button active" : "domain-button"}
            onClick={() => {
              setActiveDomain("Maestro");
              setActiveSurface("dashboard");
            }}
          >
            <Sparkles size={17} />
            <span>Maestro</span>
          </button>
          <button
            className={activeSurface === "memory" ? "domain-button active" : "domain-button"}
            onClick={() => setActiveSurface("memory")}
          >
            <Database size={17} />
            <span>Memory</span>
          </button>
          {domains.map((domain) => (
            <button
              key={domain}
              className={activeDomain === domain ? "domain-button active" : "domain-button"}
              onClick={() => {
                setActiveDomain(domain);
                setActiveSurface("dashboard");
              }}
            >
              <ChevronRight size={16} />
              <span>{domain}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button className="domain-button">
            <Settings size={17} />
            <span>Settings</span>
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Active surface</p>
            <h2>{activeSurface === "memory" ? "Memory" : activeDomain}</h2>
          </div>
          <div className="status-strip" aria-label="Runtime status">
            <span>
              <ShieldCheck size={16} />
              Local
            </span>
            {activeSurface === "memory" ? (
              <span>
                <Database size={16} />
                Memory pipeline
              </span>
            ) : (
              <span>
                <Clock3 size={16} />
                {highPriorityCount} high priority
              </span>
            )}
          </div>
        </header>

        {activeSurface === "memory" ? (
          <MemoryWorkspace />
        ) : (
          <div className="workspace-grid">
            <section className="chat-panel" aria-labelledby="chat-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Maestro chat</p>
                  <h3 id="chat-heading">Command thread</h3>
                </div>
                <button className="icon-button" aria-label="New thread" title="New thread">
                  <Plus size={18} />
                </button>
              </div>

              <div className="thread">
                <div className="message user-message">
                  <span>You</span>
                  <p>Build today around the morning standup and keep the plan adjustable.</p>
                </div>
                <div className="message maestro-message">
                  <span>Maestro</span>
                  <p>
                    Daily planner is ready as a stub. The standup workflow will populate this with
                    schedule, tasks, blockers, and recommended tradeoffs.
                  </p>
                </div>
              </div>

              <form className="composer" onSubmit={(event) => event.preventDefault()}>
                <MessageSquareText size={18} />
                <input
                  value={draftMessage}
                  onChange={(event) => setDraftMessage(event.target.value)}
                  placeholder="Tell Maestro what changed..."
                  aria-label="Message Maestro"
                />
                <button type="submit">Send</button>
              </form>
            </section>

            <section className="planner-panel" aria-labelledby="planner-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Daily planner</p>
                  <h3 id="planner-heading">Today</h3>
                </div>
                <button className="planner-action">
                  <CalendarDays size={17} />
                  Adjust
                </button>
              </div>

              <div className="timeline">
                {plannerItems.map((item, index) => (
                  <article className="timeline-item" key={item.id}>
                    <div className="time-column">
                      <span>{item.time}</span>
                      <GripVertical size={16} />
                    </div>
                    <div className="timeline-body">
                      <div className="timeline-title-row">
                        <div>
                          <h4>{item.title}</h4>
                          <p>{item.domain}</p>
                        </div>
                        <button
                          className={`status-pill ${item.status}`}
                          onClick={() => cycleItemStatus(item.id)}
                        >
                          {statusLabel(item.status)}
                        </button>
                      </div>
                      <div className="timeline-controls">
                        <button onClick={() => moveItem(item.id, -1)} disabled={index === 0}>
                          Earlier
                        </button>
                        <button
                          onClick={() => moveItem(item.id, 1)}
                          disabled={index === plannerItems.length - 1}
                        >
                          Later
                        </button>
                        <span className={`priority priority-${item.priority}`}>
                          {item.priority} priority
                        </span>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            </section>

            <section className="reports-panel" aria-labelledby="reports-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Recent reports</p>
                  <h3 id="reports-heading">Queue</h3>
                </div>
                <button className="icon-button" aria-label="Report menu" title="Report menu">
                  <MoreHorizontal size={18} />
                </button>
              </div>
              <div className="report-list">
                {reports.map((report) => (
                  <article className="report-card" key={report.title}>
                    <span>{report.meta}</span>
                    <h4>{report.title}</h4>
                    <p>{report.summary}</p>
                  </article>
                ))}
              </div>
            </section>

            <section className="domain-panel" aria-labelledby="domain-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Domain page</p>
                  <h3 id="domain-heading">Agents & tools</h3>
                </div>
                <button className="icon-button" aria-label="Add agent" title="Add agent">
                  <Bot size={18} />
                </button>
              </div>

              <div className="agent-list">
                {agents.map((agent) => (
                  <button className="agent-row" key={agent}>
                    <span>
                      <Bot size={17} />
                      {agent}
                    </span>
                    <CheckCircle2 size={17} />
                  </button>
                ))}
              </div>

              <div className="tool-shell">
                <div>
                  <Wrench size={18} />
                  <span>Tool credentials and descriptions will open here.</span>
                </div>
                <button className="planner-action">Configure</button>
              </div>
            </section>
          </div>
        )}
      </section>
    </main>
  );
}

function MemoryWorkspace() {
  const [domains, setDomains] = useState<DropboxDomain[]>(dropboxDomainDefaults);
  const [selectedDomain, setSelectedDomain] = useState("ophi");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previews, setPreviews] = useState<MemoryPreview[]>([]);
  const [pending, setPending] = useState<PendingProposal[]>([]);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [selectedPreviewFilename, setSelectedPreviewFilename] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [lastProcessSummary, setLastProcessSummary] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshMemory = useCallback(async () => {
    const [status, previewResponse, pendingResponse, itemResponse] = await Promise.all([
      apiJson<{ domains: DropboxDomain[] }>("/memory/dropbox/status"),
      apiJson<{ previews: MemoryPreview[] }>("/memory/dropbox/previews"),
      apiJson<{ proposals: PendingProposal[] }>("/memory/proposals/pending"),
      apiJson<{ items: MemoryItem[] }>("/memory/items?limit=8"),
    ]);
    setDomains(status.domains);
    const sortedPreviews = [...previewResponse.previews].sort(
      (first, second) => previewTime(second) - previewTime(first),
    );
    setPreviews(sortedPreviews.slice(0, 10));
    setPending(pendingResponse.proposals);
    setItems(itemResponse.items);
    if (!status.domains.some((domain) => domain.key === selectedDomain)) {
      setSelectedDomain(status.domains[0]?.key ?? "global");
    }
  }, [selectedDomain]);

  useEffect(() => {
    refreshMemory().catch((error) =>
      setStatusMessage(
        `Unable to reach the Memory API at ${API_BASE_URL}. ${
          error instanceof Error ? error.message : "Check that the backend is running."
        }`,
      ),
    );
  }, [refreshMemory]);

  const uploadFile = async () => {
    if (!selectedFile) {
      setStatusMessage("Choose a file first.");
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", selectedFile);
      await apiJson(`/memory/dropbox/${selectedDomain}/upload`, {
        method: "POST",
        body: form,
      });
      setSelectedFile(null);
      setStatusMessage(`Uploaded ${selectedFile.name} to ${selectedDomain}.`);
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const processInbox = async () => {
    setBusy(true);
    try {
      const result = await apiJson<{ processed: number }>("/memory/dropbox/process", {
        method: "POST",
      });
      setLastProcessSummary(
        `Processed ${result.processed} file${result.processed === 1 ? "" : "s"}.`,
      );
      setStatusMessage("Processing complete. Review preview, recent writes, and approval queue.");
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Processing failed.");
    } finally {
      setBusy(false);
    }
  };

  const decideProposal = async (proposalId: string, action: "approve" | "reject") => {
    setBusy(true);
    try {
      await apiJson(`/memory/proposals/${proposalId}/${action}`, {
        method: "POST",
        headers: action === "reject" ? { "Content-Type": "application/json" } : undefined,
        body:
          action === "reject"
            ? JSON.stringify({ reason: "Rejected in memory review UI." })
            : undefined,
      });
      setStatusMessage(action === "approve" ? "Memory approved." : "Memory rejected.");
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Approval action failed.");
    } finally {
      setBusy(false);
    }
  };

  const selectedDomainStatus = domains.find((domain) => domain.key === selectedDomain);
  const latestPreview =
    previews.find((preview) => preview.filename === selectedPreviewFilename) ?? previews[0];

  return (
    <div className="memory-grid">
      <section className="memory-panel memory-upload-panel" aria-labelledby="memory-upload-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Memory dropbox</p>
            <h3 id="memory-upload-heading">Staging</h3>
          </div>
          <button className="icon-button" onClick={refreshMemory} title="Refresh memory data">
            <RefreshCw size={18} />
          </button>
        </div>

        <div className="memory-controls">
          <label>
            Domain
            <select
              value={selectedDomain}
              onChange={(event) => setSelectedDomain(event.target.value)}
            >
              {domains.map((domain) => (
                <option key={domain.key} value={domain.key}>
                  {domainLabels[domain.key] ?? domain.key}
                </option>
              ))}
            </select>
          </label>
          <label className="file-drop">
            <HardDriveUpload size={20} />
            <span>{selectedFile ? selectedFile.name : "Choose PDF, DOCX, Markdown, text, or data"}</span>
            <input
              type="file"
              accept=".pdf,.docx,.md,.txt,.json,.csv,.tsv,.html,.htm"
              onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
            />
          </label>
        </div>

        <div className="memory-actions">
          <button className="planner-action" onClick={uploadFile} disabled={busy || !selectedFile}>
            <Inbox size={17} />
            Upload
          </button>
          <button className="planner-action" onClick={processInbox} disabled={busy}>
            <Sparkles size={17} />
            {busy ? "Working..." : "Process inbox"}
          </button>
        </div>

        {busy && (
          <div className="activity-row" role="status" aria-live="polite">
            <span className="activity-dot" />
            Processing memory pipeline
          </div>
        )}

        <div className="dropbox-stats">
          <span>Inbox {selectedDomainStatus?.inbox ?? 0}</span>
          <span>Processed {selectedDomainStatus?.processed ?? 0}</span>
          <span>Failed {selectedDomainStatus?.failed ?? 0}</span>
          <span>Previews {selectedDomainStatus?.previews ?? 0}</span>
        </div>
        {lastProcessSummary && <p className="memory-status">{lastProcessSummary}</p>}
        <p className="memory-status">{statusMessage}</p>
      </section>

      <section className="memory-panel" aria-labelledby="memory-approval-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">High impact</p>
            <h3 id="memory-approval-heading">Approval queue</h3>
          </div>
          <span className="count-badge">{pending.length}</span>
        </div>

        <div className="approval-list">
          {pending.length === 0 ? (
            <p className="empty-state">No pending high-impact memories.</p>
          ) : (
            pending.map((proposal) => (
              <article className="approval-card" key={proposal.id}>
                <div>
                  <span>
                    {proposal.scope} / {proposal.memory_type}
                  </span>
                  <h4>{proposal.title}</h4>
                  <p>{proposal.content}</p>
                </div>
                <div className="approval-actions">
                  <button onClick={() => decideProposal(proposal.id, "approve")} disabled={busy}>
                    Approve
                  </button>
                  <button onClick={() => decideProposal(proposal.id, "reject")} disabled={busy}>
                    Reject
                  </button>
                </div>
              </article>
            ))
          )}
        </div>
      </section>

      <section className="memory-panel" aria-labelledby="memory-preview-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Debug preview</p>
            <h3 id="memory-preview-heading">Latest extraction</h3>
          </div>
          <FileText size={18} />
        </div>

        {latestPreview ? (
          <div className="preview-shell">
            <div className="preview-meta">
              <span>{latestPreview.domain_key}</span>
              <span>{latestPreview.status}</span>
              <span>{latestPreview.candidate_count} candidates</span>
              <span>{latestPreview.written_count} written</span>
              <span>{latestPreview.pending_approval_count} pending approval</span>
            </div>
            {previews.length > 1 && (
              <div className="preview-picker" aria-label="Preview history">
                {previews.map((preview) => (
                  <button
                    key={preview.filename}
                    className={
                      preview.filename === latestPreview.filename
                        ? "preview-choice active"
                        : "preview-choice"
                    }
                    onClick={() => setSelectedPreviewFilename(preview.filename)}
                  >
                    {preview.source_file ?? preview.filename}
                  </button>
                ))}
              </div>
            )}
            <h4>{latestPreview.source_file}</h4>
            <div className="candidate-list">
              {(latestPreview.payload.candidates ?? []).map((candidate, index) => (
                <article className="candidate-row" key={`${candidate.title}-${index}`}>
                  <span>
                    {candidate.scope} / {candidate.memory_type} / {candidate.impact_level}
                  </span>
                  <h4>{candidate.title}</h4>
                  <p>{candidate.content}</p>
                  <div
                    className={`result-pill ${resultClass(latestPreview.payload.results?.[index])}`}
                  >
                    {resultLabel(latestPreview.payload.results?.[index])}
                  </div>
                </article>
              ))}
            </div>
          </div>
        ) : (
          <p className="empty-state">Process a file to see extracted candidates.</p>
        )}
      </section>

      <section className="memory-panel" aria-labelledby="memory-recent-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Canonical memory</p>
            <h3 id="memory-recent-heading">Recent writes</h3>
          </div>
          <Database size={18} />
        </div>
        <div className="memory-list">
          {items.map((item) => (
            <article className="memory-row" key={item.id}>
              <span>{item.scope} / {item.memory_type} / {item.impact_level}</span>
              <h4>{item.title}</h4>
              <p>{item.content}</p>
            </article>
          ))}
          {items.length === 0 && <p className="empty-state">No memory has been written yet.</p>}
        </div>
      </section>
    </div>
  );
}
