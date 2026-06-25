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
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Trash2,
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

type ChatMessage = {
  id: string;
  sender: "user" | "maestro";
  content: string;
};

type MaestroSessionSummary = {
  id: string;
  title: string;
  messages: ChatMessage[];
  stagedArtifactPath: string | null;
};

type DropboxDomain = {
  key: string;
  inbox: number;
  processing: number;
  processed: number;
  failed: number;
  previews: number;
};

type MemoryPreview = {
  domain_key: string;
  filename: string;
  source_file: string | null;
  status: string | null;
  is_processing: boolean;
  generated_at: string | null;
  candidate_count: number;
  result_count: number;
  written_count: number;
  deduped_count: number;
  pending_approval_count: number;
  progress_count: number;
  progress_total: number;
  routed_count: number;
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
      related_memory_id?: string | null;
      evaluation?: {
        decision?: string;
        confidence?: number;
        rationale?: string | null;
        related_memory_id?: string | null;
      };
    }>;
    routed_items?: Array<{
      route_type?: string;
      title?: string;
      content?: string;
      priority?: string;
      status?: string;
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

type MemorySource = {
  id: string;
  name: string;
  status: string;
  domain_key: string;
  memory_count: number;
  proposal_count: number;
  processed_at: string | null;
};

type RoutedItem = {
  id: string;
  domain_key: string | null;
  route_type: string;
  title: string;
  content: string;
  priority: string;
  status: string;
  source_refs: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

type RetrievedMemory = MemoryItem & {
  domain_key: string;
  agent_id: string | null;
  score: number;
  query_relevance: number;
  semantic_similarity: number | null;
  score_reasons: string[];
  provenance: {
    source_refs: Array<Record<string, unknown>>;
    seed_package: { id: string; name: string; source_type: string; status: string } | null;
    artifact: {
      id: string;
      name: string;
      artifact_type: string;
      uri: string;
      mime_type: string | null;
    } | null;
    processed_path: string | null;
  };
  links: Array<{
    relation_type: string;
    direction: string;
    memory: MemoryItem & { domain_key: string };
  }>;
};

type AgentTool = {
  key: string;
  name: string;
  permission: string;
  description: string;
  connection_id: string | null;
  auth_type: string | null;
};

type AgentSpec = {
  id: string;
  key: string;
  name: string;
  domain_key: string;
  agent_type: string;
  role_summary: string;
  role_prompt: string;
  memory_profile: string;
  model_profile: string;
  allowed_tools: AgentTool[];
  is_active: boolean;
  current_action: string | null;
  scheduled_actions: Array<Record<string, unknown>>;
};

type AgentTask = {
  id: string;
  status: string;
  priority: string;
  source_type: string;
  workflow_key: string | null;
  objective: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
};

type DomainContext = {
  id: string;
  key: string;
  name: string;
  context: string;
  is_active: boolean;
};

type ToolRegistryItem = {
  key: string;
  name: string;
  description: string;
  exclusive: boolean;
  connected_domains: string[];
  authorized_agents: Array<{
    agent_key: string;
    agent_name: string;
    domain_key: string;
    permission: string;
  }>;
};

type ToolConnection = {
  id: string;
  domain_key: string;
  tool_key: string;
  display_name: string;
  auth_type: string;
  config: Record<string, unknown>;
  is_active: boolean;
};

type PromptPackage = {
  assembled_prompt: string;
  memory_context: {
    included_count: number;
    semantic_status: string;
  };
};

type AgentRun = {
  run_id: string;
  status: string;
  execution_note: string;
  output_text: string | null;
  task_id: string | null;
  report_id: string | null;
  error_message: string | null;
  tool_calls?: Array<{
    id: string;
    tool_name: string;
    status: string;
    error_message: string | null;
  }>;
  scheduler?: {
    status: string;
    reason: string;
  };
  prompt_package: PromptPackage;
  staged_artifact_path: string | null;
};

type MaestroIntent = {
  type: string;
  summary: string;
  target: string;
  domain_key: string | null;
  priority: string;
  action: string | null;
};

type MaestroSubtask = {
  agent_key: string;
  agent_name: string;
  domain_key: string;
  objective: string;
  expected_output: string;
  priority: string;
  rationale: string | null;
  work_item_ids: string[] | null;
  depends_on_work_item_ids: string[] | null;
};

type MaestroWorkItem = {
  id: string;
  type: string;
  title: string;
  description: string;
  domain_key: string | null;
  priority: string;
  required_capabilities: string[];
  required_tools: string[];
  dependencies: string[];
  needs_agent: boolean;
  needs_user_input: boolean;
  blocks_execution: boolean;
  can_log_directly: boolean;
  suggested_agent_keys: string[];
  expected_output: string;
  rationale: string;
};

type MaestroPlan = {
  plan_id: string;
  parent_task_id: string;
  status: string;
  user_input: string;
  summary: string;
  execution_mode: string;
  planner_mode: string;
  work_items: MaestroWorkItem[];
  intents: MaestroIntent[];
  subtasks: MaestroSubtask[];
  execution_stages: string[][];
  is_chat_only: boolean;
  selected_agents: Array<Record<string, unknown>>;
  approval_required: boolean;
  scheduler: Record<string, unknown>;
  created_at: string;
  direct_response: string | null;
  planner_notes: string | null;
};

type MaestroRun = {
  plan: MaestroPlan;
  status: string;
  parent_task_id: string;
  synthesis_report_id: string | null;
  synthesis: string;
  staged_artifact_path: string | null;
  artifact_id: string | null;
  error_message: string | null;
  execution_stages: string[][];
  child_runs: Array<{
    run_id: string;
    status: string;
    agent: {
      key: string;
      name: string;
      domain_key: string;
    };
    task_id: string | null;
    report_id: string | null;
    execution_note: string;
    output_text: string | null;
    error_message: string | null;
  }>;
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
  processing: 0,
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

const domainKeysByLabel: Record<string, string> = Object.fromEntries(
  Object.entries(domainLabels).map(([key, label]) => [label, key]),
);

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

const routedGroups = [
  { key: "human_input", label: "RFIs", empty: "No open RFIs." },
  { key: "task", label: "Tasks", empty: "No open tasks." },
  { key: "event", label: "Events", empty: "No extracted events." },
  { key: "contact", label: "Contacts", empty: "No extracted contacts." },
  { key: "decision_log", label: "Decisions", empty: "No recent decisions." },
  { key: "think_tank", label: "Think Tank", empty: "No think tank notes." },
];

const hiddenRoutedStatuses = new Set(["done", "archived"]);

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
  if (result.outcome === "duplicate_skipped") return "Duplicate skipped";
  if (result.outcome === "reinforced") return "Reinforced existing memory";
  if (result.outcome === "rejected") return "Rejected by memory manager";
  if (result.outcome === "pending_user_approval") return "Needs approval";
  if (result.proposal_status) return `Proposal ${result.proposal_status}`;
  return result.outcome ?? "Processed";
}

function resultClass(result?: PreviewResult) {
  if (!result) return "preview-only";
  if (result.memory_item_id) return "written";
  if (result.outcome === "duplicate_skipped" || result.outcome === "reinforced") return "deduped";
  if (result.outcome === "rejected") return "rejected";
  if (result.outcome === "pending_user_approval") return "pending";
  return "processed";
}

function candidateResultLabel(preview: MemoryPreview, index: number) {
  const result = preview.payload.results?.[index];
  if (result) return resultLabel(result);
  if (preview.is_processing) return "Queued for write";
  return "Preview only";
}

function candidateResultClass(preview: MemoryPreview, index: number) {
  const result = preview.payload.results?.[index];
  if (result) return resultClass(result);
  if (preview.is_processing) return "processing";
  return "preview-only";
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

function RoutedItemsBoard({
  domainKey,
  title,
  eyebrow,
  className = "",
}: {
  domainKey?: string;
  title: string;
  eyebrow: string;
  className?: string;
}) {
  const [items, setItems] = useState<RoutedItem[]>([]);
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [busyItemId, setBusyItemId] = useState<string | null>(null);
  const headingId = `${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-heading`;

  const refreshItems = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100", status: "all" });
    if (domainKey) params.set("domain_key", domainKey);
    const response = await apiJson<{ items: RoutedItem[] }>(`/memory/routed-items?${params}`);
    setItems(response.items.filter((item) => !hiddenRoutedStatuses.has(item.status)));
    setStatusMessage("Ready");
  }, [domainKey]);

  useEffect(() => {
    refreshItems().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : "Unable to load routed items."),
    );
  }, [refreshItems]);

  const updateStatus = async (itemId: string, status: "done" | "archived") => {
    setBusyItemId(itemId);
    try {
      await apiJson(`/memory/routed-items/${itemId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status,
          reason: `${status === "done" ? "Completed" : "Archived"} from routed-item board.`,
        }),
      });
      setStatusMessage(status === "done" ? "Item marked done." : "Item archived.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Routed item update failed.");
    } finally {
      setBusyItemId(null);
    }
  };

  return (
    <section className={`memory-panel routed-board ${className}`} aria-labelledby={headingId}>
      <div className="section-heading">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h3 id={headingId}>{title}</h3>
        </div>
        <button className="icon-button" onClick={refreshItems} title="Refresh routed items">
          <RefreshCw size={18} />
        </button>
      </div>

      <div className="routed-summary">
        {routedGroups.map((group) => (
          <span key={group.key}>
            {group.label} {items.filter((item) => item.route_type === group.key).length}
          </span>
        ))}
      </div>

      <div className="routed-grid">
        {routedGroups.map((group) => {
          const groupItems = items.filter((item) => item.route_type === group.key);
          return (
            <section className="routed-column" key={group.key} aria-label={group.label}>
              <div className="routed-column-heading">
                <h4>{group.label}</h4>
                <span>{groupItems.length}</span>
              </div>
              <div className="routed-list">
                {groupItems.map((item) => (
                  <article className="routed-card" key={item.id}>
                    <span>
                      {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"} /{" "}
                      {item.priority}
                    </span>
                    <h4>{item.title}</h4>
                    <p>{item.content}</p>
                    <div className="routed-actions">
                      <button
                        className="planner-action"
                        onClick={() => updateStatus(item.id, "done")}
                        disabled={busyItemId === item.id}
                      >
                        <CheckCircle2 size={16} />
                        Done
                      </button>
                      <button
                        className="danger-action"
                        onClick={() => updateStatus(item.id, "archived")}
                        disabled={busyItemId === item.id}
                      >
                        <Trash2 size={16} />
                        Archive
                      </button>
                    </div>
                  </article>
                ))}
                {groupItems.length === 0 && <p className="empty-state">{group.empty}</p>}
              </div>
            </section>
          );
        })}
      </div>
      <p className="memory-status">{statusMessage}</p>
    </section>
  );
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activeDomain, setActiveDomain] = useState("Maestro");
  const [activeSurface, setActiveSurface] = useState<"dashboard" | "domain" | "memory" | "tools">(
    "dashboard",
  );
  const [plannerItems, setPlannerItems] = useState(initialPlannerItems);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [sessionHistory, setSessionHistory] = useState<MaestroSessionSummary[]>([]);
  const [draftMessage, setDraftMessage] = useState("");
  const [maestroPlan, setMaestroPlan] = useState<MaestroPlan | null>(null);
  const [maestroRun, setMaestroRun] = useState<MaestroRun | null>(null);
  const [maestroStatus, setMaestroStatus] = useState("Ready");
  const [executeMaestroLLM, setExecuteMaestroLLM] = useState(true);
  const [maestroBusy, setMaestroBusy] = useState(false);

  const highPriorityCount = useMemo(
    () => plannerItems.filter((item) => item.priority === "high").length,
    [plannerItems],
  );

  const maestroPlanStages = useMemo(() => {
    if (!maestroPlan) return [];
    const executionStages = maestroPlan.execution_stages ?? [];
    if (!executionStages.length) return [maestroPlan.subtasks];
    const unassigned = [...maestroPlan.subtasks];
    return executionStages
      .map((stage) =>
        stage
          .map((agentKey) => {
            const index = unassigned.findIndex((subtask) => subtask.agent_key === agentKey);
            if (index < 0) return null;
            const [subtask] = unassigned.splice(index, 1);
            return subtask;
          })
          .filter((subtask): subtask is MaestroSubtask => subtask !== null),
      )
      .filter((stage) => stage.length > 0);
  }, [maestroPlan]);

  const buildMaestroPlanResponse = (plan: MaestroPlan, refined: boolean) => {
    if (plan.is_chat_only) {
      return plan.direct_response || plan.summary || "I can handle that directly here.";
    }
    const blockingItems = plan.work_items.filter((item) => item.needs_user_input && item.blocks_execution);
    const nonBlockingQuestions = plan.work_items.filter(
      (item) => item.needs_user_input && !item.blocks_execution,
    );
    if (blockingItems.length > 0) {
      return `I need one answer before this can run: ${blockingItems
        .map((item) => item.title)
        .join("; ")}. I updated the proposed plan and will refine it when you answer.`;
    }
    const stageText =
      plan.execution_stages.length === 1
        ? "1 stage"
        : `${plan.execution_stages.length || 1} stages`;
    const questionText =
      nonBlockingQuestions.length > 0
        ? ` I also found ${nonBlockingQuestions.length} non-blocking question${
            nonBlockingQuestions.length === 1 ? "" : "s"
          } that can be answered later.`
        : "";
    return `${refined ? "I refined the plan" : "I drafted a plan"} with ${
      plan.work_items.length
    } work items, ${plan.subtasks.length} subtasks, and ${stageText}.${questionText} It is ready for review.`;
  };

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

  const sendMaestroMessage = async () => {
    if (!draftMessage.trim()) return;
    const outgoingMessage: ChatMessage = {
      id: crypto.randomUUID(),
      sender: "user",
      content: draftMessage.trim(),
    };
    const shouldRefine = maestroPlan !== null && maestroRun === null;
    setMaestroBusy(true);
    setChatMessages((messages) => [...messages, outgoingMessage]);
    setDraftMessage("");
    try {
      const response = await apiJson<{ plan: MaestroPlan }>(
        shouldRefine ? `/maestro/plans/${maestroPlan.parent_task_id}/refine` : "/maestro/plan",
        {
        method: "POST",
        headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: outgoingMessage.content }),
        },
      );
      setMaestroPlan(response.plan.is_chat_only ? null : response.plan);
      setMaestroRun(null);
      setChatMessages((messages) => [
        ...messages,
        {
          id: crypto.randomUUID(),
          sender: "maestro",
          content: buildMaestroPlanResponse(response.plan, shouldRefine),
        },
      ]);
      setMaestroStatus(shouldRefine ? "Plan refined." : "Proposed plan ready for approval.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Maestro planning failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: crypto.randomUUID(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setMaestroBusy(false);
    }
  };

  const runMaestroPlan = async () => {
    if (!maestroPlan) return;
    setMaestroBusy(true);
    try {
      const response = await apiJson<{ run: MaestroRun }>(
        `/maestro/plans/${maestroPlan.parent_task_id}/run`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ execute_llm: executeMaestroLLM }),
        },
      );
      setMaestroRun(response.run);
      setMaestroPlan(response.run.plan);
      setChatMessages((messages) => [
        ...messages,
        {
          id: crypto.randomUUID(),
          sender: "maestro",
          content:
            response.run.status === "completed"
              ? "The workflow completed. I synthesized the child agent outputs and staged the canonical workflow artifact for memory curation."
              : `The workflow finished with status ${response.run.status}. Review the synthesis below.`,
        },
      ]);
      setMaestroStatus(
        response.run.status === "completed" ? "Workflow completed." : "Workflow finished.",
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "Maestro workflow failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: crypto.randomUUID(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setMaestroBusy(false);
    }
  };

  const startNewMaestroSession = async () => {
    if (chatMessages.length === 0 && !maestroPlan && !maestroRun) return;
    setMaestroBusy(true);
    let stagedArtifactPath: string | null = null;
    try {
      if (chatMessages.length > 0) {
        const response = await apiJson<{ staged_artifact_path: string | null }>(
          "/maestro/sessions/close",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              plan_id: maestroPlan?.parent_task_id ?? null,
              messages: chatMessages.map((message) => ({
                sender: message.sender,
                content: message.content,
              })),
            }),
          },
        );
        stagedArtifactPath = response.staged_artifact_path;
      }
      setSessionHistory((sessions) => [
        {
          id: crypto.randomUUID(),
          title: chatMessages.find((message) => message.sender === "user")?.content.slice(0, 72) ||
            "Maestro session",
          messages: chatMessages,
          stagedArtifactPath,
        },
        ...sessions,
      ]);
      setChatMessages([]);
      setDraftMessage("");
      setMaestroPlan(null);
      setMaestroRun(null);
      setMaestroStatus(
        stagedArtifactPath
          ? "Previous session staged for memory curation."
          : "New Maestro session ready.",
      );
    } catch (error) {
      setMaestroStatus(error instanceof Error ? error.message : "Could not close session.");
    } finally {
      setMaestroBusy(false);
    }
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
          <button
            className={activeSurface === "tools" ? "domain-button active" : "domain-button"}
            onClick={() => setActiveSurface("tools")}
          >
            <Wrench size={17} />
            <span>Tools</span>
          </button>
          {domains.map((domain) => (
            <button
              key={domain}
              className={activeDomain === domain ? "domain-button active" : "domain-button"}
              onClick={() => {
                setActiveDomain(domain);
                setActiveSurface("domain");
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
            <h2>
              {activeSurface === "memory"
                ? "Memory"
                : activeSurface === "tools"
                  ? "Tools"
                  : activeDomain}
            </h2>
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
            ) : activeSurface === "tools" ? (
              <span>
                <Wrench size={16} />
                Shared tool suite
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
        ) : activeSurface === "tools" ? (
          <ToolsWorkspace />
        ) : activeSurface === "domain" ? (
          <DomainWorkspace domainLabel={activeDomain} />
        ) : (
          <div className="workspace-grid">
            <section className="chat-panel" aria-labelledby="chat-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Maestro chat</p>
                  <h3 id="chat-heading">Command thread</h3>
                </div>
                <button
                  className="icon-button"
                  aria-label="New session"
                  title="New session"
                  onClick={startNewMaestroSession}
                  disabled={maestroBusy || (chatMessages.length === 0 && !maestroPlan && !maestroRun)}
                >
                  <Plus size={18} />
                </button>
              </div>

              <div className="thread">
                {chatMessages.length > 0 ? (
                  chatMessages.map((message) => (
                    <div
                      className={`message ${
                        message.sender === "user" ? "user-message" : "maestro-message"
                      }`}
                      key={message.id}
                    >
                      <span>{message.sender === "user" ? "You" : "Maestro"}</span>
                      <p>{message.content}</p>
                    </div>
                  ))
                ) : (
                  <p className="empty-state">
                    No active Maestro session yet. Send a request to start a plan or conversation.
                  </p>
                )}
              </div>

              {sessionHistory.length > 0 && (
                <div className="session-history" aria-label="Previous Maestro sessions">
                  <span>Previous sessions</span>
                  {sessionHistory.slice(0, 3).map((session) => (
                    <button
                      type="button"
                      key={session.id}
                      onClick={() => {
                        setChatMessages(session.messages);
                        setMaestroPlan(null);
                        setMaestroRun(null);
                        setMaestroStatus(
                          session.stagedArtifactPath
                            ? "Viewing previous session. Artifact was staged for memory curation."
                            : "Viewing previous session.",
                        );
                      }}
                    >
                      {session.title}
                    </button>
                  ))}
                </div>
              )}

              <form
                className="composer"
                onSubmit={(event) => {
                  event.preventDefault();
                  sendMaestroMessage();
                }}
              >
                <MessageSquareText size={18} />
                <input
                  value={draftMessage}
                  onChange={(event) => setDraftMessage(event.target.value)}
                  placeholder="Ask Maestro to plan and coordinate..."
                  aria-label="Message Maestro"
                />
                <button type="submit" disabled={maestroBusy || !draftMessage.trim()}>
                  Send
                </button>
              </form>

              <div className="maestro-status-row">
                <label className="toggle-row">
                  <input
                    type="checkbox"
                    checked={executeMaestroLLM}
                    onChange={(event) => setExecuteMaestroLLM(event.target.checked)}
                  />
                  Execute LLM
                </label>
                <span>{maestroStatus}</span>
              </div>

              {maestroPlan && (
                <section className="maestro-plan" aria-labelledby="maestro-plan-heading">
                  <div className="section-heading">
                    <div>
                      <p className="eyebrow">Proposed plan</p>
                      <h3 id="maestro-plan-heading">{maestroPlan.status}</h3>
                    </div>
                    <button
                      className="planner-action"
                      onClick={runMaestroPlan}
                      disabled={maestroBusy || !["proposed", "queued", "failed"].includes(maestroPlan.status)}
                    >
                      <Sparkles size={16} />
                      Run plan
                    </button>
                  </div>
                  <p>{maestroPlan.summary}</p>
                  {maestroPlan.planner_notes && (
                    <p className="evaluation-note">{maestroPlan.planner_notes}</p>
                  )}
                  {maestroPlan.direct_response && (
                    <p className="evaluation-note">{maestroPlan.direct_response}</p>
                  )}
                  <div className="preview-meta">
                    <span>{maestroPlan.planner_mode} planner</span>
                    <span>{maestroPlan.work_items.length} work items</span>
                    <span>{maestroPlan.intents.length} lanes</span>
                    <span>{maestroPlan.subtasks.length} subtasks</span>
                    <span>{maestroPlanStages.length} stages</span>
                    <span>{String(maestroPlan.scheduler.status ?? "queue")}</span>
                  </div>
                  <div className="maestro-plan-grid">
                    <div>
                      <h4>Work items</h4>
                      {maestroPlan.work_items.map((item) => (
                        <article className="mini-row" key={item.id}>
                          <span>
                            {item.id} / {item.type} / {item.priority} /{" "}
                            {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                          </span>
                          <p>{item.title}</p>
                          <p>{item.description}</p>
                          <div className="preview-meta">
                            <span>{item.needs_agent ? "agent" : "no agent"}</span>
                            <span>{item.can_log_directly ? "loggable" : "not loggable"}</span>
                            <span>
                              {item.blocks_execution
                                ? "blocks run"
                                : item.needs_user_input
                                  ? "needs Chris"
                                  : "no RFI"}
                            </span>
                          </div>
                        </article>
                      ))}
                    </div>
                    <div>
                      <h4>Planning lanes</h4>
                      {maestroPlan.intents.map((intent, index) => (
                        <article className="mini-row" key={`${intent.type}-${index}`}>
                          <span>{intent.type} / {intent.priority}</span>
                          <p>{intent.action ?? intent.summary}</p>
                        </article>
                      ))}
                    </div>
                    <div>
                      <h4>Workflow order</h4>
                      {maestroPlanStages.map((stage, stageIndex) => (
                        <div className="workflow-stage" key={`plan-stage-${stageIndex}`}>
                          <div className="workflow-stage-heading">
                            <span>Stage {stageIndex + 1}</span>
                            <span>{stage.length > 1 ? "parallel-ready" : "single task"}</span>
                          </div>
                          {stage.map((subtask) => (
                            <article
                              className="mini-row"
                              key={`${stageIndex}-${subtask.agent_key}-${subtask.objective}`}
                            >
                              <span>
                                {domainLabels[subtask.domain_key] ?? subtask.domain_key} /{" "}
                                {subtask.agent_name}
                              </span>
                              {subtask.work_item_ids && subtask.work_item_ids.length > 0 && (
                                <span>Work items: {subtask.work_item_ids.join(", ")}</span>
                              )}
                              {subtask.depends_on_work_item_ids &&
                                subtask.depends_on_work_item_ids.length > 0 && (
                                  <span>
                                    Waits for: {subtask.depends_on_work_item_ids.join(", ")}
                                  </span>
                                )}
                              <p>{subtask.objective}</p>
                              {subtask.rationale && <p>{subtask.rationale}</p>}
                            </article>
                          ))}
                        </div>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              {maestroRun && (
                <section className="maestro-plan" aria-labelledby="maestro-run-heading">
                  <div className="section-heading">
                    <div>
                      <p className="eyebrow">Workflow result</p>
                      <h3 id="maestro-run-heading">{maestroRun.status}</h3>
                    </div>
                    <CheckCircle2 size={18} />
                  </div>
                  <div className="preview-meta">
                    <span>{maestroRun.child_runs.length} child runs</span>
                    <span>{maestroRun.execution_stages.length} stages</span>
                    <span>{maestroRun.synthesis_report_id ? "report written" : "no report"}</span>
                    <span>{maestroRun.staged_artifact_path ? "artifact staged" : "not staged"}</span>
                  </div>
                  {maestroRun.execution_stages.length > 0 && (
                    <div className="preview-meta">
                      {maestroRun.execution_stages.map((stage, index) => (
                        <span key={`stage-${index}`}>
                          Stage {index + 1}: {stage.join(", ")}
                        </span>
                      ))}
                    </div>
                  )}
                  <pre>{maestroRun.synthesis}</pre>
                </section>
              )}
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

            <RoutedItemsBoard
              title="Open routed work"
              eyebrow="Maestro aggregate"
              className="dashboard-wide"
            />
          </div>
        )}
      </section>
    </main>
  );
}

function DomainWorkspace({ domainLabel }: { domainLabel: string }) {
  const domainKey = domainKeysByLabel[domainLabel] ?? "maestro-development";
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [agents, setAgents] = useState<AgentSpec[]>([]);
  const [tools, setTools] = useState<ToolRegistryItem[]>([]);
  const [globalContext, setGlobalContext] = useState("");
  const [domainContext, setDomainContext] = useState("");
  const [selectedAgentKey, setSelectedAgentKey] = useState<string | null>(null);
  const [newAgentName, setNewAgentName] = useState("");
  const [newAgentRole, setNewAgentRole] = useState("");
  const [roleSummary, setRoleSummary] = useState("");
  const [rolePrompt, setRolePrompt] = useState("");
  const [currentAction, setCurrentAction] = useState("");
  const [toolPermissions, setToolPermissions] = useState<Record<string, string>>({});
  const [promptTask, setPromptTask] = useState("Prepare a concise domain brief.");
  const [promptPreview, setPromptPreview] = useState<PromptPackage | null>(null);
  const [runPreview, setRunPreview] = useState<AgentRun | null>(null);
  const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
  const [stageRunArtifact, setStageRunArtifact] = useState(false);
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [busy, setBusy] = useState(false);

  const domainAgents = agents.filter((agent) => agent.domain_key === domainKey);
  const selectedAgent =
    domainAgents.find((agent) => agent.key === selectedAgentKey) ?? domainAgents[0] ?? null;

  const refreshAgents = useCallback(async () => {
    const [globalResponse, domainResponse, agentResponse, toolResponse] = await Promise.all([
      apiJson<{ global_context: { context: string } }>("/agents/global-context"),
      apiJson<{ domains: DomainContext[] }>("/agents/domains"),
      apiJson<{ agents: AgentSpec[] }>("/agents"),
      apiJson<{ tools: ToolRegistryItem[] }>("/agents/tools"),
    ]);
    setGlobalContext(globalResponse.global_context.context);
    setDomains(domainResponse.domains);
    setAgents(agentResponse.agents);
    setTools(toolResponse.tools);
    const activeDomain = domainResponse.domains.find((domain) => domain.key === domainKey);
    setDomainContext(activeDomain?.context ?? "");
  }, [domainKey]);

  const refreshAgentTasks = useCallback(async (agentKey: string) => {
    const response = await apiJson<{ tasks: AgentTask[] }>(`/agents/${agentKey}/tasks`);
    setAgentTasks(response.tasks);
  }, []);

  useEffect(() => {
    refreshAgents().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : "Unable to load agents."),
    );
  }, [refreshAgents]);

  useEffect(() => {
    if (!selectedAgent) return;
    setSelectedAgentKey(selectedAgent.key);
    setRoleSummary(selectedAgent.role_summary);
    setRolePrompt(selectedAgent.role_prompt);
    setCurrentAction(selectedAgent.current_action ?? "");
    setToolPermissions(
      Object.fromEntries(selectedAgent.allowed_tools.map((tool) => [tool.key, tool.permission])),
    );
    refreshAgentTasks(selectedAgent.key).catch(() => setAgentTasks([]));
  }, [selectedAgent?.key]);

  const saveGlobalContext = async () => {
    setBusy(true);
    try {
      await apiJson("/agents/global-context", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ context: globalContext }),
      });
      setStatusMessage("Global Maestro context saved.");
      await refreshAgents();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Global context save failed.");
    } finally {
      setBusy(false);
    }
  };

  const saveDomainContext = async () => {
    setBusy(true);
    try {
      await apiJson(`/agents/domains/${domainKey}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ context: domainContext }),
      });
      setStatusMessage("Domain context saved.");
      await refreshAgents();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Domain save failed.");
    } finally {
      setBusy(false);
    }
  };

  const saveAgent = async () => {
    if (!selectedAgent) return;
    setBusy(true);
    try {
      await apiJson(`/agents/${selectedAgent.key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role_summary: roleSummary,
          role_prompt: rolePrompt,
          current_action: currentAction,
          tool_permissions: Object.fromEntries(
            Object.entries(toolPermissions).map(([key, permission]) => [
              key,
              {
                permission,
                description: tools.find((tool) => tool.key === key)?.description ?? "",
              },
            ]),
          ),
        }),
      });
      setStatusMessage("Agent settings saved.");
      await refreshAgents();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Agent save failed.");
    } finally {
      setBusy(false);
    }
  };

  const deleteAgent = async () => {
    if (!selectedAgent) return;
    setBusy(true);
    try {
      await apiJson(`/agents/${selectedAgent.key}`, { method: "DELETE" });
      setSelectedAgentKey(null);
      setPromptPreview(null);
      setRunPreview(null);
      setAgentTasks([]);
      setStatusMessage("Agent deleted.");
      await refreshAgents();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Agent delete failed.");
    } finally {
      setBusy(false);
    }
  };

  const createAgent = async () => {
    setBusy(true);
    try {
      const response = await apiJson<{ agent: AgentSpec }>("/agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          domain_key: domainKey,
          key: newAgentName,
          name: newAgentName,
          role_summary: newAgentRole,
          role_prompt:
            newAgentRole || `You are ${newAgentName}. Work only inside the ${domainLabel} domain.`,
          tool_permissions: {
            "memory.context_bundle": {
              permission: "read",
              description: "Retrieve scoped memory bundles.",
            },
            "artifact.stage_interaction": {
              permission: "write",
              description: "Stage interaction artifacts for memory curation.",
            },
            "llm.gateway": {
              permission: "use",
              description: "Use Maestro's shared LLM gateway.",
            },
          },
        }),
      });
      setNewAgentName("");
      setNewAgentRole("");
      setSelectedAgentKey(response.agent.key);
      setStatusMessage("Agent created.");
      await refreshAgents();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Agent creation failed.");
    } finally {
      setBusy(false);
    }
  };

  const generatePrompt = async () => {
    if (!selectedAgent) return;
    setBusy(true);
    try {
      const response = await apiJson<{ prompt_package: PromptPackage }>(
        `/agents/${selectedAgent.key}/prompt-package`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task_instruction: promptTask,
            query_text: promptTask,
            use_semantic: true,
          }),
        },
      );
      setPromptPreview(response.prompt_package);
      setRunPreview(null);
      setStatusMessage("Prompt package generated.");
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Prompt generation failed.");
    } finally {
      setBusy(false);
    }
  };

  const runAgentOnce = async () => {
    if (!selectedAgent) return;
    setBusy(true);
    try {
      const response = await apiJson<{ run: AgentRun }>(`/agents/${selectedAgent.key}/run-once`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_instruction: promptTask,
          query_text: promptTask,
          use_semantic: true,
          stage_interaction: stageRunArtifact,
          execute_llm: true,
        }),
      });
      setRunPreview(response.run);
      setPromptPreview(response.run.prompt_package);
      await refreshAgents();
      await refreshAgentTasks(selectedAgent.key);
      setStatusMessage(
        response.run.status === "completed" ? "Manual run completed." : "Manual run finished.",
      );
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Manual run failed.");
    } finally {
      setBusy(false);
    }
  };

  const toggleTool = (toolKey: string, checked: boolean) => {
    setToolPermissions((current) => {
      const next = { ...current };
      if (checked) next[toolKey] = next[toolKey] ?? "use";
      else delete next[toolKey];
      return next;
    });
  };

  return (
    <div className="admin-grid">
      {domainKey === "maestro-development" && (
        <section className="memory-panel admin-panel wide-panel" aria-labelledby="global-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Global context</p>
              <h3 id="global-heading">Maestro base prompt</h3>
            </div>
            <Settings size={18} />
          </div>
          <textarea
            value={globalContext}
            onChange={(event) => setGlobalContext(event.target.value)}
            aria-label="Global Maestro context"
          />
          <button className="planner-action" onClick={saveGlobalContext} disabled={busy}>
            Save global context
          </button>
        </section>
      )}

      <section className="memory-panel admin-panel" aria-labelledby="domain-context-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Domain context</p>
            <h3 id="domain-context-heading">{domainLabel}</h3>
          </div>
          <button className="icon-button" onClick={refreshAgents} title="Refresh agents">
            <RefreshCw size={18} />
          </button>
        </div>
        <textarea
          value={domainContext}
          onChange={(event) => setDomainContext(event.target.value)}
          aria-label="Domain context"
        />
        <button className="planner-action" onClick={saveDomainContext} disabled={busy}>
          Save domain context
        </button>
        <p className="memory-status">{statusMessage}</p>
      </section>

      <RoutedItemsBoard
        domainKey={domainKey}
        title={`${domainLabel} routed work`}
        eyebrow="Domain activity"
        className="wide-panel"
      />

      <section className="memory-panel admin-panel" aria-labelledby="domain-agent-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Active agents</p>
            <h3 id="domain-agent-heading">Registry</h3>
          </div>
          <span className="count-badge">{domainAgents.length}</span>
        </div>
        <div className="inline-form">
          <input
            value={newAgentName}
            onChange={(event) => setNewAgentName(event.target.value)}
            placeholder="New agent name"
          />
          <input
            value={newAgentRole}
            onChange={(event) => setNewAgentRole(event.target.value)}
            placeholder="Role summary"
          />
          <button
            className="planner-action"
            onClick={createAgent}
            disabled={busy || !newAgentName.trim()}
          >
            <Plus size={16} />
            Add agent
          </button>
        </div>
        <div className="agent-list">
          {domainAgents.map((agent) => (
            <button
              className={agent.key === selectedAgent?.key ? "agent-row active" : "agent-row"}
              key={agent.key}
              onClick={() => setSelectedAgentKey(agent.key)}
            >
              <span>
                <Bot size={17} />
                {agent.name}
              </span>
              <CheckCircle2 size={17} />
            </button>
          ))}
          {domainAgents.length === 0 && (
            <p className="empty-state">No agents in this domain yet.</p>
          )}
        </div>
      </section>

      <section className="memory-panel admin-panel wide-panel" aria-labelledby="agent-edit-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Agent settings</p>
            <h3 id="agent-edit-heading">{selectedAgent?.name ?? "No agent selected"}</h3>
          </div>
          <Bot size={18} />
        </div>
        {selectedAgent ? (
          <div className="admin-form">
            <label>
              Role summary
              <textarea
                value={roleSummary}
                onChange={(event) => setRoleSummary(event.target.value)}
              />
            </label>
            <label>
              Role prompt
              <textarea
                value={rolePrompt}
                onChange={(event) => setRolePrompt(event.target.value)}
              />
            </label>
            <label>
              Current tasking
              <input
                value={currentAction}
                onChange={(event) => setCurrentAction(event.target.value)}
                placeholder="What this agent is currently working on..."
              />
            </label>
            <label>
              Tool access
              <div className="tool-picker">
                {tools.map((tool) => (
                  <div className="tool-picker-row" key={tool.key}>
                    <label>
                      <input
                        type="checkbox"
                        checked={tool.key in toolPermissions}
                        onChange={(event) => toggleTool(tool.key, event.target.checked)}
                      />
                      <span>{tool.name}</span>
                    </label>
                    <select
                      value={toolPermissions[tool.key] ?? "use"}
                      disabled={!(tool.key in toolPermissions)}
                      onChange={(event) =>
                        setToolPermissions((current) => ({
                          ...current,
                          [tool.key]: event.target.value,
                        }))
                      }
                    >
                      <option value="use">Use</option>
                      <option value="read">Read</option>
                      <option value="write">Write</option>
                      <option value="admin">Admin</option>
                    </select>
                  </div>
                ))}
              </div>
            </label>
            <button className="planner-action" onClick={saveAgent} disabled={busy}>
              Save agent
            </button>
            <button className="danger-action" onClick={deleteAgent} disabled={busy}>
              <Trash2 size={16} />
              Delete agent
            </button>
          </div>
        ) : (
          <p className="empty-state">Select an agent to edit role, tasking, and tools.</p>
        )}
      </section>

      <section className="memory-panel admin-panel wide-panel" aria-labelledby="agent-task-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Agent queue</p>
            <h3 id="agent-task-heading">{selectedAgent?.name ?? "No agent selected"}</h3>
          </div>
          <Clock3 size={18} />
        </div>
        <div className="task-list">
          {agentTasks.map((task) => (
            <article className="task-row" key={task.id}>
              <span>{task.status}</span>
              <strong>{task.objective}</strong>
              <p>{task.workflow_key ?? task.source_type}</p>
              {task.error_message && <p>{task.error_message}</p>}
            </article>
          ))}
          {agentTasks.length === 0 && (
            <p className="empty-state">No queued or recent tasks for this agent.</p>
          )}
        </div>
      </section>

      <section
        className="memory-panel admin-panel wide-panel"
        aria-labelledby="prompt-debug-heading"
      >
        <div className="section-heading">
          <div>
            <p className="eyebrow">Prompt package</p>
            <h3 id="prompt-debug-heading">Debug assembly</h3>
          </div>
          <Sparkles size={18} />
        </div>
        <div className="admin-form">
          <label>
            Task instruction
            <input value={promptTask} onChange={(event) => setPromptTask(event.target.value)} />
          </label>
          <button
            className="planner-action"
            onClick={generatePrompt}
            disabled={busy || !selectedAgent}
          >
            Generate prompt package
          </button>
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={stageRunArtifact}
              onChange={(event) => setStageRunArtifact(event.target.checked)}
            />
            Stage run artifact for memory curation
          </label>
          <button
            className="planner-action"
            onClick={runAgentOnce}
            disabled={busy || !selectedAgent}
          >
            <Sparkles size={16} />
            Run once
          </button>
        </div>
        {runPreview && (
          <div className="run-preview">
            <span>{runPreview.status}</span>
            <p>{runPreview.execution_note}</p>
            {runPreview.task_id && <p>Task: {runPreview.task_id}</p>}
            {runPreview.report_id && <p>Report: {runPreview.report_id}</p>}
            <p>Scheduler: {runPreview.scheduler?.status ?? "unknown"}</p>
            {(runPreview.tool_calls ?? []).map((toolCall) => (
              <p key={toolCall.id}>
                {toolCall.tool_name}: {toolCall.status}
              </p>
            ))}
            {runPreview.error_message && <p>{runPreview.error_message}</p>}
            {runPreview.staged_artifact_path && (
              <p>Staged artifact: {runPreview.staged_artifact_path}</p>
            )}
            {runPreview.output_text && <pre>{runPreview.output_text}</pre>}
          </div>
        )}
        {promptPreview && (
          <div className="prompt-preview">
            <div className="preview-meta">
              <span>{promptPreview.memory_context.included_count} memories</span>
              <span>semantic {promptPreview.memory_context.semantic_status}</span>
            </div>
            <pre>{promptPreview.assembled_prompt}</pre>
          </div>
        )}
      </section>
    </div>
  );
}

function ToolsWorkspace() {
  const [tools, setTools] = useState<ToolRegistryItem[]>([]);
  const [connections, setConnections] = useState<ToolConnection[]>([]);
  const [selectedToolKey, setSelectedToolKey] = useState("memory.context_bundle");
  const [connectionDomain, setConnectionDomain] = useState("praxis");
  const [connectionName, setConnectionName] = useState("Praxis memory retrieval");
  const [connectionAuthType, setConnectionAuthType] = useState("service");
  const [connectionConfig, setConnectionConfig] = useState("{}");
  const [statusMessage, setStatusMessage] = useState("Ready");

  const selectedTool = tools.find((tool) => tool.key === selectedToolKey) ?? tools[0] ?? null;
  const selectedToolConnections = connections.filter(
    (connection) => connection.tool_key === selectedTool?.key,
  );
  const selectedToolAgents = selectedTool?.authorized_agents ?? [];
  const selectedConnection = selectedToolConnections.find(
    (connection) => connection.domain_key === connectionDomain,
  );

  const refreshTools = useCallback(async () => {
    const [toolResponse, connectionResponse] = await Promise.all([
      apiJson<{ tools: ToolRegistryItem[] }>("/agents/tools"),
      apiJson<{ connections: ToolConnection[] }>("/agents/tools/connections"),
    ]);
    setTools(toolResponse.tools);
    setConnections(connectionResponse.connections);
    if (!toolResponse.tools.some((tool) => tool.key === selectedToolKey)) {
      setSelectedToolKey(toolResponse.tools[0]?.key ?? "memory.context_bundle");
    }
  }, [selectedToolKey]);

  useEffect(() => {
    if (!selectedTool) return;
    const existing = connections.find(
      (connection) =>
        connection.tool_key === selectedTool.key && connection.domain_key === connectionDomain,
    );
    if (existing) {
      setConnectionName(existing.display_name);
      setConnectionAuthType(existing.auth_type);
      setConnectionConfig(JSON.stringify(existing.config, null, 2));
      return;
    }
    setConnectionName(`${domainLabels[connectionDomain] ?? connectionDomain} ${selectedTool.name}`);
    setConnectionAuthType("service");
    setConnectionConfig("{}");
  }, [connectionDomain, connections, selectedTool?.key]);

  useEffect(() => {
    if (!selectedTool) return;
    setConnectionDomain(selectedToolConnections[0]?.domain_key ?? "praxis");
  }, [selectedTool?.key]);

  const selectConnection = (domainKey: string) => {
    setConnectionDomain(domainKey);
  };

  useEffect(() => {
    refreshTools().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : "Unable to load tools."),
    );
  }, [refreshTools]);

  const saveConnection = async () => {
    try {
      const config = JSON.parse(connectionConfig) as Record<string, unknown>;
      await apiJson("/agents/tools/connections", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          domain_key: connectionDomain,
          tool_key: selectedTool?.key,
          display_name: connectionName,
          auth_type: connectionAuthType,
          config,
          is_active: true,
        }),
      });
      setStatusMessage("Tool connection saved.");
      await refreshTools();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Tool connection save failed.");
    }
  };

  return (
    <div className="admin-grid">
      <section className="memory-panel admin-panel" aria-labelledby="tools-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Shared tool suite</p>
            <h3 id="tools-heading">Tools</h3>
          </div>
          <button className="icon-button" onClick={refreshTools} title="Refresh tools">
            <RefreshCw size={18} />
          </button>
        </div>
        <div className="tool-registry-list">
          {tools.map((tool) => (
            <button
              className={
                tool.key === selectedTool?.key
                  ? "tool-registry-row selectable active"
                  : "tool-registry-row selectable"
              }
              key={tool.key}
              onClick={() => setSelectedToolKey(tool.key)}
            >
              <div>
                <span>{tool.exclusive ? "Exclusive / queued" : "Shared"}</span>
                <h4>{tool.name}</h4>
                <p>{tool.description}</p>
              </div>
              <div className="preview-meta">
                <span>{tool.connected_domains.length} connected domains</span>
                <span>{tool.authorized_agents.length} authorized agents</span>
              </div>
            </button>
          ))}
          {tools.length === 0 && <p className="empty-state">No tools registered yet.</p>}
        </div>
        <p className="memory-status">{statusMessage}</p>
      </section>

      <section className="memory-panel admin-panel" aria-labelledby="tool-detail-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Selected tool</p>
            <h3 id="tool-detail-heading">{selectedTool?.name ?? "No tool selected"}</h3>
          </div>
          <ShieldCheck size={18} />
        </div>
        {selectedTool ? (
          <>
            <p className="empty-state">{selectedTool.description}</p>
            <div className="connection-list">
              {Object.entries(domainLabels)
                .filter(([key]) => key !== "global")
                .map(([domainKey, label]) => {
                  const connection = selectedToolConnections.find(
                    (item) => item.domain_key === domainKey,
                  );
                  const domainAgents = selectedToolAgents.filter(
                    (agent) => agent.domain_key === domainKey,
                  );
                  return (
                    <button
                      className={
                        domainKey === connectionDomain
                          ? "connection-row selectable active"
                          : "connection-row selectable"
                      }
                      key={domainKey}
                      onClick={() => selectConnection(domainKey)}
                    >
                      <span>{label}</span>
                      <strong>{connection?.display_name ?? "No credentials stored"}</strong>
                      <span>{connection?.auth_type ?? "not connected"}</span>
                      <span>{domainAgents.length} agents</span>
                    </button>
                  );
                })}
            </div>
            <div className="agent-chip-list">
              {selectedToolAgents.map((agent) => (
                <span key={`${selectedTool.key}-${agent.agent_key}`} className="agent-chip">
                  {domainLabels[agent.domain_key] ?? agent.domain_key}: {agent.agent_name} (
                  {agent.permission})
                </span>
              ))}
              {selectedToolAgents.length === 0 && (
                <p className="empty-state">No agents currently have access to this tool.</p>
              )}
            </div>
            <div className="admin-form">
              <label>
                Domain
                <select
                  value={connectionDomain}
                  onChange={(event) => setConnectionDomain(event.target.value)}
                >
                  {Object.entries(domainLabels)
                    .filter(([key]) => key !== "global")
                    .map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                Display name
                <input
                  value={connectionName}
                  onChange={(event) => setConnectionName(event.target.value)}
                />
              </label>
              <label>
                Auth type
                <select
                  value={connectionAuthType}
                  onChange={(event) => setConnectionAuthType(event.target.value)}
                >
                  <option value="service">Service</option>
                  <option value="api_key">API key</option>
                  <option value="oauth">OAuth</option>
                  <option value="login_password">Login + password</option>
                  <option value="manual">Manual</option>
                </select>
              </label>
              <label>
                Credential/config JSON
                <textarea
                  value={connectionConfig}
                  onChange={(event) => setConnectionConfig(event.target.value)}
                  placeholder='{"username":"praxis@example.com","password":"..."}'
                />
              </label>
              {selectedConnection && (
                <p className="memory-status">
                  Existing secret-like values are redacted. Replace them here to update.
                </p>
              )}
              <button className="planner-action" onClick={saveConnection}>
                Save {domainLabels[connectionDomain] ?? connectionDomain} credentials
              </button>
            </div>
          </>
        ) : (
          <p className="empty-state">Select a tool to inspect domain credentials.</p>
        )}
      </section>
    </div>
  );
}

function MemoryWorkspace() {
  const [domains, setDomains] = useState<DropboxDomain[]>(dropboxDomainDefaults);
  const [selectedDomain, setSelectedDomain] = useState("ophi");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previews, setPreviews] = useState<MemoryPreview[]>([]);
  const [pending, setPending] = useState<PendingProposal[]>([]);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [sources, setSources] = useState<MemorySource[]>([]);
  const [sourceTargetDomain, setSourceTargetDomain] = useState("personal");
  const [selectedPreviewFilename, setSelectedPreviewFilename] = useState<string | null>(null);
  const [retrievalDomain, setRetrievalDomain] = useState("praxis");
  const [retrievalQuery, setRetrievalQuery] = useState("");
  const [retrievalMode, setRetrievalMode] = useState<"balanced" | "strict" | "broad">("balanced");
  const [semanticRetrieval, setSemanticRetrieval] = useState(true);
  const [retrievalResults, setRetrievalResults] = useState<RetrievedMemory[]>([]);
  const [retrievalTotal, setRetrievalTotal] = useState(0);
  const [retrievalFiltered, setRetrievalFiltered] = useState(0);
  const [semanticStatus, setSemanticStatus] = useState("not requested");
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [lastProcessSummary, setLastProcessSummary] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshMemory = useCallback(async () => {
    const [status, previewResponse, pendingResponse, itemResponse, sourceResponse] =
      await Promise.all([
      apiJson<{ domains: DropboxDomain[] }>("/memory/dropbox/status"),
      apiJson<{ previews: MemoryPreview[] }>("/memory/dropbox/previews"),
      apiJson<{ proposals: PendingProposal[] }>("/memory/proposals/pending"),
      apiJson<{ items: MemoryItem[] }>("/memory/items?limit=8"),
      apiJson<{ sources: MemorySource[] }>("/memory/sources?limit=8"),
      ]);
    setDomains(status.domains);
    const sortedPreviews = [...previewResponse.previews].sort(
      (first, second) => previewTime(second) - previewTime(first),
    );
    setPreviews(sortedPreviews.slice(0, 10));
    setPending(pendingResponse.proposals);
    setItems(itemResponse.items);
    setSources(sourceResponse.sources);
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

  const reclassifySource = async (sourceId: string) => {
    setBusy(true);
    try {
      await apiJson(`/memory/sources/${sourceId}/reclassify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_domain_key: sourceTargetDomain,
          reason: "Corrected from Memory tab source review.",
        }),
      });
      setStatusMessage(`Source reclassified to ${sourceTargetDomain}.`);
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Source reclassification failed.");
    } finally {
      setBusy(false);
    }
  };

  const archiveMemory = async (memoryItemId: string) => {
    setBusy(true);
    try {
      await apiJson(`/memory/items/${memoryItemId}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Archived from Memory tab." }),
      });
      setStatusMessage("Memory archived.");
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Memory archive failed.");
    } finally {
      setBusy(false);
    }
  };

  const runRetrieval = async () => {
    setBusy(true);
    try {
      const params = new URLSearchParams({
        audience: "maestro",
        domain_key: retrievalDomain,
        mode: retrievalMode,
        use_semantic: semanticRetrieval ? "true" : "false",
        limit: "8",
      });
      if (retrievalQuery.trim()) {
        params.set("query_text", retrievalQuery.trim());
      }
      const response = await apiJson<{
        total_visible: number;
        filtered_count: number;
        semantic_status: string;
        results: RetrievedMemory[];
      }>(`/memory/retrieve?${params.toString()}`);
      setRetrievalResults(response.results);
      setRetrievalTotal(response.total_visible);
      setRetrievalFiltered(response.filtered_count);
      setSemanticStatus(response.semantic_status);
      setStatusMessage(`Retrieved ${response.results.length} memories.`);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Retrieval failed.");
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
            <span>
              {selectedFile ? selectedFile.name : "Choose PDF, DOCX, Markdown, text, or data"}
            </span>
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
          <span>Processing {selectedDomainStatus?.processing ?? 0}</span>
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
              <span>{latestPreview.routed_count} routed</span>
              <span>
                {latestPreview.progress_count}/{latestPreview.progress_total} processed
              </span>
              <span>{latestPreview.written_count} written</span>
              <span>{latestPreview.deduped_count} deduped</span>
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
                  <div className={`result-pill ${candidateResultClass(latestPreview, index)}`}>
                    {candidateResultLabel(latestPreview, index)}
                  </div>
                  {latestPreview.payload.results?.[index]?.evaluation?.rationale && (
                    <p className="evaluation-note">
                      {latestPreview.payload.results[index].evaluation?.rationale}
                    </p>
                  )}
                </article>
              ))}
            </div>
            {(latestPreview.payload.routed_items ?? []).length > 0 && (
              <>
                <h4>Routed items</h4>
                <div className="candidate-list">
                  {(latestPreview.payload.routed_items ?? []).map((item, index) => (
                    <article className="candidate-row routed" key={`${item.title}-${index}`}>
                      <span>
                        {item.route_type} / {item.priority} / {item.status}
                      </span>
                      <h4>{item.title}</h4>
                      <p>{item.content}</p>
                    </article>
                  ))}
                </div>
              </>
            )}
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
              <button
                className="danger-action"
                onClick={() => archiveMemory(item.id)}
                disabled={busy}
              >
                <Trash2 size={16} />
                Archive memory
              </button>
            </article>
          ))}
          {items.length === 0 && <p className="empty-state">No memory has been written yet.</p>}
        </div>
      </section>

      <section className="memory-panel" aria-labelledby="memory-retrieval-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Retrieval debug</p>
            <h3 id="memory-retrieval-heading">Context search</h3>
          </div>
          <Search size={18} />
        </div>
        <div className="retrieval-controls">
          <label>
            Domain
            <select
              value={retrievalDomain}
              onChange={(event) => setRetrievalDomain(event.target.value)}
            >
              {domains.map((domain) => (
                <option key={domain.key} value={domain.key}>
                  {domainLabels[domain.key] ?? domain.key}
                </option>
              ))}
            </select>
          </label>
          <label>
            Query
            <input
              value={retrievalQuery}
              onChange={(event) => setRetrievalQuery(event.target.value)}
              placeholder="Search task context..."
            />
          </label>
          <label>
            Mode
            <select
              value={retrievalMode}
              onChange={(event) =>
                setRetrievalMode(event.target.value as "balanced" | "strict" | "broad")
              }
            >
              <option value="balanced">Balanced</option>
              <option value="strict">Strict</option>
              <option value="broad">Broad</option>
            </select>
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={semanticRetrieval}
              onChange={(event) => setSemanticRetrieval(event.target.checked)}
            />
            Semantic
          </label>
          <button className="planner-action" onClick={runRetrieval} disabled={busy}>
            <Search size={17} />
            Retrieve
          </button>
        </div>
        <div className="retrieval-list">
          {retrievalResults.map((item) => (
            <article className="memory-row" key={item.id}>
              <span>
                {item.domain_key} / {item.scope} / score {item.score.toFixed(2)}
              </span>
              <h4>{item.title}</h4>
              <p>{item.content}</p>
              <div className="preview-meta">
                <span>relevance {(item.query_relevance * 100).toFixed(0)}%</span>
                <span>
                  semantic{" "}
                  {item.semantic_similarity === null
                    ? "n/a"
                    : `${(item.semantic_similarity * 100).toFixed(0)}%`}
                </span>
                <span>importance {(item.importance * 100).toFixed(0)}%</span>
              </div>
              <p className="evaluation-note">{item.score_reasons.join(" | ")}</p>
              <div className="preview-meta">
                <span>{item.provenance.source_refs.length} source refs</span>
                <span>{item.provenance.artifact ? "artifact" : "no artifact"}</span>
                <span>{item.links.length} links</span>
              </div>
            </article>
          ))}
          {retrievalResults.length === 0 ? (
            <p className="empty-state">
              Run retrieval to inspect ranked context. {retrievalTotal} visible memories.
            </p>
          ) : (
            <p className="memory-status">
              {retrievalTotal} visible memories. {retrievalFiltered} filtered by retrieval mode.
              {" "}Semantic: {semanticStatus}.
            </p>
          )}
        </div>
      </section>

      <section className="memory-panel" aria-labelledby="memory-sources-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Source review</p>
            <h3 id="memory-sources-heading">Recent ingests</h3>
          </div>
          <Database size={18} />
        </div>
        <div className="source-controls">
          <label>
            Reclassify target
            <select
              value={sourceTargetDomain}
              onChange={(event) => setSourceTargetDomain(event.target.value)}
            >
              {domains.map((domain) => (
                <option key={domain.key} value={domain.key}>
                  {domainLabels[domain.key] ?? domain.key}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="source-list">
          {sources.map((source) => (
            <article className="source-row" key={source.id}>
              <div>
                <span>
                  {domainLabels[source.domain_key] ?? source.domain_key} / {source.status}
                </span>
                <h4>{source.name}</h4>
                <p>
                  {source.memory_count} memories / {source.proposal_count} proposals
                </p>
              </div>
              <button
                className="planner-action"
                onClick={() => reclassifySource(source.id)}
                disabled={busy || source.domain_key === sourceTargetDomain}
              >
                Reclassify
              </button>
            </article>
          ))}
          {sources.length === 0 && <p className="empty-state">No ingested sources yet.</p>}
        </div>
      </section>
    </div>
  );
}
