import {
  Bot,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  HardDriveUpload,
  Inbox,
  Menu,
  MessageSquareText,
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

type ChatMessage = {
  id: string;
  sender: "user" | "maestro";
  content: string;
};

type MaestroSessionSummary = {
  id: string;
  title: string;
  messages: ChatMessage[];
  message_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  stagedArtifactPath: string | null;
  active_plan?: MaestroPlan | null;
  archived?: boolean;
  archived_at?: string | null;
};

type SchedulerQueueItem = {
  id: string;
  workflow_run_id: string;
  external_key: string;
  status: string;
  priority: string;
  stage_index: number;
  position: number;
  objective: string;
  dependency_keys: string[];
  resource_locks: Array<Record<string, unknown>>;
  fairness_group: string | null;
  domain_key: string | null;
  agent_key: string | null;
  agent_name: string | null;
  lease_owner: string | null;
  error_message: string | null;
};

type SchedulerRun = {
  id: string;
  workflow_definition_id: string | null;
  parent_task_id: string | null;
  conversation_id: string | null;
  source_type: string;
  status: string;
  priority: string;
  fairness_group: string | null;
  summary: string | null;
  created_at: string | null;
  input_payload?: Record<string, unknown>;
  output_payload?: Record<string, unknown>;
  error_message?: string | null;
  events?: SchedulerEvent[];
  queue_items: SchedulerQueueItem[];
};

type SchedulerEvent = {
  id: string;
  event_type: string;
  message: string;
  queue_item_id: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
};

type SchedulerDefinition = {
  id: string;
  domain_key: string | null;
  key: string;
  name: string;
  description: string | null;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  workflow_spec?: Record<string, unknown>;
  priority: string;
  fairness_group: string | null;
  is_active: boolean;
};

type SchedulerDashboard = {
  definitions: SchedulerDefinition[];
  runs: SchedulerRun[];
  runnable_batches: Array<{
    workflow_run_id: string;
    status: string;
    fairness_group: string | null;
    parallel_ready: SchedulerQueueItem[];
  }>;
  active_locks: Array<Record<string, unknown>>;
};

type SchedulerWorkerAgentRun = {
  run_id: string;
  status: string;
  agent_key: string;
  agent_name: string;
  task_id: string | null;
  report_id: string | null;
  execution_note: string;
  output_preview: string;
  tool_calls: AgentRun["tool_calls"];
  staged_artifact_path: string | null;
  artifact_id: string | null;
  error_message: string | null;
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
    output_payload?: Record<string, unknown> | null;
  }>;
  scheduler?: {
    status: string;
    reason: string;
  };
  tool_loop?: Record<string, unknown>;
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

type MaestroQueueItem = {
  id: string;
  stage_index: number;
  position: number;
  status: string;
  agent_key: string;
  agent_name: string;
  domain_key: string;
  objective: string;
  priority: string;
  work_item_ids: string[];
  depends_on_work_item_ids: string[];
  child_task_id: string | null;
  child_report_id: string | null;
  retry_count?: number;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
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
  workflow_graph: {
    nodes?: Array<Record<string, unknown>>;
    edges?: Array<Record<string, unknown>>;
    stages?: Array<Record<string, unknown>>;
  };
  is_chat_only: boolean;
  selected_agents: Array<Record<string, unknown>>;
  approval_required: boolean;
  scheduler: Record<string, unknown> & {
    queue_items?: MaestroQueueItem[];
    current_step?: string;
    active_queue_item_id?: string | null;
    active_stage_index?: number | null;
  };
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
  chat_summary: string;
  staged_artifact_path: string | null;
  artifact_id: string | null;
  error_message: string | null;
  execution_stages: string[][];
  tool_activity: Array<{
    tool_call_id: string | null;
    agent_key: string;
    agent_name: string;
    domain_key: string;
    tool_name: string;
    status: string;
    error_message: string | null;
    details: string;
    output_payload?: Record<string, unknown>;
  }>;
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
    tool_calls?: AgentRun["tool_calls"];
    tool_loop?: Record<string, unknown>;
  }>;
};

type MaestroRespond = {
  kind: "chat_only" | "planned" | "refined" | "rfi_answered" | "routed";
  classification: string;
  message: string;
  plan: MaestroPlan | null;
  chat_plan: MaestroPlan | null;
  active_plan: MaestroPlan | null;
  conversation: MaestroSessionSummary;
};

type MaestroToolCallResponse = {
  tool_call: {
    id: string;
    tool_name: string;
    status: string;
    error_message: string | null;
    output_payload?: Record<string, unknown> | null;
  };
  message: string;
  run?: MaestroRun | null;
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

const routedGroups = [
  { key: "human_input", label: "RFIs", empty: "No open RFIs." },
  { key: "task", label: "Tasks", empty: "No open tasks." },
  { key: "event", label: "Events", empty: "No extracted events." },
  { key: "contact", label: "Contacts", empty: "No extracted contacts." },
  { key: "decision_log", label: "Decisions", empty: "No recent decisions." },
  { key: "think_tank", label: "Think Tank", empty: "No think tank notes." },
];

const hiddenRoutedStatuses = new Set(["done", "archived"]);

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
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [sessionHistory, setSessionHistory] = useState<MaestroSessionSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [schedulerDashboard, setSchedulerDashboard] = useState<SchedulerDashboard | null>(null);
  const [showSessionHistory, setShowSessionHistory] = useState(false);
  const [draftMessage, setDraftMessage] = useState("");
  const [maestroPlan, setMaestroPlan] = useState<MaestroPlan | null>(null);
  const [maestroRun, setMaestroRun] = useState<MaestroRun | null>(null);
  const [maestroStatus, setMaestroStatus] = useState("Ready");
  const [executeMaestroLLM, setExecuteMaestroLLM] = useState(true);
  const [autoMaestroToolLoop, setAutoMaestroToolLoop] = useState(true);
  const [maestroBusy, setMaestroBusy] = useState(false);
  const [busyToolCallId, setBusyToolCallId] = useState<string | null>(null);
  const [expandedWorkflowNodeId, setExpandedWorkflowNodeId] = useState<string | null>(null);
  const [schedulerDefinitionMode, setSchedulerDefinitionMode] = useState<"recurring" | "event">(
    "recurring",
  );
  const [schedulerDefinitionName, setSchedulerDefinitionName] = useState("Daily Before 8");
  const [schedulerDefinitionDomain, setSchedulerDefinitionDomain] = useState("personal");
  const [schedulerDefinitionObjective, setSchedulerDefinitionObjective] = useState(
    "Prepare the daily brief.",
  );
  const [schedulerDefinitionTime, setSchedulerDefinitionTime] = useState("07:55");
  const [schedulerDefinitionEvent, setSchedulerDefinitionEvent] = useState("gmail.message.received");
  const [schedulerEventId, setSchedulerEventId] = useState("manual-test-event");
  const [schedulerStatusMessage, setSchedulerStatusMessage] = useState("");
  const [selectedSchedulerRun, setSelectedSchedulerRun] = useState<SchedulerRun | null>(null);
  const [selectedSchedulerDefinition, setSelectedSchedulerDefinition] =
    useState<SchedulerDefinition | null>(null);

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

  const queueStages = useMemo(() => {
    const queueItems = maestroPlan?.scheduler.queue_items ?? [];
    const stages = new Map<number, MaestroQueueItem[]>();
    queueItems.forEach((item) => {
      const items = stages.get(item.stage_index) ?? [];
      items.push(item);
      stages.set(item.stage_index, items);
    });
    return Array.from(stages)
      .sort(([left], [right]) => left - right)
      .map(([stageIndex, items]) => ({
        stageIndex,
        items: items.sort((left, right) => left.position - right.position),
      }));
  }, [maestroPlan]);

  const selectedWorkflowItem = useMemo(() => {
    const queueItems = maestroPlan?.scheduler.queue_items ?? [];
    return queueItems.find((item) => item.id === expandedWorkflowNodeId) ?? null;
  }, [expandedWorkflowNodeId, maestroPlan]);

  const selectedWorkflowWorkItems = useMemo(() => {
    if (!maestroPlan || !selectedWorkflowItem) return [];
    const selectedIds = new Set(selectedWorkflowItem.work_item_ids);
    return maestroPlan.work_items.filter((item) => selectedIds.has(item.id));
  }, [maestroPlan, selectedWorkflowItem]);

  const selectedWorkflowSubtask = useMemo(() => {
    if (!maestroPlan || !selectedWorkflowItem) return null;
    return (
      maestroPlan.subtasks.find(
        (subtask) =>
          subtask.agent_key === selectedWorkflowItem.agent_key &&
          JSON.stringify(subtask.work_item_ids ?? []) ===
            JSON.stringify(selectedWorkflowItem.work_item_ids),
      ) ?? null
    );
  }, [maestroPlan, selectedWorkflowItem]);

  const routedPlanItems = useMemo(() => {
    if (!maestroPlan) return [];
    const queuedWorkItemIds = new Set(
      (maestroPlan.scheduler.queue_items ?? []).flatMap((item) => item.work_item_ids),
    );
    return maestroPlan.work_items.filter(
      (item) =>
        !queuedWorkItemIds.has(item.id) &&
        (item.can_log_directly || item.needs_user_input || !item.needs_agent),
    );
  }, [maestroPlan]);

  const activeWorkflowSummary = useMemo(() => {
    if (!maestroPlan && !maestroRun) return null;
    const plan = maestroRun?.plan ?? maestroPlan;
    if (!plan) return null;
    const queueItems = plan.scheduler.queue_items ?? [];
    const completed = queueItems.filter((item) => item.status === "completed").length;
    const blocked = queueItems.filter((item) => item.status === "blocked").length;
    const failed = queueItems.filter((item) => item.status === "failed").length;
    const running = queueItems.filter((item) =>
      ["ready", "running", "retrying", "pending"].includes(item.status),
    ).length;
    return {
      id: plan.parent_task_id,
      title: plan.summary,
      status: maestroRun?.status ?? plan.status,
      schedulerStatus: String(plan.scheduler.status ?? "queue"),
      queueItems,
      completed,
      blocked,
      failed,
      running,
      stageCount: plan.execution_stages.length || queueStages.length,
      reportWritten: Boolean(maestroRun?.synthesis_report_id),
      artifactStaged: Boolean(maestroRun?.staged_artifact_path),
    };
  }, [maestroPlan, maestroRun, queueStages.length]);

  const pendingToolApprovals = useMemo(
    () =>
      (maestroRun?.tool_activity ?? []).filter(
        (activity) => activity.status === "approval_required" && activity.tool_call_id,
      ),
    [maestroRun],
  );

  const codexReviewPayload = (activity: MaestroRun["tool_activity"][number]) => {
    const payload = activity.output_payload ?? {};
    const pr = payload.pr && typeof payload.pr === "object" ? (payload.pr as Record<string, unknown>) : {};
    const prUrl = String(payload.pr_url ?? pr.pr_url ?? pr.url ?? "");
    const prNumber = payload.pr_number ?? pr.pr_number ?? pr.number;
    const prTitle = String(pr.title ?? "");
    const prBody = String(pr.body ?? "");
    const branch = String(payload.branch ?? "");
    const baseBranch = String(payload.base_branch ?? "");
    const diffSummary = String(payload.diff_summary ?? "");
    const finalMessage = String(payload.final_message ?? "");
    const changedFiles = Array.isArray(payload.changed_files)
      ? payload.changed_files.map((item) => String(item))
      : [];
    return {
      prUrl,
      prNumber,
      prTitle,
      prBody,
      branch,
      baseBranch,
      diffSummary,
      finalMessage,
      changedFiles,
      hasReview: Boolean(prUrl || changedFiles.length > 0 || diffSummary || prBody),
    };
  };

  const isApprovalMessage = (message: string) => {
    const normalized = message.trim().toLowerCase();
    return ["approved", "approve", "yes approved", "yes, approved", "go ahead", "run it"].includes(
      normalized,
    );
  };

  const loadSessionHistory = useCallback(async () => {
    const response = await apiJson<{ sessions: MaestroSessionSummary[] }>("/maestro/sessions");
    setSessionHistory(response.sessions);
  }, []);

  const applyConversation = useCallback((conversation: MaestroSessionSummary) => {
    setActiveConversationId(conversation.id);
    setChatMessages(conversation.messages ?? []);
    setMaestroPlan(conversation.active_plan ?? null);
    setMaestroRun(null);
  }, []);

  const pollActiveChannel = useCallback(async () => {
    const response = await apiJson<{ conversation: MaestroSessionSummary }>(
      "/maestro/sessions/active",
    );
    setActiveConversationId(response.conversation.id);
    setChatMessages(response.conversation.messages ?? []);
    setMaestroPlan((currentPlan) => currentPlan ?? response.conversation.active_plan ?? null);
  }, []);

  const loadActiveSession = useCallback(async () => {
    const response = await apiJson<{ conversation: MaestroSessionSummary }>(
      "/maestro/sessions/active",
    );
    applyConversation(response.conversation);
  }, [applyConversation]);

  const loadSchedulerDashboard = useCallback(async () => {
    const response = await apiJson<SchedulerDashboard>("/scheduler/dashboard");
    setSchedulerDashboard(response);
  }, []);

  const archiveSession = async (sessionId: string) => {
    await apiJson<{ conversation: MaestroSessionSummary }>(
      `/maestro/sessions/${sessionId}/archive`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived: true }),
      },
    );
    if (activeConversationId === sessionId) {
      const started = await apiJson<{ conversation: MaestroSessionSummary }>("/maestro/sessions/start", {
        method: "POST",
      });
      applyConversation(started.conversation);
      setMaestroPlan(null);
      setMaestroRun(null);
    }
    await loadSessionHistory();
    setMaestroStatus("Session archived.");
  };

  const createSchedulerDefinition = async () => {
    const keyBase = schedulerDefinitionName
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
    const key = `${schedulerDefinitionDomain}-${keyBase || schedulerDefinitionMode}`;
    const queueItemId = schedulerDefinitionMode === "event" ? "event-work" : "scheduled-work";
    const triggerConfig =
      schedulerDefinitionMode === "event"
        ? {
            event_type: schedulerDefinitionEvent,
            filters: { domain_key: schedulerDefinitionDomain },
          }
        : {
            time_of_day: schedulerDefinitionTime,
            interval_minutes: 1440,
          };
    const path = selectedSchedulerDefinition
      ? `/scheduler/definitions/${selectedSchedulerDefinition.id}`
      : "/scheduler/definitions";
    const method = selectedSchedulerDefinition ? "PATCH" : "POST";
    const response = await apiJson<{ definition: SchedulerDefinition }>(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key,
        name: schedulerDefinitionName,
        domain_key: schedulerDefinitionDomain,
        trigger_type: schedulerDefinitionMode,
        trigger_config: triggerConfig,
        workflow_spec: {
          queue_items: [
            {
              id: queueItemId,
              objective: schedulerDefinitionObjective,
              domain_key: schedulerDefinitionDomain,
            },
          ],
        },
      }),
    });
    setSelectedSchedulerDefinition(response.definition);
    setSchedulerStatusMessage("Workflow definition saved.");
    await loadSchedulerDashboard();
  };

  const selectSchedulerDefinition = (definition: SchedulerDefinition) => {
    setSelectedSchedulerDefinition(definition);
    setSchedulerDefinitionMode(definition.trigger_type === "event" ? "event" : "recurring");
    setSchedulerDefinitionName(definition.name);
    setSchedulerDefinitionDomain(definition.domain_key ?? "personal");
    const queueItems = Array.isArray(definition.workflow_spec?.queue_items)
      ? (definition.workflow_spec?.queue_items as Array<Record<string, unknown>>)
      : [];
    setSchedulerDefinitionObjective(String(queueItems[0]?.objective ?? definition.description ?? ""));
    if (typeof definition.trigger_config.event_type === "string") {
      setSchedulerDefinitionEvent(definition.trigger_config.event_type);
    }
    if (typeof definition.trigger_config.time_of_day === "string") {
      setSchedulerDefinitionTime(definition.trigger_config.time_of_day);
    }
    setSchedulerStatusMessage(`Editing ${definition.name}.`);
  };

  const selectSchedulerRun = async (runId: string) => {
    const response = await apiJson<{ run: SchedulerRun }>(`/scheduler/runs/${runId}`);
    setSelectedSchedulerRun(response.run);
    setSchedulerStatusMessage("Workflow run loaded.");
  };

  const archiveSchedulerRun = async (runId: string) => {
    await apiJson<{ run: SchedulerRun }>(`/scheduler/runs/${runId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "archived" }),
    });
    if (selectedSchedulerRun?.id === runId) setSelectedSchedulerRun(null);
    setSchedulerStatusMessage("Workflow archived from queue.");
    await loadSchedulerDashboard();
  };

  const reenterSchedulerRunSession = async (run: SchedulerRun) => {
    if (!run.conversation_id) {
      setSchedulerStatusMessage("This workflow is not tied to a Maestro chat session.");
      return;
    }
    const response = await apiJson<{ conversation: MaestroSessionSummary }>(
      `/maestro/sessions/${run.conversation_id}`,
    );
    setMaestroPlan(response.conversation.active_plan ?? maestroPlan);
    setSchedulerStatusMessage("Referenced this workflow in the main Maestro channel.");
  };

  const runSchedulerTick = async () => {
    const response = await apiJson<{
      enqueued: SchedulerRun[];
      claimed: SchedulerQueueItem[];
      runnable_batches: SchedulerDashboard["runnable_batches"];
    }>("/scheduler/tick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner: "maestro-ui", claim_limit: 4, lease_seconds: 300 }),
    });
    setSchedulerStatusMessage(
      `Tick enqueued ${response.enqueued.length} run(s) and claimed ${response.claimed.length} item(s).`,
    );
    await loadSchedulerDashboard();
  };

  const runSchedulerWorker = async () => {
    const response = await apiJson<{
      enqueued: SchedulerRun[];
      claimed: SchedulerQueueItem[];
      executed: Array<{
        status: string;
        queue_item: SchedulerQueueItem;
        agent_run: SchedulerWorkerAgentRun | null;
      }>;
      runnable_batches: SchedulerDashboard["runnable_batches"];
    }>("/scheduler/worker/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        owner: "maestro-ui-worker",
        claim_limit: 4,
        lease_seconds: 300,
        execute_llm: executeMaestroLLM,
        auto_tool_loop: autoMaestroToolLoop,
        max_tool_iterations: 2,
      }),
    });
    const completed = response.executed.filter((item) => item.status === "completed").length;
    const blocked = response.executed.filter((item) => item.status === "blocked").length;
    const failed = response.executed.filter((item) => item.status === "failed").length;
    setSchedulerStatusMessage(
      `Worker enqueued ${response.enqueued.length}, claimed ${response.claimed.length}, completed ${completed}, blocked ${blocked}, failed ${failed}.`,
    );
    await loadSchedulerDashboard();
  };

  const triggerSchedulerEvent = async () => {
    const response = await apiJson<{ runs: SchedulerRun[] }>("/scheduler/triggers/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_type: schedulerDefinitionEvent,
        event_id: schedulerEventId || crypto.randomUUID(),
        event_payload: {
          id: schedulerEventId || crypto.randomUUID(),
          domain_key: schedulerDefinitionDomain,
          source: "maestro-ui",
        },
      }),
    });
    setSchedulerStatusMessage(`Event trigger enqueued ${response.runs.length} run(s).`);
    await loadSchedulerDashboard();
  };

  useEffect(() => {
    loadActiveSession().catch(() => {
      setMaestroStatus("Could not restore active Maestro session.");
    });
    loadSessionHistory().catch(() => undefined);
    loadSchedulerDashboard().catch(() => undefined);
  }, [loadActiveSession, loadSchedulerDashboard, loadSessionHistory]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (maestroBusy) return;
      pollActiveChannel().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [loadSchedulerDashboard, maestroBusy, pollActiveChannel]);

  const applyToolCallUpdate = (toolCall: MaestroToolCallResponse["tool_call"]) => {
    setMaestroRun((run) => {
      if (!run) return run;
      return {
        ...run,
        tool_activity: run.tool_activity.map((activity) =>
          activity.tool_call_id === toolCall.id
            ? {
                ...activity,
                status: toolCall.status,
                error_message: toolCall.error_message,
                details:
                  toolCall.status === "complete"
                    ? "Approved and executed."
                    : toolCall.status === "rejected"
                      ? "Rejected by Chris."
                      : activity.details,
              }
            : activity,
        ),
      };
    });
  };

  const markToolApprovalRunning = (toolCallId: string) => {
    let approvalAgentKey: string | null = null;
    setMaestroRun((run) => {
      if (!run) return run;
      return {
        ...run,
        tool_activity: run.tool_activity.map((activity) => {
          if (activity.tool_call_id !== toolCallId) return activity;
          approvalAgentKey = activity.agent_key;
          return {
            ...activity,
            status: "running",
            details: "Approved; running the tool now.",
            error_message: null,
          };
        }),
      };
    });
    setMaestroPlan((plan) => {
      if (!plan || !approvalAgentKey) return plan;
      const queueItems = (plan.scheduler.queue_items ?? []).map((item) =>
        item.agent_key === approvalAgentKey && item.status === "blocked"
          ? { ...item, status: "running", error_message: null }
          : item,
      );
      return {
        ...plan,
        scheduler: {
          ...plan.scheduler,
          status: "running",
          current_step: `Running approved tool for ${approvalAgentKey}.`,
          queue_items: queueItems,
        },
      };
    });
  };

  const approveToolCall = async (toolCallId: string) => {
    setBusyToolCallId(toolCallId);
    setMaestroBusy(true);
    markToolApprovalRunning(toolCallId);
    setMaestroStatus("Running approved tool. This can take a few minutes for Codex tasks.");
    try {
      const response = await apiJson<MaestroToolCallResponse>(
        `/maestro/tool-calls/${toolCallId}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            execute_llm: true,
            auto_tool_loop: true,
            max_tool_iterations: 2,
            conversation_id: activeConversationId,
          }),
        },
      );
      applyToolCallUpdate(response.tool_call);
      if (response.run) {
        setMaestroRun(response.run);
        setMaestroPlan(response.run.plan);
      }
      await pollActiveChannel().catch(() => {
        setChatMessages((messages) => [
          ...messages,
          { id: crypto.randomUUID(), sender: "maestro", content: response.message },
        ]);
      });
      setMaestroStatus(
        response.run
          ? `Workflow ${response.run.status}.`
          : response.tool_call.status === "complete"
            ? "Tool approved and run."
            : "Tool approval finished.",
      );
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Tool approval failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: crypto.randomUUID(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setBusyToolCallId(null);
      setMaestroBusy(false);
    }
  };

  const rejectToolCall = async (toolCallId: string) => {
    setBusyToolCallId(toolCallId);
    try {
      const response = await apiJson<MaestroToolCallResponse>(
        `/maestro/tool-calls/${toolCallId}/reject`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: "Rejected from Maestro UI.",
            conversation_id: activeConversationId,
          }),
        },
      );
      applyToolCallUpdate(response.tool_call);
      await pollActiveChannel().catch(() => {
        setChatMessages((messages) => [
          ...messages,
          { id: crypto.randomUUID(), sender: "maestro", content: response.message },
        ]);
      });
      setMaestroStatus("Tool rejected.");
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Tool rejection failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: crypto.randomUUID(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setBusyToolCallId(null);
    }
  };

  const sendMaestroMessage = async () => {
    if (!draftMessage.trim()) return;
    const outgoingMessage: ChatMessage = {
      id: crypto.randomUUID(),
      sender: "user",
      content: draftMessage.trim(),
    };
    const activePlanId = maestroPlan ? maestroPlan.parent_task_id : null;
    setMaestroBusy(true);
    setChatMessages((messages) => [...messages, outgoingMessage]);
    setDraftMessage("");
    if (isApprovalMessage(outgoingMessage.content) && pendingToolApprovals.length > 0) {
      if (pendingToolApprovals.length === 1 && pendingToolApprovals[0].tool_call_id) {
        await approveToolCall(pendingToolApprovals[0].tool_call_id);
      } else {
        setChatMessages((messages) => [
          ...messages,
          {
            id: crypto.randomUUID(),
            sender: "maestro",
            content:
              "I found multiple actions waiting for approval. Use the Approve button on the specific tool card you want me to run.",
          },
        ]);
        setMaestroStatus("Multiple approvals pending.");
      }
      setMaestroBusy(false);
      return;
    }
    try {
      const response = await apiJson<MaestroRespond>("/maestro/respond", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: outgoingMessage.content,
          active_plan_id: activePlanId,
          conversation_id: activeConversationId,
        }),
      });
      setMaestroPlan(response.plan ?? response.active_plan);
      setMaestroRun(null);
      if (response.conversation) setActiveConversationId(response.conversation.id);
      if (response.conversation?.messages?.length) {
        setChatMessages(response.conversation.messages);
      } else {
        setChatMessages((messages) => [
          ...messages,
          {
            id: crypto.randomUUID(),
            sender: "maestro",
            content: response.message,
          },
        ]);
      }
      setMaestroStatus(
        response.kind === "chat_only"
          ? "Answered directly."
          : response.kind === "rfi_answered"
            ? "RFI answer applied."
            : response.kind === "routed"
              ? "Routed context applied."
          : response.kind === "refined"
            ? "Plan refined."
            : "Proposed plan ready for approval.",
      );
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
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
          body: JSON.stringify({
            execute_llm: executeMaestroLLM,
            auto_tool_loop: autoMaestroToolLoop,
            max_tool_iterations: 2,
            conversation_id: activeConversationId,
          }),
        },
      );
      setMaestroRun(response.run);
      setMaestroPlan(response.run.plan);
      await pollActiveChannel().catch(() => {
        setChatMessages((messages) => [
          ...messages,
          {
            id: crypto.randomUUID(),
            sender: "maestro",
            content:
              response.run.status === "completed"
                ? response.run.chat_summary
                : `The workflow finished with status ${response.run.status}.\n\n${response.run.chat_summary}`,
          },
        ]);
      });
      setMaestroStatus(
        response.run.status === "completed" ? "Workflow completed." : "Workflow finished.",
      );
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
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
              conversation_id: activeConversationId,
              messages: chatMessages.map((message) => ({
                sender: message.sender,
                content: message.content,
              })),
            }),
          },
        );
        stagedArtifactPath = response.staged_artifact_path;
      }
      const active = await apiJson<{ conversation: MaestroSessionSummary }>("/maestro/sessions/active");
      setActiveConversationId(active.conversation.id);
      setChatMessages(active.conversation.messages ?? chatMessages);
      setDraftMessage("");
      setMaestroPlan(null);
      setMaestroRun(null);
      loadSessionHistory().catch(() => undefined);
      setMaestroStatus(
        stagedArtifactPath
          ? "Channel checkpoint staged for memory curation."
          : "Maestro channel ready.",
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
                {activeWorkflowSummary
                  ? `${activeWorkflowSummary.schedulerStatus} scheduler`
                  : "No active workflow"}
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
                <div className="chat-actions">
                  <button
                    className="icon-button"
                    aria-label="Previous sessions"
                    title="Previous sessions"
                    type="button"
                    onClick={() => setShowSessionHistory((visible) => !visible)}
                    disabled={sessionHistory.length === 0}
                  >
                    <Clock3 size={18} />
                  </button>
                  <button
                    className="icon-button"
                    aria-label="New session"
                    title="Checkpoint channel"
                    onClick={startNewMaestroSession}
                    disabled={maestroBusy || (chatMessages.length === 0 && !maestroPlan && !maestroRun)}
                  >
                    <Plus size={18} />
                  </button>
                </div>
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
                {maestroBusy && (
                  <div className="message maestro-message working-message" aria-live="polite">
                    <span>Maestro</span>
                  <p>
                      {busyToolCallId ? "Running approved tool" : "Conducting"}
                      <span className="working-dots" aria-hidden="true">
                        <span />
                        <span />
                        <span />
                      </span>
                    </p>
                  </div>
                )}
              </div>

              {showSessionHistory && sessionHistory.length > 0 && (
                <div className="session-history" aria-label="Previous Maestro sessions">
                  <span>Previous sessions</span>
                  {sessionHistory.slice(0, 8).map((session) => (
                    <div className="session-history-row" key={session.id}>
                      <button
                        type="button"
                        onClick={async () => {
                          const response = await apiJson<{ conversation: MaestroSessionSummary }>(
                            `/maestro/sessions/${session.id}`,
                          );
                          applyConversation(response.conversation);
                          setShowSessionHistory(false);
                          setMaestroStatus(
                            response.conversation.active_plan
                              ? "Viewing historical segment with its workflow restored."
                              : "Viewing historical segment.",
                          );
                        }}
                      >
                        {session.title}
                      </button>
                      <button
                        type="button"
                        className="session-archive-button"
                        onClick={() => archiveSession(session.id)}
                      >
                        Archive
                      </button>
                    </div>
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
                <textarea
                  value={draftMessage}
                  onChange={(event) => {
                    setDraftMessage(event.target.value);
                    event.currentTarget.style.height = "auto";
                    event.currentTarget.style.height = `${event.currentTarget.scrollHeight}px`;
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      sendMaestroMessage();
                      event.currentTarget.style.height = "auto";
                    }
                  }}
                  placeholder="Ask Maestro to plan and coordinate..."
                  aria-label="Message Maestro"
                  rows={1}
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
                <label className="toggle-row">
                  <input
                    type="checkbox"
                    checked={autoMaestroToolLoop}
                    onChange={(event) => setAutoMaestroToolLoop(event.target.checked)}
                    disabled={!executeMaestroLLM}
                  />
                  Let agents plan safe tools
                </label>
                <span>
                  {maestroBusy
                    ? busyToolCallId
                      ? "Running approved tool. Long Codex tasks may take a few minutes."
                      : "Conducting"
                    : maestroPlan?.scheduler.current_step || maestroStatus}
                </span>
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
                    <span>{maestroPlan.workflow_graph.edges?.length ?? 0} edges</span>
                    <span>{String(maestroPlan.scheduler.status ?? "queue")}</span>
                    {maestroPlan.scheduler.current_step && (
                      <span>{maestroPlan.scheduler.current_step}</span>
                    )}
                  </div>
                  {queueStages.length > 0 && (
                    <div className="workflow-map" aria-label="Workflow dependency map">
                      <h4>Workflow map</h4>
                      <div className="workflow-map-scroll">
                        {queueStages.map((stage, index) => (
                          <div className="workflow-map-stage" key={`queue-stage-${stage.stageIndex}`}>
                            <div className="workflow-map-heading">
                              <span>Stage {stage.stageIndex}</span>
                              <span>{stage.items.length > 1 ? "Parallel" : "Single"}</span>
                            </div>
                            <div className="workflow-map-items">
                              {stage.items.map((item) => (
                                <button
                                  className={`workflow-node node-${item.status} ${
                                    expandedWorkflowNodeId === item.id ? "node-selected" : ""
                                  }`}
                                  key={item.id}
                                  type="button"
                                  onClick={() =>
                                    setExpandedWorkflowNodeId((selectedId) =>
                                      selectedId === item.id ? null : item.id,
                                    )
                                  }
                                >
                                  <span>{item.status}</span>
                                  <strong>{item.agent_name}</strong>
                                  <small>{item.work_item_ids.join(", ")}</small>
                                </button>
                              ))}
                            </div>
                            {index < queueStages.length - 1 && (
                              <ChevronRight className="workflow-arrow" size={18} />
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {routedPlanItems.length > 0 && (
                    <div className="routed-plan-panel">
                      <h4>Routed items</h4>
                      <div className="workflow-detail-grid">
                        {routedPlanItems.map((item) => (
                          <article className="mini-row" key={item.id}>
                            <span>
                              {item.id} / {item.type} / {item.priority} /{" "}
                              {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                            </span>
                            <p>{item.title}</p>
                            <p>{item.description}</p>
                            {item.dependencies.length > 0 && (
                              <span>Depends on: {item.dependencies.join(", ")}</span>
                            )}
                            <div className="preview-meta">
                              <span>{item.can_log_directly ? "board route" : "plan context"}</span>
                              <span>
                                {item.blocks_execution
                                  ? "asked in chat"
                                  : item.needs_user_input
                                    ? "needs Chris"
                                    : "no RFI"}
                              </span>
                            </div>
                          </article>
                        ))}
                      </div>
                    </div>
                  )}
                  {selectedWorkflowItem && (
                    <div className="workflow-detail-panel">
                      <div className="workflow-detail-heading">
                        <div>
                          <span>
                            Stage {selectedWorkflowItem.stage_index} / {selectedWorkflowItem.status}
                          </span>
                          <h4>{selectedWorkflowItem.agent_name}</h4>
                        </div>
                        <button type="button" onClick={() => setExpandedWorkflowNodeId(null)}>
                          Close
                        </button>
                      </div>
                      <p>{selectedWorkflowItem.objective}</p>
                      <div className="preview-meta">
                        <span>{domainLabels[selectedWorkflowItem.domain_key] ?? selectedWorkflowItem.domain_key}</span>
                        <span>{selectedWorkflowItem.priority}</span>
                        <span>{selectedWorkflowItem.work_item_ids.join(", ") || "no work items"}</span>
                        <span>{selectedWorkflowItem.retry_count ?? 0} retries</span>
                        {selectedWorkflowItem.depends_on_work_item_ids.length > 0 && (
                          <span>Waits for {selectedWorkflowItem.depends_on_work_item_ids.join(", ")}</span>
                        )}
                        {selectedWorkflowItem.child_task_id && (
                          <span>Task {selectedWorkflowItem.child_task_id.slice(0, 8)}</span>
                        )}
                      </div>
                      {selectedWorkflowItem.error_message && (
                        <p className="evaluation-note">{selectedWorkflowItem.error_message}</p>
                      )}
                      {selectedWorkflowSubtask?.rationale && (
                        <p className="evaluation-note">{selectedWorkflowSubtask.rationale}</p>
                      )}
                      <div className="workflow-detail-grid">
                        {selectedWorkflowWorkItems.map((item) => (
                          <article className="mini-row" key={item.id}>
                            <span>
                              {item.id} / {item.type} / {item.priority} /{" "}
                              {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                            </span>
                            <p>{item.title}</p>
                            <p>{item.description}</p>
                            {item.dependencies.length > 0 && (
                              <span>Depends on: {item.dependencies.join(", ")}</span>
                            )}
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
                    </div>
                  )}
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
                  {maestroRun.tool_activity.length > 0 && (
                    <div className="tool-activity-list">
                      <h4>Tool activity</h4>
                      {maestroRun.tool_activity.map((activity, index) => (
                        <article
                          className={`tool-activity-item tool-activity-${activity.status}`}
                          key={`${activity.agent_key}-${activity.tool_name}-${index}`}
                        >
                          <strong>{activity.agent_name}</strong>
                          <span>{activity.tool_name}</span>
                          <p>
                            {activity.status === "complete"
                              ? "Completed"
                              : activity.status === "running"
                                ? "Running"
                              : activity.status === "approval_required"
                                ? "Needs approval"
                              : activity.status === "failed"
                                ? "Failed"
                                : activity.status === "rejected"
                                  ? "Rejected"
                                  : activity.status}
                            {activity.details ? ` - ${activity.details}` : ""}
                            {activity.error_message ? ` - ${activity.error_message}` : ""}
                          </p>
                          {activity.status === "approval_required" && activity.tool_call_id && (
                            <div className="tool-approval-actions">
                              <button
                                className="planner-action"
                                onClick={() => approveToolCall(activity.tool_call_id!)}
                                disabled={busyToolCallId === activity.tool_call_id}
                              >
                                Approve
                              </button>
                              <button
                                className="danger-action"
                                onClick={() => rejectToolCall(activity.tool_call_id!)}
                                disabled={busyToolCallId === activity.tool_call_id}
                              >
                                Reject
                              </button>
                            </div>
                          )}
                          {activity.tool_name === "codex.task.run" &&
                            (() => {
                              const review = codexReviewPayload(activity);
                              if (!review.hasReview) return null;
                              return (
                                <div className="tool-review-panel">
                                  <div className="preview-meta">
                                    {review.prNumber ? <span>PR #{String(review.prNumber)}</span> : null}
                                    {review.branch ? <span>{review.branch}</span> : null}
                                    {review.baseBranch ? <span>base {review.baseBranch}</span> : null}
                                  </div>
                                  {review.prTitle && <strong>{review.prTitle}</strong>}
                                  {review.prUrl && (
                                    <a href={review.prUrl} target="_blank" rel="noreferrer">
                                      Open PR
                                    </a>
                                  )}
                                  {review.prBody && (
                                    <details>
                                      <summary>PR body</summary>
                                      <pre>{review.prBody}</pre>
                                    </details>
                                  )}
                                  {review.changedFiles.length > 0 && (
                                    <details>
                                      <summary>{review.changedFiles.length} changed files</summary>
                                      <ul>
                                        {review.changedFiles.map((file) => (
                                          <li key={file}>{file}</li>
                                        ))}
                                      </ul>
                                    </details>
                                  )}
                                  {review.diffSummary && (
                                    <details>
                                      <summary>Diff summary</summary>
                                      <pre>{review.diffSummary}</pre>
                                    </details>
                                  )}
                                  {review.finalMessage && (
                                    <details>
                                      <summary>Codex report</summary>
                                      <pre>{review.finalMessage}</pre>
                                    </details>
                                  )}
                                </div>
                              );
                            })()}
                        </article>
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
                <button className="planner-action" disabled>
                  <CalendarDays size={17} />
                  Adjust
                </button>
              </div>

              <div className="empty-planner-state">
                <CalendarDays size={20} />
                <p>
                  Daily planner is ready for the morning standup workflow. Scheduled blocks will
                  appear here once Maestro starts producing a real daily plan.
                </p>
              </div>
            </section>

            <section className="reports-panel" aria-labelledby="reports-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Scheduler</p>
                  <h3 id="reports-heading">Queue</h3>
                </div>
                <Clock3 size={18} />
              </div>
              <div className="scheduler-control-panel">
                <div className="workflow-detail-heading">
                  <div>
                    <span>Control</span>
                    <h4>Workflow triggers</h4>
                  </div>
                  <button type="button" onClick={runSchedulerTick}>
                    Run tick
                  </button>
                  <button type="button" onClick={runSchedulerWorker}>
                    Run worker
                  </button>
                  {selectedSchedulerDefinition && (
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedSchedulerDefinition(null);
                        setSchedulerStatusMessage("Creating a new workflow trigger.");
                      }}
                    >
                      New trigger
                    </button>
                  )}
                </div>
                <div className="scheduler-form-grid">
                  <label>
                    <span>Mode</span>
                    <select
                      value={schedulerDefinitionMode}
                      onChange={(event) =>
                        setSchedulerDefinitionMode(event.target.value as "recurring" | "event")
                      }
                    >
                      <option value="recurring">Recurring</option>
                      <option value="event">Event</option>
                    </select>
                  </label>
                  <label>
                    <span>Name</span>
                    <input
                      value={schedulerDefinitionName}
                      onChange={(event) => setSchedulerDefinitionName(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>Domain</span>
                    <select
                      value={schedulerDefinitionDomain}
                      onChange={(event) => setSchedulerDefinitionDomain(event.target.value)}
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
                    <span>{schedulerDefinitionMode === "event" ? "Event" : "Time"}</span>
                    <input
                      value={
                        schedulerDefinitionMode === "event"
                          ? schedulerDefinitionEvent
                          : schedulerDefinitionTime
                      }
                      onChange={(event) =>
                        schedulerDefinitionMode === "event"
                          ? setSchedulerDefinitionEvent(event.target.value)
                          : setSchedulerDefinitionTime(event.target.value)
                      }
                    />
                  </label>
                </div>
                <label className="scheduler-wide-field">
                  <span>Objective</span>
                  <input
                    value={schedulerDefinitionObjective}
                    onChange={(event) => setSchedulerDefinitionObjective(event.target.value)}
                  />
                </label>
                <div className="scheduler-action-row">
                  <button type="button" onClick={createSchedulerDefinition}>
                    {selectedSchedulerDefinition ? "Update trigger" : "Save trigger"}
                  </button>
                  {schedulerDefinitionMode === "event" && (
                    <>
                      <input
                        value={schedulerEventId}
                        onChange={(event) => setSchedulerEventId(event.target.value)}
                        aria-label="Event id"
                      />
                      <button type="button" onClick={triggerSchedulerEvent}>
                        Trigger event
                      </button>
                    </>
                  )}
                </div>
                {schedulerStatusMessage && (
                  <p className="evaluation-note">{schedulerStatusMessage}</p>
                )}
              </div>
              {activeWorkflowSummary ? (
                <div className="scheduler-visualizer">
                  <article className="workflow-summary-card">
                    <span>{activeWorkflowSummary.schedulerStatus}</span>
                    <h4>{activeWorkflowSummary.title}</h4>
                    <div className="preview-meta">
                      <span>{activeWorkflowSummary.status}</span>
                      <span>{activeWorkflowSummary.queueItems.length} queue items</span>
                      <span>{activeWorkflowSummary.stageCount} stages</span>
                      <span>{activeWorkflowSummary.completed} complete</span>
                      {activeWorkflowSummary.running > 0 && (
                        <span>{activeWorkflowSummary.running} active/pending</span>
                      )}
                      {activeWorkflowSummary.blocked > 0 && (
                        <span>{activeWorkflowSummary.blocked} blocked</span>
                      )}
                      {activeWorkflowSummary.failed > 0 && (
                        <span>{activeWorkflowSummary.failed} failed</span>
                      )}
                    </div>
                  </article>
                  {queueStages.length > 0 && (
                    <div className="workflow-map compact-map" aria-label="Scheduler workflow map">
                      <div className="workflow-map-scroll">
                        {queueStages.map((stage, index) => (
                          <div className="workflow-map-stage" key={`scheduler-stage-${stage.stageIndex}`}>
                            <div className="workflow-map-heading">
                              <span>Stage {stage.stageIndex}</span>
                              <span>{stage.items.length > 1 ? "Parallel" : "Single"}</span>
                            </div>
                            <div className="workflow-map-items">
                              {stage.items.map((item) => (
                                <button
                                  className={`workflow-node node-${item.status} ${
                                    expandedWorkflowNodeId === item.id ? "node-selected" : ""
                                  }`}
                                  key={item.id}
                                  type="button"
                                  onClick={() =>
                                    setExpandedWorkflowNodeId((selectedId) =>
                                      selectedId === item.id ? null : item.id,
                                    )
                                  }
                                >
                                  <span>{item.status}</span>
                                  <strong>{item.agent_name}</strong>
                                  <small>{item.work_item_ids.join(", ")}</small>
                                </button>
                              ))}
                            </div>
                            {index < queueStages.length - 1 && (
                              <ChevronRight className="workflow-arrow" size={18} />
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {selectedWorkflowItem && (
                    <div className="workflow-detail-panel">
                      <div className="workflow-detail-heading">
                        <div>
                          <span>
                            Stage {selectedWorkflowItem.stage_index} / {selectedWorkflowItem.status}
                          </span>
                          <h4>{selectedWorkflowItem.agent_name}</h4>
                        </div>
                        <button type="button" onClick={() => setExpandedWorkflowNodeId(null)}>
                          Close
                        </button>
                      </div>
                      <p>{selectedWorkflowItem.objective}</p>
                      <div className="preview-meta">
                        <span>{domainLabels[selectedWorkflowItem.domain_key] ?? selectedWorkflowItem.domain_key}</span>
                        <span>{selectedWorkflowItem.priority}</span>
                        <span>{selectedWorkflowItem.work_item_ids.join(", ") || "no work items"}</span>
                        <span>{selectedWorkflowItem.retry_count ?? 0} retries</span>
                        {selectedWorkflowItem.depends_on_work_item_ids.length > 0 && (
                          <span>Waits for {selectedWorkflowItem.depends_on_work_item_ids.join(", ")}</span>
                        )}
                        {selectedWorkflowItem.child_task_id && (
                          <span>Task {selectedWorkflowItem.child_task_id.slice(0, 8)}</span>
                        )}
                      </div>
                      {selectedWorkflowItem.error_message && (
                        <p className="evaluation-note">{selectedWorkflowItem.error_message}</p>
                      )}
                      {selectedWorkflowSubtask?.rationale && (
                        <p className="evaluation-note">{selectedWorkflowSubtask.rationale}</p>
                      )}
                      <div className="workflow-detail-grid">
                        {selectedWorkflowWorkItems.map((item) => (
                          <article className="mini-row" key={`queue-${item.id}`}>
                            <span>
                              {item.id} / {item.type} / {item.priority} /{" "}
                              {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                            </span>
                            <p>{item.title}</p>
                            <p>{item.description}</p>
                            {item.dependencies.length > 0 && (
                              <span>Depends on: {item.dependencies.join(", ")}</span>
                            )}
                          </article>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="empty-state">
                  No scheduled or running workflows yet. Maestro workflows will appear here after
                  they are proposed, queued, or executed.
                </p>
              )}
              {schedulerDashboard && schedulerDashboard.runs.length > 0 && (
                <div className="scheduler-run-list" aria-label="Durable scheduler runs">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>Durable queue</span>
                      <h4>Scheduled and recent workflows</h4>
                    </div>
                    <span>{schedulerDashboard.runnable_batches.length} runnable batch(es)</span>
                  </div>
                  {schedulerDashboard.runs.slice(0, 5).map((run) => {
                    const completed = run.queue_items.filter((item) => item.status === "completed").length;
                    const blocked = run.queue_items.filter((item) => item.status === "blocked").length;
                    const runnableBatch = schedulerDashboard.runnable_batches.find(
                      (batch) => batch.workflow_run_id === run.id,
                    );
                    return (
                      <article className="workflow-summary-card compact-run-card" key={run.id}>
                        <button
                          type="button"
                          className="card-reset"
                          onClick={() => selectSchedulerRun(run.id)}
                        >
                          <span>{run.status}</span>
                          <h4>{run.summary || "Maestro workflow"}</h4>
                        </button>
                        <div className="preview-meta">
                          <span>{run.priority}</span>
                          <span>{run.fairness_group || "global"} fairness</span>
                          <span>{run.queue_items.length} queued</span>
                          <span>{completed} complete</span>
                          {blocked > 0 && <span>{blocked} blocked</span>}
                          {runnableBatch && (
                            <span>{runnableBatch.parallel_ready.length} parallel-ready</span>
                          )}
                        </div>
                        <div className="scheduler-action-row compact-actions">
                          <button type="button" onClick={() => selectSchedulerRun(run.id)}>
                            Inspect
                          </button>
                          {run.conversation_id && (
                            <button type="button" onClick={() => reenterSchedulerRunSession(run)}>
                              Re-enter session
                            </button>
                          )}
                          <button type="button" onClick={() => archiveSchedulerRun(run.id)}>
                            Archive
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
              {selectedSchedulerRun && (
                <div className="workflow-detail-panel scheduler-detail-panel">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>{selectedSchedulerRun.status}</span>
                      <h4>{selectedSchedulerRun.summary || "Workflow run"}</h4>
                    </div>
                    <button type="button" onClick={() => setSelectedSchedulerRun(null)}>
                      Close
                    </button>
                  </div>
                  <div className="preview-meta">
                    <span>{selectedSchedulerRun.source_type}</span>
                    <span>{selectedSchedulerRun.priority}</span>
                    <span>{selectedSchedulerRun.fairness_group || "global"} fairness</span>
                    <span>{selectedSchedulerRun.queue_items.length} queue items</span>
                    {selectedSchedulerRun.workflow_definition_id && <span>Recurring run</span>}
                  </div>
                  {selectedSchedulerRun.error_message && (
                    <p className="evaluation-note">{selectedSchedulerRun.error_message}</p>
                  )}
                  <div className="workflow-detail-grid">
                    {selectedSchedulerRun.queue_items.map((item) => (
                      <article className="mini-row" key={item.id}>
                        <span>
                          Stage {item.stage_index} / {item.status} /{" "}
                          {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                        </span>
                        <p>{item.objective}</p>
                        <div className="preview-meta">
                          <span>{item.agent_name ?? item.agent_key ?? "Unassigned"}</span>
                          <span>{item.priority}</span>
                          {item.dependency_keys.length > 0 && (
                            <span>Waits for {item.dependency_keys.join(", ")}</span>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                  {(selectedSchedulerRun.events ?? []).length > 0 && (
                    <div className="scheduler-event-list">
                      <h4>Run history</h4>
                      {selectedSchedulerRun.events?.map((event) => (
                        <article className="mini-row" key={event.id}>
                          <span>{event.event_type}</span>
                          <p>{event.message}</p>
                        </article>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {schedulerDashboard && schedulerDashboard.definitions.length > 0 && (
                <div className="scheduler-run-list" aria-label="Scheduled workflow definitions">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>Triggers</span>
                      <h4>Recurring and event workflows</h4>
                    </div>
                    <span>{schedulerDashboard.definitions.length} configured</span>
                  </div>
                  {schedulerDashboard.definitions.slice(0, 5).map((definition) => (
                    <article className="workflow-summary-card compact-run-card" key={definition.id}>
                      <button
                        type="button"
                        className="card-reset"
                        onClick={() => selectSchedulerDefinition(definition)}
                      >
                        <span>{definition.trigger_type}</span>
                        <h4>{definition.name}</h4>
                      </button>
                      <div className="preview-meta">
                        <span>{definition.is_active ? "active" : "paused"}</span>
                        <span>{definition.priority}</span>
                        <span>{definition.fairness_group || definition.domain_key || "global"} fairness</span>
                        {typeof definition.trigger_config.next_run_at === "string" && (
                          <span>Next {definition.trigger_config.next_run_at}</span>
                        )}
                        {typeof definition.trigger_config.event_type === "string" && (
                          <span>{definition.trigger_config.event_type}</span>
                        )}
                      </div>
                      <div className="scheduler-action-row compact-actions">
                        <button type="button" onClick={() => selectSchedulerDefinition(definition)}>
                          Edit schedule
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
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
  const [toolRequestJson, setToolRequestJson] = useState("[]");
  const [promptPreview, setPromptPreview] = useState<PromptPackage | null>(null);
  const [runPreview, setRunPreview] = useState<AgentRun | null>(null);
  const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
  const [stageRunArtifact, setStageRunArtifact] = useState(false);
  const [autoToolLoop, setAutoToolLoop] = useState(false);
  const [agentScheduleName, setAgentScheduleName] = useState("Daily agent check-in");
  const [agentScheduleTime, setAgentScheduleTime] = useState("08:00");
  const [agentScheduleObjective, setAgentScheduleObjective] = useState(
    "Review relevant context and produce a short status report.",
  );
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
            Object.keys(toolPermissions).map((key) => [
              key,
              {
                permission: "use",
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
      const parsedToolRequests = JSON.parse(toolRequestJson || "[]");
      if (!Array.isArray(parsedToolRequests)) {
        throw new Error("Tool requests JSON must be an array.");
      }
      const response = await apiJson<{ run: AgentRun }>(`/agents/${selectedAgent.key}/run-once`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_instruction: promptTask,
          query_text: promptTask,
          use_semantic: true,
          stage_interaction: stageRunArtifact,
          execute_llm: true,
          tool_requests: parsedToolRequests,
          auto_tool_loop: autoToolLoop,
          max_tool_iterations: 2,
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

  const scheduleSelectedAgent = async () => {
    if (!selectedAgent) return;
    setBusy(true);
    try {
      const keyBase = `${selectedAgent.key}-${agentScheduleName}`
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "");
      await apiJson<{ definition: SchedulerDefinition }>("/scheduler/definitions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: keyBase || `${selectedAgent.key}-scheduled-work`,
          name: agentScheduleName,
          domain_key: domainKey,
          description: `Recurring task for ${selectedAgent.name}.`,
          trigger_type: "recurring",
          trigger_config: {
            time_of_day: agentScheduleTime,
            interval_minutes: 1440,
            source: "agent_detail",
          },
          workflow_spec: {
            queue_items: [
              {
                id: `${selectedAgent.key}-scheduled-work`,
                objective: agentScheduleObjective,
                domain_key: domainKey,
                agent_key: selectedAgent.key,
                required_tools: selectedAgent.allowed_tools.map((tool) => tool.key),
              },
            ],
          },
          fairness_group: domainKey,
          priority: "normal",
          is_active: true,
        }),
      });
      setStatusMessage("Recurring agent task scheduled.");
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Agent schedule save failed.");
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
                    <small>{tool.description}</small>
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

      <section className="memory-panel admin-panel wide-panel" aria-labelledby="agent-schedule-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Recurring work</p>
            <h3 id="agent-schedule-heading">{selectedAgent?.name ?? "No agent selected"}</h3>
          </div>
          <CalendarDays size={18} />
        </div>
        {selectedAgent ? (
          <div className="scheduler-control-panel embedded-scheduler">
            <div className="scheduler-form-grid">
              <label>
                <span>Name</span>
                <input
                  value={agentScheduleName}
                  onChange={(event) => setAgentScheduleName(event.target.value)}
                />
              </label>
              <label>
                <span>Time</span>
                <input
                  value={agentScheduleTime}
                  onChange={(event) => setAgentScheduleTime(event.target.value)}
                />
              </label>
            </div>
            <label className="scheduler-wide-field">
              <span>Objective</span>
              <input
                value={agentScheduleObjective}
                onChange={(event) => setAgentScheduleObjective(event.target.value)}
              />
            </label>
            <button className="planner-action" onClick={scheduleSelectedAgent} disabled={busy}>
              Schedule recurring task
            </button>
          </div>
        ) : (
          <p className="empty-state">Select an agent to schedule recurring work.</p>
        )}
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
          <label>
            Tool requests JSON
            <textarea
              value={toolRequestJson}
              onChange={(event) => setToolRequestJson(event.target.value)}
              placeholder='[{"tool_key":"github.issue.search","payload":{"query":"tool integration","limit":5}}]'
            />
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
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={autoToolLoop}
              onChange={(event) => setAutoToolLoop(event.target.checked)}
            />
            Let agent plan safe tool calls
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
            {runPreview.tool_loop?.enabled === true && (
              <pre>{JSON.stringify(runPreview.tool_loop, null, 2)}</pre>
            )}
            {(runPreview.tool_calls ?? []).map((toolCall) => (
              <div key={toolCall.id} className="tool-call-preview">
                <p>
                  {toolCall.tool_name}: {toolCall.status}
                </p>
                {toolCall.error_message && <p>{toolCall.error_message}</p>}
                {toolCall.output_payload && (
                  <pre>{JSON.stringify(toolCall.output_payload, null, 2)}</pre>
                )}
              </div>
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
  const [selectedToolKey, setSelectedToolKey] = useState("github");
  const [expandedToolFamilies, setExpandedToolFamilies] = useState<Record<string, boolean>>({
    github: true,
  });
  const [connectionDomain, setConnectionDomain] = useState("praxis");
  const [connectionName, setConnectionName] = useState("Praxis memory retrieval");
  const [connectionAuthType, setConnectionAuthType] = useState("service");
  const [connectionConfig, setConnectionConfig] = useState("{}");
  const [statusMessage, setStatusMessage] = useState("Ready");

  const selectedTool = tools.find((tool) => tool.key === selectedToolKey) ?? tools[0] ?? null;
  const selectedConnectionToolKey = selectedTool?.key.startsWith("github.")
    ? "github"
    : selectedTool?.key;
  const toolFamilies = useMemo(() => {
    const providerKeys = new Set(
      tools.filter((tool) => !tool.key.includes(".")).map((tool) => tool.key),
    );
    const families = tools
      .filter((tool) => providerKeys.has(tool.key))
      .map((provider) => ({
        provider,
        children: tools.filter((tool) => tool.key.startsWith(`${provider.key}.`)),
      }));
    const childKeys = new Set(
      families.flatMap((family) => family.children.map((tool) => tool.key)),
    );
    const standalone = tools.filter(
      (tool) =>
        !childKeys.has(tool.key) && !families.some((family) => family.provider.key === tool.key),
    );
    return { families, standalone };
  }, [tools]);
  const selectedToolConnections = connections.filter(
    (connection) => connection.tool_key === selectedConnectionToolKey,
  );
  const selectedToolAgents = useMemo(() => {
    if (!selectedTool) return [];
    if (selectedTool.key === "github") {
      const githubAgents = tools
        .filter((tool) => tool.key.startsWith("github."))
        .flatMap((tool) => tool.authorized_agents);
      const unique = new Map<string, ToolRegistryItem["authorized_agents"][number]>();
      githubAgents.forEach((agent) => {
        unique.set(`${agent.domain_key}-${agent.agent_key}`, agent);
      });
      return Array.from(unique.values()).sort((a, b) =>
        `${a.domain_key}-${a.agent_key}`.localeCompare(`${b.domain_key}-${b.agent_key}`),
      );
    }
    return selectedTool.authorized_agents;
  }, [selectedTool, tools]);
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
      setSelectedToolKey(
        toolResponse.tools.some((tool) => tool.key === "github")
          ? "github"
          : (toolResponse.tools[0]?.key ?? "memory.context_bundle"),
      );
    }
  }, [selectedToolKey]);

  useEffect(() => {
    if (!selectedTool) return;
    const existing = connections.find(
      (connection) =>
        connection.tool_key === selectedConnectionToolKey &&
        connection.domain_key === connectionDomain,
    );
    if (existing) {
      setConnectionName(existing.display_name);
      setConnectionAuthType(existing.auth_type);
      setConnectionConfig(JSON.stringify(existing.config, null, 2));
      return;
    }
    const isGitHub = selectedConnectionToolKey === "github";
    setConnectionName(
      `${domainLabels[connectionDomain] ?? connectionDomain} ${isGitHub ? "GitHub" : selectedTool.name}`,
    );
    setConnectionAuthType(isGitHub ? "gh_cli" : "service");
    setConnectionConfig(
      isGitHub
        ? JSON.stringify(
            {
              repo: "Caliperti1/Maestro",
              env_token_name: "",
            },
            null,
            2,
          )
        : "{}",
    );
  }, [connectionDomain, connections, selectedConnectionToolKey, selectedTool?.key]);

  useEffect(() => {
    if (!selectedTool) return;
    setConnectionDomain(selectedToolConnections[0]?.domain_key ?? "praxis");
  }, [selectedTool?.key]);

  const selectConnection = (domainKey: string) => {
    setConnectionDomain(domainKey);
  };

  const toggleToolFamily = (familyKey: string) => {
    setExpandedToolFamilies((current) => ({
      ...current,
      [familyKey]: !current[familyKey],
    }));
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
          tool_key: selectedConnectionToolKey,
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
          {toolFamilies.families.map(({ provider, children }) => (
            <div className="tool-family-group" key={provider.key}>
              <div
                className={
                  provider.key === selectedTool?.key
                    ? "tool-registry-row tool-family-row active"
                    : "tool-registry-row tool-family-row"
                }
              >
                <button
                  className="tool-family-main selectable"
                  onClick={() => setSelectedToolKey(provider.key)}
                >
                  <div>
                    <span>Tool family</span>
                    <h4>{provider.name}</h4>
                    <p>{provider.description}</p>
                  </div>
                  <div className="preview-meta">
                    <span>{provider.connected_domains.length} connected domains</span>
                    <span>{children.length} tools</span>
                  </div>
                </button>
                <button
                  className="icon-button"
                  onClick={() => toggleToolFamily(provider.key)}
                  title={expandedToolFamilies[provider.key] ? "Collapse tools" : "Expand tools"}
                >
                  <ChevronRight
                    className={expandedToolFamilies[provider.key] ? "expanded-icon" : ""}
                    size={18}
                  />
                </button>
              </div>
              {expandedToolFamilies[provider.key] && (
                <div className="tool-family-children">
                  {children.map((tool) => (
                    <button
                      className={
                        tool.key === selectedTool?.key
                          ? "tool-child-row selectable active"
                          : "tool-child-row selectable"
                      }
                      key={tool.key}
                      onClick={() => setSelectedToolKey(tool.key)}
                    >
                      <div>
                        <span>{tool.exclusive ? "Exclusive / queued" : "Shared tool"}</span>
                        <h4>{tool.name}</h4>
                        <p>{tool.description}</p>
                      </div>
                      <div className="preview-meta">
                        <span>{tool.connected_domains.length} connected domains</span>
                        <span>{tool.authorized_agents.length} agents</span>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
          {toolFamilies.standalone.map((tool) => (
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
            {selectedTool.key === "github" && (
              <p className="memory-status">
                Edit the shared GitHub repo and credential config here. Every GitHub child tool in
                this domain inherits it unless a more specific override is added later.
              </p>
            )}
            {selectedTool.key.startsWith("github.") && (
              <p className="memory-status">
                GitHub tools share one domain connection named <strong>GitHub</strong>. Save repo
                and token env config once here, then every GitHub tool can inherit it.
              </p>
            )}
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
                  <option value="gh_cli">GitHub CLI</option>
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
