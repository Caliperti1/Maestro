import {
  Archive,
  Bot,
  Building2,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  ExternalLink,
  FileText,
  HardDriveUpload,
  Inbox,
  ListTodo,
  GitPullRequest,
  Mail,
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
  Users,
  Wrench,
  X,
} from "lucide-react";
import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import timeGridPlugin from "@fullcalendar/timegrid";
import listPlugin from "@fullcalendar/list";
import interactionPlugin, {
  type EventResizeDoneArg,
} from "@fullcalendar/interaction";
import rrulePlugin from "@fullcalendar/rrule";
import type { DateSelectArg, EventDropArg, EventInput } from "@fullcalendar/core";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { API_BASE_URL, apiJson, websocketUrl } from "./api";
import { CalendarErrorBoundary } from "./CalendarErrorBoundary";
import {
  domainKeysByLabel,
  domainLabels,
  domains,
  dropboxDomainDefaults,
  hiddenRoutedStatuses,
  routedGroups,
} from "./constants";
import type {
  ActiveSurface,
  AgentRun,
  AgentSpec,
  AgentTask,
  ChatMessage,
  ContactHydrationCandidate,
  ContactHydrationJob,
  DomainContext,
  DropboxDomain,
  GmailTriggerStatus,
  IngestionHealth,
  MaestroPlan,
  MaestroRespond,
  MaestroRun,
  MaestroSessionSummary,
  MaestroSubtask,
  MaestroToolCallResponse,
  MaestroQueueItem,
  MemoryArtifact,
  MemoryItem,
  MemoryPreview,
  MemorySource,
  PendingProposal,
  ProductIssue,
  ProductProject,
  PromptPackage,
  FederatedRetrievedDocument,
  RoutedEvent,
  EventWorkLink,
  RoutedContact,
  RoutedEntity,
  RoutedItem,
  RoutedObjectRecord,
  RoutedObjectSurface,
  RoutedTodo,
  SchedulerDashboard,
  SchedulerDefinition,
  SchedulerQueueItem,
  SchedulerRun,
  SchedulerWorkerAgentRun,
  SchedulerWorkerStatus,
  CalendarTriggerStatus,
  WorkflowTemplate,
  SkillRegistryItem,
  ToolConnection,
  ToolRegistryItem,
  WorkflowReport,
  WorkflowRunLogEntry,
} from "./types";
import {
  candidateResultClass,
  candidateResultLabel,
  createClientId,
  definitionQueueItems,
  formatDateOnly,
  formatDateTime,
  previewTime,
  routedObjectDomain,
  routedObjectTitle,
  safeJson,
  triggerSummary,
  unassignedDefinitionItemCount,
} from "./uiHelpers";

const calendarColors: Record<string, string> = {
  personal: "#22a06b",
  "maestro-development": "#2563eb",
  praxis: "#ef7d32",
  "perti-laboratories": "#7c3aed",
  usma: "#b42318",
  l3: "#536471",
  global: "#111827",
};

const staleWorkflowProgressLabels = new Set(["Not started.", "Not Started", "not_started"]);

function queueAgentLabel(item: MaestroQueueItem) {
  return item.agent_name || item.agent_key || item.work_item_ids.join(", ") || "agent";
}

function workflowProgressLabel({
  plan,
  run,
  busyToolCallId,
  maestroStatus,
}: {
  plan: MaestroPlan | null;
  run: MaestroRun | null;
  busyToolCallId: string | null;
  maestroStatus: string;
}) {
  if (busyToolCallId) return "Running approved tool";
  const activePlan = run?.plan ?? plan;
  const queueItems = activePlan?.scheduler.queue_items ?? [];
  const runStatus = run?.status;
  const planStatus = activePlan?.status;

  const itemWithStatus = (statuses: string[]) =>
    queueItems.find((item) => statuses.includes(item.status));

  const running = itemWithStatus(["running", "retrying"]);
  if (running) {
    return `${running.status === "retrying" ? "Retrying" : "Running"}: ${queueAgentLabel(running)}`;
  }
  const approval = itemWithStatus(["approval_required"]);
  if (approval) return `Waiting for approval: ${queueAgentLabel(approval)}`;
  const blocked = itemWithStatus(["blocked"]);
  if (blocked) {
    return `Waiting on ${queueAgentLabel(blocked)}${
      blocked.error_message ? `: ${blocked.error_message}` : ""
    }`;
  }
  const failed = itemWithStatus(["failed"]);
  if (failed) return `Failed: ${queueAgentLabel(failed)}`;
  const queued = itemWithStatus(["ready", "queued"]);
  if (queued) return `Queued: ${queueAgentLabel(queued)}`;
  const pending = itemWithStatus(["pending", "proposed"]);
  if (pending) {
    return planStatus === "running" || runStatus === "running"
      ? `Preparing: ${queueAgentLabel(pending)}`
      : "Proposed plan ready for review";
  }
  const scheduled = itemWithStatus(["scheduled"]);
  if (scheduled) return `Scheduled: ${queueAgentLabel(scheduled)}`;
  if (queueItems.length > 0 && queueItems.every((item) => item.status === "completed")) {
    return "Workflow complete";
  }
  if (queueItems.length > 0 && queueItems.every((item) => item.status === "archived")) {
    return "Workflow archived";
  }
  if (runStatus === "completed") return "Workflow complete";
  if (runStatus === "scheduled") return "Scheduled workflow saved";
  if (runStatus === "blocked") return "Workflow waiting on input";
  if (runStatus === "failed") return "Workflow failed";
  const currentStep = String(activePlan?.scheduler.current_step ?? "").trim();
  if (currentStep && !staleWorkflowProgressLabels.has(currentStep)) return currentStep;
  if (maestroStatus && maestroStatus !== "Idle") return maestroStatus;
  return "Ready";
}

function shouldShowPlanPreview(plan: MaestroPlan | null) {
  return Boolean(plan && plan.status === "proposed");
}

function shouldShowInlineRunPreview(_run: MaestroRun | null) {
  return false;
}

function queueItemAgentRun(item: SchedulerQueueItem): SchedulerWorkerAgentRun | null {
  const payload = item.output_payload ?? {};
  const agentRun = payload.agent_run;
  if (!agentRun || typeof agentRun !== "object") return null;
  return agentRun as SchedulerWorkerAgentRun;
}

function queueItemToolCalls(item: SchedulerQueueItem): NonNullable<AgentRun["tool_calls"]> {
  return queueItemAgentRun(item)?.tool_calls ?? [];
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const tokens = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g);
  return tokens
    .filter((token) => token.length > 0)
    .map((token, index) => {
      const key = `${keyPrefix}-${index}`;
      if (token.startsWith("`") && token.endsWith("`")) {
        return <code key={key}>{token.slice(1, -1)}</code>;
      }
      if (token.startsWith("**") && token.endsWith("**")) {
        return <strong key={key}>{token.slice(2, -2)}</strong>;
      }
      if (token.startsWith("*") && token.endsWith("*")) {
        return <em key={key}>{token.slice(1, -1)}</em>;
      }
      const linkMatch = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (linkMatch) {
        const [, label, href] = linkMatch;
        const safeHref = /^(https?:|mailto:)/i.test(href) ? href : "";
        if (safeHref) {
          return (
            <a key={key} href={safeHref} target="_blank" rel="noreferrer">
              {label}
            </a>
          );
        }
        return <span key={key}>{label}</span>;
      }
      return token;
    });
}

function normalizeMarkdownContent(content: string) {
  return content
    .replace(/([^\n])\s+(\d+\.\s+\*\*)/g, "$1\n\n$2")
    .split(/\r?\n/)
    .map((line) => {
      if (/^\s*\d+\.\s+/.test(line) && line.includes(" - ")) {
        return line.replace(/\s+-\s+/g, "\n- ");
      }
      return line;
    })
    .join("\n");
}

function MarkdownMessage({ content }: { content: string }) {
  const normalizedContent = normalizeMarkdownContent(content);
  const lines = normalizedContent.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let orderedItems: string[] = [];
  let codeLines: string[] = [];
  let inCodeBlock = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(
      <p key={`p-${blocks.length}`}>
        {paragraph.map((text, index) => (
          <span key={`p-${blocks.length}-${index}`}>
            {index > 0 && <br />}
            {renderInlineMarkdown(text, `p-${blocks.length}-${index}`)}
          </span>
        ))}
      </p>,
    );
    paragraph = [];
  };
  const flushList = () => {
    if (listItems.length) {
      blocks.push(
        <ul key={`ul-${blocks.length}`}>
          {listItems.map((item, index) => (
            <li key={`li-${index}`}>{renderInlineMarkdown(item, `ul-${blocks.length}-${index}`)}</li>
          ))}
        </ul>,
      );
      listItems = [];
    }
    if (orderedItems.length) {
      blocks.push(
        <ol key={`ol-${blocks.length}`}>
          {orderedItems.map((item, index) => (
            <li key={`oli-${index}`}>{renderInlineMarkdown(item, `ol-${blocks.length}-${index}`)}</li>
          ))}
        </ol>,
      );
      orderedItems = [];
    }
  };
  const flushCode = () => {
    if (!codeLines.length) return;
    blocks.push(
      <pre key={`pre-${blocks.length}`}>
        <code>{codeLines.join("\n")}</code>
      </pre>,
    );
    codeLines = [];
  };
  const headingBlock = (level: number, text: string) => {
    const children = renderInlineMarkdown(text, `h-${blocks.length}`);
    if (level <= 1) return <h3 key={`h-${blocks.length}`}>{children}</h3>;
    if (level === 2) return <h4 key={`h-${blocks.length}`}>{children}</h4>;
    if (level === 3) return <h5 key={`h-${blocks.length}`}>{children}</h5>;
    return <h6 key={`h-${blocks.length}`}>{children}</h6>;
  };

  lines.forEach((line) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      if (inCodeBlock) {
        flushCode();
        inCodeBlock = false;
      } else {
        flushParagraph();
        flushList();
        inCodeBlock = true;
      }
      return;
    }
    if (inCodeBlock) {
      codeLines.push(line);
      return;
    }
    if (!trimmed) {
      flushParagraph();
      flushList();
      return;
    }
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const level = Math.min(headingMatch[1].length, 4);
      blocks.push(headingBlock(level, headingMatch[2]));
      return;
    }
    const bulletMatch = trimmed.match(/^[-*]\s+(.+)$/);
    if (bulletMatch) {
      flushParagraph();
      if (orderedItems.length) flushList();
      listItems.push(bulletMatch[1]);
      return;
    }
    const orderedMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (orderedMatch) {
      flushParagraph();
      if (listItems.length) flushList();
      orderedItems.push(orderedMatch[1]);
      return;
    }
    paragraph.push(trimmed);
  });

  flushParagraph();
  flushList();
  flushCode();

  return <div className="markdown-message">{blocks.length > 0 ? blocks : <p>{normalizedContent}</p>}</div>;
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

const routedSurfaceConfig: Record<
  RoutedObjectSurface,
  {
    title: string;
    eyebrow: string;
    endpoint: string;
    responseKey: "events" | "contacts" | "todos" | "entities";
    icon: typeof CalendarDays;
    empty: string;
  }
> = {
  calendar: {
    title: "Calendar",
    eyebrow: "Routed events",
    endpoint: "/memory/routed-objects/events",
    responseKey: "events",
    icon: CalendarDays,
    empty: "No routed events yet.",
  },
  contacts: {
    title: "Contacts",
    eyebrow: "Routed CRM",
    endpoint: "/memory/routed-objects/contacts",
    responseKey: "contacts",
    icon: Users,
    empty: "No contacts yet.",
  },
  todos: {
    title: "To Do List",
    eyebrow: "Routed action items",
    endpoint: "/memory/routed-objects/todos",
    responseKey: "todos",
    icon: ListTodo,
    empty: "No routed to dos yet.",
  },
  organizations: {
    title: "Organizations",
    eyebrow: "Routed organizations",
    endpoint: "/memory/routed-objects/entities",
    responseKey: "entities",
    icon: Building2,
    empty: "No organizations yet.",
  },
};

function routedDraftFor(item: RoutedObjectRecord | null): Record<string, string> {
  if (!item) return {};
  if ("attendees" in item) {
    return {
      title: item.title ?? "",
      summary: item.summary ?? "",
      start_at: calendarInputValue(item.start_at),
      end_at: calendarInputValue(item.end_at),
      timezone: item.timezone ?? "America/New_York",
      all_day: item.all_day ? "true" : "false",
      recurrence_rule: item.recurrence_rule ?? "",
      item_kind: item.item_kind ?? "event",
      context_type: item.context_type ?? "routine",
      scheduling_effect: item.scheduling_effect ?? (item.blocks_time ? "hard" : "informational"),
      blocks_time: item.blocks_time ? "true" : "false",
      location: item.location ?? "",
      conferencing_url: item.conferencing_url ?? "",
      status: item.status ?? "scheduled",
    };
  }
  if ("todo_type" in item) {
    return {
      title: item.title ?? "",
      description: item.description ?? "",
      due_at: item.due_at ?? "",
      estimated_minutes: item.estimated_minutes?.toString() ?? "",
      scheduled_start_at: calendarInputValue(item.scheduled_start_at),
      agent_task: item.agent_task ? "true" : "false",
      agent_task_status: item.agent_task_status ?? "not_agent",
      priority: item.priority ?? "normal",
      status: item.status ?? "open",
      owner_type: item.owner_type ?? "user",
      owner_ref: item.owner_ref ?? "",
    };
  }
  if ("email" in item) {
    return {
      name: item.name ?? "",
      email: item.email ?? "",
      phone: item.phone ?? "",
      linkedin: item.linkedin ?? "",
      aliases: item.aliases.join(", "),
      summary: item.summary ?? "",
      origination: item.origination ?? "",
      status: item.status ?? "active",
    };
  }
  return {
    name: item.name ?? "",
    website: item.website ?? "",
    aliases: item.aliases.join(", "),
    summary: item.summary ?? "",
    status: item.status ?? "active",
  };
}

function calendarInputValue(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function calendarApiValue(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

type CalendarRecurrenceOptions = {
  freq: "daily" | "weekly" | "monthly" | "yearly";
  dtstart: string;
  until?: string;
  interval?: number;
  count?: number;
  wkst?: string;
  byweekday?: string[];
  bymonth?: number[];
  bymonthday?: number[];
  byhour?: number[];
  byminute?: number[];
  bysecond?: number[];
  bysetpos?: number[];
};

function recurrenceInteger(
  value: string | undefined,
  minimum: number,
  maximum: number,
): number | undefined {
  if (!value || !/^\d+$/.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : undefined;
}

function recurrenceIntegerList(
  value: string | undefined,
  minimum: number,
  maximum: number,
  excludeZero = false,
): number[] | undefined {
  if (!value) return undefined;
  const parsed = value.split(",").map(Number);
  if (
    parsed.length === 0
    || parsed.some((entry) => (
      !Number.isSafeInteger(entry)
      || entry < minimum
      || entry > maximum
      || (excludeZero && entry === 0)
    ))
  ) return undefined;
  return parsed;
}

function calendarRecurrence(item: RoutedEvent): CalendarRecurrenceOptions | undefined {
  if (!item.recurrence_rule || !item.start_at) return undefined;
  const fields = Object.fromEntries(
    item.recurrence_rule
      .split(";")
      .map((part) => part.split("=", 2))
      .filter((part) => part.length === 2)
      .map(([key, value]) => [key.trim().toUpperCase(), value.trim().toUpperCase()]),
  ) as Record<string, string>;
  const frequency = fields.FREQ?.toLowerCase();
  if (!(["daily", "weekly", "monthly", "yearly"] as string[]).includes(frequency)) return undefined;
  const start = new Date(item.start_at);
  if (Number.isNaN(start.getTime())) return undefined;

  let until: string | undefined;
  if (fields.UNTIL) {
    const match = fields.UNTIL.match(/^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})Z)?$/);
    if (!match) return undefined;
    const untilTimestamp = Date.UTC(
      Number(match[1]),
      Number(match[2]) - 1,
      Number(match[3]),
      Number(match[4] ?? 23),
      Number(match[5] ?? 59),
      Number(match[6] ?? 59),
    );
    const untilDate = new Date(untilTimestamp);
    if (
      Number.isNaN(untilTimestamp)
      || untilDate.getUTCFullYear() !== Number(match[1])
      || untilDate.getUTCMonth() !== Number(match[2]) - 1
      || untilDate.getUTCDate() !== Number(match[3])
      || untilTimestamp < start.getTime()
    ) return undefined;
    until = untilDate.toISOString();
  }

  const recurrence: CalendarRecurrenceOptions = {
    freq: frequency as CalendarRecurrenceOptions["freq"],
    dtstart: item.start_at,
  };
  if (until) recurrence.until = until;

  if (fields.INTERVAL) {
    const interval = recurrenceInteger(fields.INTERVAL, 1, 10_000);
    if (interval === undefined) return undefined;
    recurrence.interval = interval;
  }
  if (fields.COUNT) {
    const count = recurrenceInteger(fields.COUNT, 1, 100_000);
    if (count === undefined) return undefined;
    recurrence.count = count;
  }
  if (fields.WKST) {
    if (!/^(MO|TU|WE|TH|FR|SA|SU)$/.test(fields.WKST)) return undefined;
    recurrence.wkst = fields.WKST;
  }
  if (fields.BYDAY) {
    const weekdays = fields.BYDAY.split(",");
    if (weekdays.some((weekday) => !/^(MO|TU|WE|TH|FR|SA|SU)$/.test(weekday))) return undefined;
    recurrence.byweekday = weekdays;
  }

  const numericLists: Array<[
    keyof Pick<CalendarRecurrenceOptions, "bymonth" | "bymonthday" | "byhour" | "byminute" | "bysecond" | "bysetpos">,
    string,
    number,
    number,
    boolean,
  ]> = [
    ["bymonth", "BYMONTH", 1, 12, false],
    ["bymonthday", "BYMONTHDAY", -31, 31, true],
    ["byhour", "BYHOUR", 0, 23, false],
    ["byminute", "BYMINUTE", 0, 59, false],
    ["bysecond", "BYSECOND", 0, 60, false],
    ["bysetpos", "BYSETPOS", -366, 366, true],
  ];
  for (const [target, source, minimum, maximum, excludeZero] of numericLists) {
    if (!fields[source]) continue;
    const values = recurrenceIntegerList(fields[source], minimum, maximum, excludeZero);
    if (!values) return undefined;
    recurrence[target] = values;
  }
  return recurrence;
}

function calendarRecurrenceExdates(item: RoutedEvent): string[] {
  const values = item.metadata.recurrence_exdates;
  if (!Array.isArray(values)) return [];
  return values
    .filter((value): value is string => typeof value === "string")
    .filter((value) => !Number.isNaN(new Date(value).getTime()));
}

function ContactHydrationPanel({ surface }: { surface: "contacts" | "organizations" }) {
  const candidateType = surface === "contacts" ? "contact" : "organization";
  const [domainKey, setDomainKey] = useState("praxis");
  const [query, setQuery] = useState("in:sent newer_than:90d");
  const [maxMessages, setMaxMessages] = useState(200);
  const [maxContacts, setMaxContacts] = useState(100);
  const [enableEnrichment, setEnableEnrichment] = useState(true);
  const [enableCloudFallback, setEnableCloudFallback] = useState(false);
  const [jobs, setJobs] = useState<ContactHydrationJob[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<ContactHydrationCandidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Ready");
  const selectedJob = jobs.find((job) => job.id === selectedJobId) ?? jobs[0] ?? null;

  const refresh = useCallback(async () => {
    const response = await apiJson<{ jobs: ContactHydrationJob[] }>(
      `/memory/contact-hydration/jobs?limit=20`,
    );
    setJobs(response.jobs);
    const nextJobId = selectedJobId && response.jobs.some((job) => job.id === selectedJobId)
      ? selectedJobId
      : (response.jobs[0]?.id ?? null);
    setSelectedJobId(nextJobId);
    if (nextJobId) {
      const candidateResponse = await apiJson<{ candidates: ContactHydrationCandidate[] }>(
        `/memory/contact-hydration/jobs/${nextJobId}/candidates?candidate_type=${candidateType}&limit=500`,
      );
      setCandidates(candidateResponse.candidates);
    } else {
      setCandidates([]);
    }
  }, [candidateType, selectedJobId]);

  useEffect(() => {
    refresh().catch((error) => setMessage(error instanceof Error ? error.message : "Unable to load imports."));
  }, [refresh]);

  useEffect(() => {
    if (!selectedJob || !["pending", "scanning", "enriching", "promoting"].includes(selectedJob.status)) return;
    const timer = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refresh, selectedJob]);

  const startImport = async () => {
    setBusy(true);
    try {
      const response = await apiJson<{ job: ContactHydrationJob }>("/memory/contact-hydration/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          domain_key: domainKey,
          query,
          max_messages: maxMessages,
          max_contacts: maxContacts,
          enable_enrichment: enableEnrichment,
          enable_cloud_fallback: enableCloudFallback,
          max_cloud_calls: enableCloudFallback ? 10 : 0,
        }),
      });
      setSelectedJobId(response.job.id);
      setMessage("Historical Gmail import started in shadow mode.");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to start import.");
    } finally {
      setBusy(false);
    }
  };

  const actOnJob = async (action: string) => {
    if (!selectedJob) return;
    setBusy(true);
    try {
      await apiJson(`/memory/contact-hydration/jobs/${selectedJob.id}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      setMessage(`Import ${action} request applied.`);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : `Unable to ${action} import.`);
    } finally {
      setBusy(false);
    }
  };

  const approveHighConfidence = async () => {
    if (!selectedJob) return;
    setBusy(true);
    try {
      await apiJson(`/memory/contact-hydration/jobs/${selectedJob.id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ minimum_confidence: 0.8 }),
      });
      setMessage("High-confidence candidates approved for background promotion.");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Approval failed.");
    } finally {
      setBusy(false);
    }
  };

  const reviewCandidate = async (candidate: ContactHydrationCandidate, decision: "create" | "update" | "reject") => {
    setBusy(true);
    try {
      await apiJson(`/memory/contact-hydration/candidates/${candidate.id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          existing_object_id: decision === "update" ? candidate.existing_object_id : null,
        }),
      });
      setMessage(`${candidate.display_name} marked ${decision}.`);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Candidate review failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="hydration-panel">
      <summary>
        <span><HardDriveUpload size={17} /> Historical Gmail import</span>
        <small>{selectedJob ? `${selectedJob.status} / ${selectedJob.messages_scanned} messages` : "No import yet"}</small>
      </summary>
      <div className="hydration-controls">
        <label>Domain
          <select value={domainKey} onChange={(event) => setDomainKey(event.target.value)}>
            {Object.entries(domainLabels).filter(([key]) => key !== "global").map(([key, label]) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>
        </label>
        <label>Gmail query
          <input value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label>Maximum messages
          <input type="number" min="1" max="10000" value={maxMessages} onChange={(event) => setMaxMessages(Number(event.target.value))} />
        </label>
        <label>Maximum contacts
          <input type="number" min="1" max="5000" value={maxContacts} onChange={(event) => setMaxContacts(Number(event.target.value))} />
        </label>
        <label className="toggle-row"><input type="checkbox" checked={enableEnrichment} onChange={(event) => setEnableEnrichment(event.target.checked)} /> Enrich representative threads locally</label>
        <label className="toggle-row"><input type="checkbox" checked={enableCloudFallback} onChange={(event) => setEnableCloudFallback(event.target.checked)} /> Allow capped Terra fallback</label>
        <button className="planner-action" type="button" onClick={startImport} disabled={busy}>Start shadow import</button>
      </div>
      {jobs.length > 0 && (
        <label className="hydration-job-select">Import run
          <select value={selectedJob?.id ?? ""} onChange={(event) => setSelectedJobId(event.target.value)}>
            {jobs.map((job) => <option key={job.id} value={job.id}>{formatDateTime(job.created_at)} / {job.status} / {job.query}</option>)}
          </select>
        </label>
      )}
      {selectedJob && (
        <>
          <div className="preview-meta hydration-stats">
            <span>{selectedJob.messages_scanned} scanned</span>
            <span>{selectedJob.candidates_found} candidates</span>
            <span>{selectedJob.ambiguous_count} ambiguous</span>
            <span>{selectedJob.promoted_count} promoted</span>
          </div>
          <div className="hydration-actions">
            {!["paused", "complete", "cancelled", "failed"].includes(selectedJob.status) && <button type="button" onClick={() => actOnJob("pause")} disabled={busy}>Pause</button>}
            {selectedJob.status === "paused" && <button type="button" onClick={() => actOnJob("resume")} disabled={busy}>Resume</button>}
            {!["complete", "cancelled"].includes(selectedJob.status) && <button type="button" className="danger-action" onClick={() => actOnJob("cancel")} disabled={busy}>Cancel</button>}
            {selectedJob.status === "review" && <button type="button" className="planner-action" onClick={approveHighConfidence} disabled={busy}>Approve confidence 80%+</button>}
          </div>
          {selectedJob.error_message && <p className="error-message">{selectedJob.error_message}</p>}
          <div className="hydration-candidates">
            {candidates.filter((candidate) => candidate.status !== "excluded").map((candidate) => (
              <article key={candidate.id}>
                <div><strong>{candidate.display_name}</strong><small>{candidate.action} / {candidate.status} / {Math.round(candidate.confidence * 100)}%</small></div>
                <p>{String(candidate.proposed_data.summary ?? candidate.identity_key)}</p>
                {candidate.status === "review" && (
                  <div className="hydration-candidate-actions">
                    <button type="button" onClick={() => reviewCandidate(candidate, "create")} disabled={busy}>Create{candidate.existing_object_id ? " separate" : ""}</button>
                    {candidate.existing_object_id && <button type="button" onClick={() => reviewCandidate(candidate, "update")} disabled={busy}>Update matched</button>}
                    <button type="button" className="danger-action" onClick={() => reviewCandidate(candidate, "reject")} disabled={busy}>Reject</button>
                  </div>
                )}
              </article>
            ))}
            {candidates.length === 0 && <p className="empty-state">No {candidateType} candidates in this import yet.</p>}
          </div>
        </>
      )}
      <p className="memory-status">{message}</p>
    </details>
  );
}

function RoutedObjectsWorkspace({ surface }: { surface: RoutedObjectSurface }) {
  const config = routedSurfaceConfig[surface];
  const Icon = config.icon;
  const [items, setItems] = useState<RoutedObjectRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [domainFilter, setDomainFilter] = useState("all");
  const [showArchived, setShowArchived] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [showCalendarContext, setShowCalendarContext] = useState(true);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [creatingEvent, setCreatingEvent] = useState(false);
  const [creatingTodo, setCreatingTodo] = useState(false);
  const [todoDomainKey, setTodoDomainKey] = useState("personal");
  const [calendarDomainKey, setCalendarDomainKey] = useState("personal");
  const [draftAttendees, setDraftAttendees] = useState<RoutedEvent["attendees"]>([]);
  const [draftOrganizations, setDraftOrganizations] = useState<RoutedEvent["organizations"]>([]);
  const [contactOptions, setContactOptions] = useState<RoutedContact[]>([]);
  const [organizationOptions, setOrganizationOptions] = useState<RoutedEntity[]>([]);
  const [todoOptions, setTodoOptions] = useState<RoutedTodo[]>([]);
  const [issueOptions, setIssueOptions] = useState<ProductIssue[]>([]);
  const [workTargetType, setWorkTargetType] = useState<EventWorkLink["target_type"]>("todo");
  const [workTargetId, setWorkTargetId] = useState("");
  const [workRelationship, setWorkRelationship] = useState<EventWorkLink["relationship_type"]>("prerequisite");
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [busy, setBusy] = useState(false);
  const [contactQuery, setContactQuery] = useState("");
  const [contactSearchDraft, setContactSearchDraft] = useState("");
  const [mergeTargetId, setMergeTargetId] = useState("");
  const [selectedOccurrence, setSelectedOccurrence] = useState<{
    eventId: string;
    originalStartAt: string;
    startAt: string;
    endAt: string | null;
  } | null>(null);

  const supportsDomainFilter = surface === "calendar" || surface === "contacts" || surface === "todos" || surface === "organizations";
  const supportsLifecycleFilters = surface === "calendar" || surface === "todos";
  const supportsDoneFilter = surface === "todos";
  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
        if (!showArchived && item.status === "archived") return false;
        if (supportsDoneFilter && !showDone && item.status === "done") return false;
        if (
          surface === "calendar"
          && !showCalendarContext
          && "item_kind" in item
          && item.item_kind === "context_window"
        ) return false;
        return true;
      }),
    [items, showArchived, showCalendarContext, showDone, supportsDoneFilter, surface],
  );
  const selectedItem = creatingEvent || creatingTodo
    ? null
    : visibleItems.find((item) => item.id === selectedId) ?? null;
  const calendarItems = useMemo(
    () =>
      visibleItems
        .filter((item): item is RoutedEvent => "start_at" in item && Boolean(item.start_at))
        .map((item) => {
          const color = calendarColors[item.domain_key ?? "global"] ?? calendarColors.global;
          const isContextWindow = item.item_kind === "context_window";
          const isScheduledTodo = item.item_kind === "scheduled_todo";
          const calendarEvent: EventInput & {
            exdate?: string[];
            extendedProps: { resource: RoutedEvent };
          } = {
            id: item.id,
            title: item.title,
            start: item.start_at!,
            end: item.end_at ?? undefined,
            allDay: item.all_day,
            backgroundColor: item.status === "cancelled"
              ? "#f3f4f6"
              : isContextWindow ? `${color}24` : isScheduledTodo ? `${color}80` : color,
            borderColor: color,
            textColor: item.status === "cancelled" || isContextWindow || isScheduledTodo ? "#20252b" : "#ffffff",
            extendedProps: { resource: item },
          };
          const recurrence = calendarRecurrence(item);
          if (recurrence) {
            calendarEvent.rrule = recurrence;
            const exclusions = calendarRecurrenceExdates(item);
            if (exclusions.length > 0) calendarEvent.exdate = exclusions;
            if (item.end_at) {
              const duration = new Date(item.end_at).getTime() - new Date(item.start_at!).getTime();
              if (Number.isFinite(duration) && duration > 0) calendarEvent.duration = duration;
            }
          }
          return calendarEvent;
        }),
    [visibleItems],
  );
  const unscheduledCalendarItems = visibleItems.filter(
    (item): item is RoutedEvent => "start_at" in item && !item.start_at,
  );

  const refreshItems = useCallback(async () => {
    const params = new URLSearchParams({ limit: surface === "calendar" ? "500" : "100" });
    if (supportsDomainFilter && domainFilter !== "all") {
      params.set("domain_key", domainFilter);
    }
    if ((surface === "contacts" || surface === "organizations") && contactQuery.trim()) {
      params.set("query_text", contactQuery.trim());
    }
    const response = await apiJson<Record<string, RoutedObjectRecord[]>>(
      `${config.endpoint}?${params.toString()}`,
    );
    const nextItems = response[config.responseKey] ?? [];
    setItems(nextItems);
    setSelectedId((current) => current && nextItems.some((item) => item.id === current) ? current : null);
    setStatusMessage("Ready");
  }, [config.endpoint, config.responseKey, contactQuery, domainFilter, supportsDomainFilter, surface]);

  useEffect(() => {
    refreshItems().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : `Unable to load ${config.title}.`),
    );
  }, [refreshItems, config.title]);

  useEffect(() => {
    if (creatingEvent || creatingTodo) return;
    const nextDraft = routedDraftFor(selectedItem);
    if (
      selectedItem
      && "recurrence_rule" in selectedItem
      && selectedItem.recurrence_rule
      && selectedOccurrence?.eventId === selectedItem.id
    ) {
      nextDraft.start_at = calendarInputValue(selectedOccurrence.startAt);
      nextDraft.end_at = selectedOccurrence.endAt
        ? calendarInputValue(selectedOccurrence.endAt)
        : "";
    }
    setDraft(nextDraft);
    if (selectedItem && "attendees" in selectedItem) {
      setDraftAttendees(selectedItem.attendees);
      setDraftOrganizations(selectedItem.organizations);
      setCalendarDomainKey(selectedItem.domain_key ?? "personal");
    }
  }, [creatingEvent, creatingTodo, selectedItem, selectedOccurrence]);

  useEffect(() => {
    if (surface !== "calendar") return;
    Promise.all([
      apiJson<{ contacts: RoutedContact[] }>("/memory/routed-objects/contacts?limit=500"),
      apiJson<{ entities: RoutedEntity[] }>("/memory/routed-objects/entities?limit=500"),
      apiJson<{ todos: RoutedTodo[] }>("/memory/routed-objects/todos?limit=500"),
      apiJson<{ issues: ProductIssue[] }>("/issues?status=open&limit=250"),
    ])
      .then(([contacts, organizations, todos, issues]) => {
        setContactOptions(contacts.contacts);
        setOrganizationOptions(organizations.entities);
        setTodoOptions(todos.todos.filter((todo) => todo.status !== "archived"));
        setIssueOptions(issues.issues);
      })
      .catch(() => undefined);
  }, [surface]);

  const updateDraft = (key: string, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const saveSelected = async () => {
    if (!selectedItem && !(surface === "calendar" && creatingEvent) && !(surface === "todos" && creatingTodo)) return;
    setBusy(true);
    try {
      const calendarUpdates = surface === "calendar"
        ? {
            ...draft,
            start_at: calendarApiValue(draft.start_at ?? ""),
            end_at: calendarApiValue(draft.end_at ?? ""),
            all_day: draft.all_day === "true",
            blocks_time: draft.item_kind === "context_window" ? false : draft.blocks_time !== "false",
            attendees: draft.item_kind === "context_window" ? [] : draftAttendees,
            organizations: draft.item_kind === "context_window" ? [] : draftOrganizations,
            ...(selectedItem
              && "recurrence_rule" in selectedItem
              && selectedItem.recurrence_rule
              && selectedOccurrence?.eventId === selectedItem.id
              ? {
                  edit_scope: "instance",
                  occurrence_start_at: selectedOccurrence.originalStartAt,
                }
              : {}),
          }
        : null;
      const todoUpdates = surface === "todos"
        ? {
            ...draft,
            due_at: calendarApiValue(draft.due_at ?? ""),
            scheduled_start_at: calendarApiValue(draft.scheduled_start_at ?? ""),
            estimated_minutes: draft.estimated_minutes ? Number(draft.estimated_minutes) : null,
            agent_task: draft.agent_task === "true",
          }
        : null;
      const response = await apiJson<{ event?: RoutedEvent }>(
        selectedItem ? `${config.endpoint}/${selectedItem.id}` : config.endpoint,
        {
        method: selectedItem ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...(selectedItem
            ? {
                updates: calendarUpdates ?? todoUpdates ?? Object.fromEntries(
                  Object.entries(draft).map(([key, value]) => [
                    key,
                    key === "aliases"
                      ? value.split(",").map((alias) => alias.trim()).filter(Boolean)
                      : value.trim() || null,
                  ]),
                ),
              }
            : surface === "calendar"
              ? { ...calendarUpdates, domain_key: calendarDomainKey }
              : { ...todoUpdates, domain_key: todoDomainKey }),
        }),
      });
      if (response.event) setSelectedId(response.event.id);
      setSelectedOccurrence(null);
      setCreatingEvent(false);
      setCreatingTodo(false);
      setStatusMessage(`${config.title} item saved.`);
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const updateCalendarTiming = async (info: EventDropArg | EventResizeDoneArg) => {
    const nextStart = info.event.start;
    const defaultDuration = info.event.allDay ? 24 * 60 * 60 * 1000 : 60 * 60 * 1000;
    const nextEnd = info.event.end ?? (nextStart ? new Date(nextStart.getTime() + defaultDuration) : null);
    const eventId = info.event.id;
    const resource = info.event.extendedProps.resource as RoutedEvent | undefined;
    const originalStart = info.oldEvent.start ?? nextStart;
    const editingOccurrence = Boolean(resource?.recurrence_rule && originalStart);
    if (!nextStart || !nextEnd) return;
    if (Number.isNaN(nextStart.getTime()) || Number.isNaN(nextEnd.getTime())) {
      setStatusMessage("Calendar update failed: invalid date or time.");
      return;
    }

    setSelectedId(eventId);
    setBusy(true);
    setItems((current) =>
      current.map((item) =>
        item.id === eventId && "start_at" in item
          ? {
              ...item,
              start_at: nextStart.toISOString(),
              end_at: nextEnd.toISOString(),
            }
          : item,
      ),
    );
    try {
      const response = await apiJson<{ event?: RoutedEvent }>(`${config.endpoint}/${eventId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          updates: {
            start_at: nextStart.toISOString(),
            end_at: nextEnd.toISOString(),
            all_day: info.event.allDay,
            ...(editingOccurrence
              ? {
                  edit_scope: "instance",
                  occurrence_start_at: originalStart!.toISOString(),
                }
              : {}),
          },
        }),
      });
      if (response.event) setSelectedId(response.event.id);
      setSelectedOccurrence(null);
      setStatusMessage(editingOccurrence ? "This occurrence was updated." : "Event time updated.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Calendar update failed.");
      await refreshItems().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  const beginNewEvent = (
    start = new Date(),
    end = new Date(start.getTime() + 60 * 60 * 1000),
    itemKind: RoutedEvent["item_kind"] = "event",
  ) => {
    setCreatingEvent(true);
    setSelectedId(null);
    setDraft({
      title: "",
      summary: "",
      start_at: calendarInputValue(start.toISOString()),
      end_at: calendarInputValue(end.toISOString()),
      timezone: "America/New_York",
      all_day: "false",
      recurrence_rule: "",
      item_kind: itemKind,
      context_type: "routine",
      scheduling_effect: itemKind === "context_window" ? "informational" : "hard",
      blocks_time: itemKind === "context_window" ? "false" : "true",
      location: "",
      conferencing_url: "",
      status: "scheduled",
    });
    setDraftAttendees([]);
    setDraftOrganizations([]);
    setCalendarDomainKey(domainFilter === "all" ? "personal" : domainFilter);
  };

  const beginNewTodo = () => {
    setCreatingTodo(true);
    setCreatingEvent(false);
    setSelectedId(null);
    setTodoDomainKey(domainFilter === "all" ? "personal" : domainFilter);
    setDraft({
      title: "",
      description: "",
      due_at: "",
      estimated_minutes: "",
      scheduled_start_at: "",
      agent_task: "false",
      priority: "normal",
      status: "open",
    });
  };

  const selectCalendarRange = (selection: DateSelectArg) => {
    beginNewEvent(selection.start, selection.end);
    setDraft((current) => ({ ...current, all_day: selection.allDay ? "true" : "false" }));
  };

  const addCalendarAttendee = (contactId: string) => {
    const contact = contactOptions.find((item) => item.id === contactId);
    if (!contact || draftAttendees.some((item) => item.contact_id === contact.id)) return;
    setDraftAttendees((current) => [
      ...current,
      {
        id: null,
        contact_id: contact.id,
        name: contact.name,
        email: contact.email,
        attendee_type: "required",
        response_status: "needs_action",
        is_organizer: false,
        is_user: false,
      },
    ]);
  };

  const addCalendarOrganization = (organizationId: string) => {
    const organization = organizationOptions.find((item) => item.id === organizationId);
    if (!organization || draftOrganizations.some((item) => item.id === organization.id)) return;
    setDraftOrganizations((current) => [
      ...current,
      { id: organization.id, name: organization.name, role: "related" },
    ]);
  };

  const addEventWorkLink = async (eventItem: RoutedEvent) => {
    if (!workTargetId) return;
    setBusy(true);
    try {
      await apiJson(`/memory/routed-objects/events/${eventItem.id}/work-links`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_type: workTargetType,
          target_id: workTargetId,
          relationship_type: workRelationship,
        }),
      });
      setWorkTargetId("");
      setStatusMessage("Linked work added to the event.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Unable to link work.");
    } finally {
      setBusy(false);
    }
  };

  const removeEventWorkLink = async (eventItem: RoutedEvent, linkId: string) => {
    setBusy(true);
    try {
      await apiJson(`/memory/routed-objects/events/${eventItem.id}/work-links/${linkId}`, {
        method: "DELETE",
      });
      setStatusMessage("Linked work removed.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Unable to remove linked work.");
    } finally {
      setBusy(false);
    }
  };

  const eventWorkEditor = (eventItem: RoutedEvent) => {
    const options = workTargetType === "todo" ? todoOptions : issueOptions;
    const linkedIds = new Set(eventItem.work_links.map((link) => link.target_id));
    return <section className="calendar-link-editor event-work-editor">
      <h4>Linked work</h4>
      <div className="event-work-controls">
        <select value={workRelationship} onChange={(event) => setWorkRelationship(event.target.value as EventWorkLink["relationship_type"])} aria-label="Work relationship">
          <option value="prerequisite">Complete before event</option>
          <option value="during">Work during event</option>
          <option value="follow_up">Follow up after event</option>
        </select>
        <select value={workTargetType} onChange={(event) => { setWorkTargetType(event.target.value as EventWorkLink["target_type"]); setWorkTargetId(""); }} aria-label="Work type">
          <option value="todo">To do</option>
          <option value="product_issue">Product issue</option>
        </select>
        <select value={workTargetId} onChange={(event) => setWorkTargetId(event.target.value)} aria-label="Work item">
          <option value="">Select work</option>
          {options.filter((option) => !linkedIds.has(option.id)).map((option) => (
            <option key={option.id} value={option.id}>{option.title}</option>
          ))}
        </select>
        <button type="button" onClick={() => addEventWorkLink(eventItem)} disabled={!workTargetId || busy} title="Link work to event"><Plus size={15} /> Add</button>
      </div>
      <div className="event-work-list">
        {eventItem.work_links.map((link) => (
          <div className={`event-work-row ${link.relationship_type}`} key={link.id}>
            <span><strong>{link.title}</strong><small>{link.target_type === "todo" ? "To do" : "Product issue"} / {link.relationship_type.replace("_", " ")} / {link.status}</small></span>
            <button className="icon-button" type="button" onClick={() => removeEventWorkLink(eventItem, link.id)} disabled={busy} title={`Unlink ${link.title}`}><X size={14} /></button>
          </div>
        ))}
        {eventItem.work_links.length === 0 && <p className="empty-state compact">No work linked to this event.</p>}
      </div>
    </section>;
  };

  const eventReferences = (links: RoutedTodo["event_links"]) => links.length > 0 && (
    <section className="contact-intelligence-section event-reference-list">
      <h4>Calendar links</h4>
      {links.map((link) => (
        <div className="contact-evidence-row" key={link.id}>
          <strong>{link.event_title}</strong>
          <span>{link.relationship_type.replace("_", " ")} / {formatDateTime(link.start_at)}</span>
        </div>
      ))}
    </section>
  );

  const archiveSelected = async () => {
    if (!selectedItem) return;
    const objectType =
      surface === "calendar"
        ? "event"
        : surface === "todos"
          ? "todo"
          : surface === "contacts"
            ? "contact"
            : "entity";
    setBusy(true);
    try {
      await apiJson(`/memory/routed-objects/${objectType}/${selectedItem.id}/archive`, {
        method: "PATCH",
      });
      setStatusMessage("Item archived.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Archive failed.");
    } finally {
      setBusy(false);
    }
  };

  const markSelectedDone = async () => {
    if (!selectedItem || surface !== "todos") return;
    setBusy(true);
    try {
      await apiJson(`${config.endpoint}/${selectedItem.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: { status: "done" } }),
      });
      setStatusMessage("Item marked done.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Done update failed.");
    } finally {
      setBusy(false);
    }
  };

  const setSelectedEventStatus = async (status: "scheduled" | "cancelled") => {
    if (!selectedItem || surface !== "calendar") return;
    setBusy(true);
    try {
      await apiJson(`${config.endpoint}/${selectedItem.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: { status } }),
      });
      setStatusMessage(status === "cancelled" ? "Event cancelled. The time is now free." : "Event restored.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Event status update failed.");
    } finally {
      setBusy(false);
    }
  };

  const mergeSelectedContact = async () => {
    if (surface !== "contacts" || !selectedItem || !mergeTargetId) return;
    setBusy(true);
    try {
      await apiJson(`${config.endpoint}/${selectedItem.id}/merge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duplicate_contact_id: mergeTargetId }),
      });
      setMergeTargetId("");
      setStatusMessage("Contacts merged. The selected contact was kept as canonical.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Contact merge failed.");
    } finally {
      setBusy(false);
    }
  };

  const mergeSelectedOrganization = async () => {
    if (surface !== "organizations" || !selectedItem || !mergeTargetId) return;
    setBusy(true);
    try {
      await apiJson(`${config.endpoint}/${selectedItem.id}/merge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ duplicate_organization_id: mergeTargetId }),
      });
      setMergeTargetId("");
      setStatusMessage("Organizations merged. The selected organization was kept as canonical.");
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Organization merge failed.");
    } finally {
      setBusy(false);
    }
  };

  const mobileInlineDetail = (item: RoutedObjectRecord) => (
    <div className="mobile-routed-detail" aria-label={`${routedObjectTitle(item)} details`}>
      <div className="mobile-routed-detail-heading">
        <strong>{routedObjectTitle(item)}</strong>
        <button className="icon-button" type="button" title="Minimize details" onClick={() => setSelectedId(null)}>
          <X size={16} />
        </button>
      </div>
      {"todo_type" in item && <>
        <label>Title<input value={draft.title ?? ""} onChange={(event) => updateDraft("title", event.target.value)} /></label>
        <label>Description<textarea value={draft.description ?? ""} onChange={(event) => updateDraft("description", event.target.value)} /></label>
        <div className="two-column-fields">
          <label>Estimate (minutes)<input type="number" min="5" max="480" value={draft.estimated_minutes ?? ""} onChange={(event) => updateDraft("estimated_minutes", event.target.value)} /></label>
          <label>Scheduled<input type="datetime-local" value={draft.scheduled_start_at ?? ""} onChange={(event) => updateDraft("scheduled_start_at", event.target.value)} /></label>
        </div>
        <label className="toggle-row"><input type="checkbox" checked={draft.agent_task === "true"} onChange={(event) => updateDraft("agent_task", event.target.checked ? "true" : "false")} />Agent task</label>
        {eventReferences(item.event_links)}
      </>}
      {"email" in item && <>
        <label>Name<input value={draft.name ?? ""} onChange={(event) => updateDraft("name", event.target.value)} /></label>
        <label>Email<input value={draft.email ?? ""} onChange={(event) => updateDraft("email", event.target.value)} /></label>
        <label>Phone<input value={draft.phone ?? ""} onChange={(event) => updateDraft("phone", event.target.value)} /></label>
        <label>Summary<textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} /></label>
      </>}
      {"website" in item && !("email" in item) && <>
        <label>Name<input value={draft.name ?? ""} onChange={(event) => updateDraft("name", event.target.value)} /></label>
        <label>Website<input value={draft.website ?? ""} onChange={(event) => updateDraft("website", event.target.value)} /></label>
        <label>Summary<textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} /></label>
      </>}
      {"attendees" in item && <>
        <label>Title<input value={draft.title ?? ""} onChange={(event) => updateDraft("title", event.target.value)} /></label>
        <div className="two-column-fields">
          <label>Starts<input type="datetime-local" value={draft.start_at ?? ""} onChange={(event) => updateDraft("start_at", event.target.value)} /></label>
          <label>Ends<input type="datetime-local" value={draft.end_at ?? ""} onChange={(event) => updateDraft("end_at", event.target.value)} /></label>
        </div>
        <label>Summary<textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} /></label>
        {eventWorkEditor(item)}
      </>}
      <div className="routed-detail-actions">
        <button className="planner-action" type="button" onClick={saveSelected} disabled={busy}>Save</button>
        {surface === "todos" && item.status !== "done" && <button type="button" onClick={markSelectedDone} disabled={busy}><CheckCircle2 size={16} /> Done</button>}
      </div>
    </div>
  );

  return (
    <div className={surface === "calendar" ? "routed-object-workspace calendar-workspace" : "routed-object-workspace"}>
      {(surface === "contacts" || surface === "organizations") && <ContactHydrationPanel surface={surface} />}
      <section className="memory-panel routed-object-list-panel" aria-labelledby={`${surface}-heading`}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">{config.eyebrow}</p>
            <h3 id={`${surface}-heading`}>{config.title}</h3>
          </div>
          <div className="section-heading-actions">
            {surface === "calendar" && (
              <>
                <button type="button" onClick={() => beginNewEvent(undefined, undefined, "context_window")}>
                  <Clock3 size={17} /> New context
                </button>
                <button className="planner-action" type="button" onClick={() => beginNewEvent()}>
                  <Plus size={17} /> New event
                </button>
              </>
            )}
            {surface === "todos" && (
              <button className="planner-action" type="button" onClick={beginNewTodo}>
                <Plus size={17} /> New task
              </button>
            )}
            <button className="icon-button" onClick={refreshItems} title={`Refresh ${config.title}`}>
              <RefreshCw size={18} />
            </button>
          </div>
        </div>

        {supportsDomainFilter && (
          <label className="routed-filter">
            <span>Domain</span>
            <select value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)}>
              <option value="all">All domains</option>
              {Object.entries(domainLabels)
                .filter(([key]) => key !== "global")
                .map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
        )}

        {surface === "calendar" && domainFilter === "all" && (
          <div className="calendar-legend" aria-label="Calendar domain colors">
            {Object.entries(domainLabels).filter(([key]) => key !== "global").map(([key, label]) => (
              <span key={key}><i style={{ backgroundColor: calendarColors[key] ?? calendarColors.global }} />{label}</span>
            ))}
            <span className="calendar-context-legend"><i />Context window</span>
          </div>
        )}

        {(surface === "contacts" || surface === "organizations") && (
          <form
            className="contact-search"
            onSubmit={(event) => {
              event.preventDefault();
              setContactQuery(contactSearchDraft.trim());
            }}
          >
            <Search size={17} />
            <input
              value={contactSearchDraft}
              onChange={(event) => setContactSearchDraft(event.target.value)}
              placeholder={surface === "contacts" ? "Name, organization, relationship, or prior discussion" : "Name, alias, website, person, or prior interaction"}
            />
            <button className="planner-action" type="submit">Search</button>
          </form>
        )}

        {supportsLifecycleFilters && (
          <div className="routed-filter-row">
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={showArchived}
                onChange={(event) => setShowArchived(event.target.checked)}
              />
              Show archived
            </label>
            {surface === "calendar" && (
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={showCalendarContext}
                  onChange={(event) => setShowCalendarContext(event.target.checked)}
                />
                Show context
              </label>
            )}
            {supportsDoneFilter && (
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={showDone}
                  onChange={(event) => setShowDone(event.target.checked)}
                />
                Show done
              </label>
            )}
          </div>
        )}

        {surface === "calendar" && (
          <div className="calendar-shell">
            <CalendarErrorBoundary
              resetKey={calendarItems.map((item) => `${item.id}:${String(item.start)}:${String(item.rrule ?? "")}`).join("|")}
              fallback={(
                <div className="calendar-grid-fallback" role="alert">
                  <strong>Calendar grid unavailable</strong>
                  <span>The event list is still available. Refresh after correcting the affected event.</span>
                  {visibleItems
                    .filter((item): item is RoutedEvent => "start_at" in item)
                    .map((item) => (
                      <button key={item.id} type="button" onClick={() => {
                        setSelectedOccurrence(null);
                        setSelectedId(item.id);
                      }}>
                        <span>{item.title}</span>
                        <small>{item.start_at ? new Date(item.start_at).toLocaleString() : "Unscheduled"}</small>
                      </button>
                    ))}
                </div>
              )}
            >
              <FullCalendar
                plugins={[dayGridPlugin, timeGridPlugin, listPlugin, interactionPlugin, rrulePlugin]}
                events={calendarItems}
                initialView={window.innerWidth < 720 ? "timeGridDay" : "timeGridWeek"}
                headerToolbar={{
                  left: "prev,next today",
                  center: "title",
                  right: "dayGridMonth,timeGridWeek,timeGridDay,listWeek",
                }}
                buttonText={{ today: "Today", month: "Month", week: "Week", day: "Day", list: "Agenda" }}
                editable={!busy}
                selectable={!busy}
                selectMirror
                nowIndicator
                dayMaxEvents
                allDaySlot
                slotMinTime="06:00:00"
                slotMaxTime="22:00:00"
                scrollTime="08:00:00"
                eventTimeFormat={{ hour: "numeric", minute: "2-digit", meridiem: "short" }}
                slotLabelFormat={{ hour: "numeric", minute: "2-digit", meridiem: "short" }}
                height="auto"
                select={selectCalendarRange}
                eventClick={(info) => {
                  setCreatingEvent(false);
                  setSelectedId(info.event.id);
                  const resource = info.event.extendedProps.resource as RoutedEvent | undefined;
                  setSelectedOccurrence(
                    resource?.recurrence_rule && info.event.start
                      ? {
                          eventId: info.event.id,
                          originalStartAt: info.event.start.toISOString(),
                          startAt: info.event.start.toISOString(),
                          endAt: info.event.end?.toISOString() ?? null,
                        }
                      : null,
                  );
                }}
                eventDrop={updateCalendarTiming}
                eventResize={updateCalendarTiming}
                eventClassNames={(info) => {
                  const item = info.event.extendedProps.resource as RoutedEvent | undefined;
                  return [
                    item?.status === "cancelled" ? "calendar-event-cancelled" : "",
                    item?.status === "archived" ? "calendar-event-archived" : "",
                    item?.item_kind === "context_window" ? "calendar-event-context" : "",
                    item?.item_kind === "scheduled_todo" ? "calendar-event-task" : "",
                    item?.item_kind === "scheduled_todo" && item.todo_status === "done" ? "calendar-event-task-done" : "",
                    item?.conflicts?.length ? "calendar-event-conflict" : "",
                  ].filter(Boolean);
                }}
                eventContent={(info) => (
                  <div className="calendar-event-content">
                    <strong className="calendar-event-title">{info.event.title}</strong>
                    {info.timeText && <span className="calendar-event-time">{info.timeText}</span>}
                  </div>
                )}
              />
            </CalendarErrorBoundary>
            {selectedItem && "attendees" in selectedItem && mobileInlineDetail(selectedItem)}
          </div>
        )}

        {surface === "calendar" && unscheduledCalendarItems.length > 0 && (
          <div className="unscheduled-list">
            <span>Unscheduled</span>
            {unscheduledCalendarItems.map((item) => (
              <button
                className={item.id === selectedItem?.id ? "routed-object-row active" : "routed-object-row"}
                key={item.id}
                onClick={() => {
                  setCreatingEvent(false);
                  setSelectedId(item.id);
                }}
                type="button"
              >
                <CalendarDays size={18} />
                <span>
                  <strong>{item.title}</strong>
                  <small>{item.status}</small>
                </span>
              </button>
            ))}
          </div>
        )}

        {surface !== "calendar" && <div className="routed-object-list">
          {visibleItems.map((item) => (
            <div className="routed-object-mobile-group" key={item.id}>
            <button
              className={item.id === selectedItem?.id ? "routed-object-row active" : "routed-object-row"}
              onClick={() => {
                setCreatingTodo(false);
                setSelectedId((current) => current === item.id ? null : item.id);
              }}
              type="button"
            >
              {item.status === "done" ? <CheckCircle2 size={18} /> : <Icon size={18} />}
              <span>
                <strong>{routedObjectTitle(item)}</strong>
                <small>
                  {surface === "todos" && "due_at" in item
                      ? `${domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"} / ${item.status} / ${item.priority}`
                      : (surface === "contacts" || surface === "organizations") && "match_reasons" in item && item.match_reasons?.length
                        ? item.match_reasons.join(" / ")
                        : item.status}
                </small>
              </span>
            </button>
            {item.id === selectedItem?.id && mobileInlineDetail(item)}
            </div>
          ))}
          {visibleItems.length === 0 && <p className="empty-state">{config.empty}</p>}
        </div>}
      </section>

      <section className="memory-panel routed-object-detail-panel" aria-labelledby={`${surface}-detail`}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">Details</p>
            <h3 id={`${surface}-detail`}>
              {creatingEvent
                ? draft.item_kind === "context_window" ? "New context window" : "New event"
                : creatingTodo ? "New task"
                : selectedItem ? routedObjectTitle(selectedItem) : config.title}
            </h3>
          </div>
          <Icon size={18} />
        </div>

        {selectedItem || (surface === "calendar" && creatingEvent) || (surface === "todos" && creatingTodo) ? (
          <div className="routed-object-detail">
            {surface === "calendar" && (creatingEvent || (selectedItem && "attendees" in selectedItem)) && (
              <>
                {selectedOccurrence && selectedItem && "recurrence_rule" in selectedItem && (
                  <div className="calendar-instance-notice">
                    <span>
                      Editing only the {new Date(selectedOccurrence.originalStartAt).toLocaleString()} occurrence.
                    </span>
                    <button type="button" onClick={() => setSelectedOccurrence(null)}>
                      Edit full series
                    </button>
                  </div>
                )}
                <label>
                  Calendar item
                  <select
                    value={draft.item_kind ?? "event"}
                    onChange={(event) => {
                      const itemKind = event.target.value as RoutedEvent["item_kind"];
                      setDraft((current) => ({
                        ...current,
                        item_kind: itemKind,
                        context_type: itemKind === "context_window" ? current.context_type || "routine" : "",
                        scheduling_effect: itemKind === "context_window" ? "informational" : "hard",
                        blocks_time: itemKind === "context_window" ? "false" : "true",
                      }));
                      if (itemKind === "context_window") {
                        setDraftAttendees([]);
                        setDraftOrganizations([]);
                      }
                    }}
                  >
                    <option value="event">Event</option>
                    <option value="context_window">Context window</option>
                    <option value="scheduled_todo">Scheduled to do</option>
                  </select>
                </label>
                <label>
                  Calendar
                  <select value={calendarDomainKey} onChange={(event) => setCalendarDomainKey(event.target.value)} disabled={!creatingEvent}>
                    {Object.entries(domainLabels).filter(([key]) => key !== "global").map(([key, label]) => (
                      <option key={key} value={key}>{label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Title
                  <input value={draft.title ?? ""} onChange={(event) => updateDraft("title", event.target.value)} />
                </label>
                <label>
                  Summary
                  <textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} />
                </label>
                <div className="two-column-fields">
                  <label>
                    Start
                    <input type="datetime-local" value={draft.start_at ?? ""} onChange={(event) => updateDraft("start_at", event.target.value)} />
                  </label>
                  <label>
                    End
                    <input type="datetime-local" value={draft.end_at ?? ""} onChange={(event) => updateDraft("end_at", event.target.value)} />
                  </label>
                </div>
                {draft.item_kind === "context_window" && (
                  <div className="two-column-fields calendar-context-fields">
                    <label>
                      Context type
                      <select value={draft.context_type ?? "routine"} onChange={(event) => updateDraft("context_type", event.target.value)}>
                        <option value="household">Household</option>
                        <option value="childcare">Childcare</option>
                        <option value="routine">Routine</option>
                        <option value="availability">Availability</option>
                        <option value="energy">Energy</option>
                        <option value="location">Location</option>
                      </select>
                    </label>
                    <label>
                      Scheduling effect
                      <select value={draft.scheduling_effect ?? "informational"} onChange={(event) => updateDraft("scheduling_effect", event.target.value)}>
                        <option value="informational">Informational</option>
                        <option value="prefer">Prefer this window</option>
                        <option value="prefer_avoid">Prefer to avoid</option>
                        <option value="strongly_avoid">Strongly avoid</option>
                      </select>
                    </label>
                  </div>
                )}
                <div className="two-column-fields">
                  <label>
                    Timezone
                    <select value={draft.timezone ?? "America/New_York"} onChange={(event) => updateDraft("timezone", event.target.value)}>
                      <option value="America/New_York">Eastern</option>
                      <option value="America/Chicago">Central</option>
                      <option value="America/Denver">Mountain</option>
                      <option value="America/Los_Angeles">Pacific</option>
                      <option value="UTC">UTC</option>
                    </select>
                  </label>
                  <label>
                    Repeat
                    <select value={draft.recurrence_rule ?? ""} onChange={(event) => updateDraft("recurrence_rule", event.target.value)}>
                      <option value="">Does not repeat</option>
                      <option value="FREQ=DAILY">Daily</option>
                      <option value="FREQ=WEEKLY">Weekly</option>
                      <option value="FREQ=MONTHLY">Monthly</option>
                    </select>
                  </label>
                </div>
                <label className="toggle-row calendar-all-day">
                  <input type="checkbox" checked={draft.all_day === "true"} onChange={(event) => updateDraft("all_day", event.target.checked ? "true" : "false")} />
                  All day
                </label>
                {draft.item_kind !== "context_window" && <label>
                  Location
                  <input value={draft.location ?? ""} onChange={(event) => updateDraft("location", event.target.value)} />
                </label>}
                {draft.item_kind !== "context_window" && <label>
                  Meeting link
                  <input value={draft.conferencing_url ?? ""} onChange={(event) => updateDraft("conferencing_url", event.target.value)} placeholder="Google Meet, Teams, or Zoom URL" />
                </label>}
                {draft.item_kind !== "context_window" && draft.conferencing_url && /^https?:\/\//i.test(draft.conferencing_url) && (
                  <a className="calendar-meeting-link" href={draft.conferencing_url} target="_blank" rel="noreferrer">
                    <ExternalLink size={15} /> Open meeting
                  </a>
                )}
                <label>
                  Status
                  <select value={draft.status ?? "scheduled"} onChange={(event) => updateDraft("status", event.target.value)}>
                    <option value="scheduled">Scheduled</option>
                    <option value="tentative">Tentative</option>
                    <option value="cancelled">Cancelled</option>
                  </select>
                </label>
                {draft.item_kind !== "context_window" && <section className="calendar-link-editor">
                  <h4>Attendees</h4>
                  <select defaultValue="" onChange={(event) => { addCalendarAttendee(event.target.value); event.target.value = ""; }}>
                    <option value="">Add a contact</option>
                    {contactOptions.filter((contact) => !draftAttendees.some((attendee) => attendee.contact_id === contact.id)).map((contact) => (
                      <option key={contact.id} value={contact.id}>{contact.name}{contact.email ? ` / ${contact.email}` : ""}</option>
                    ))}
                  </select>
                  <div className="calendar-chip-list">
                    {draftAttendees.map((attendee, index) => (
                      <span className="calendar-chip" key={attendee.contact_id ?? `${attendee.name}-${index}`}>
                        <Users size={14} /> {attendee.name}
                        <button type="button" onClick={() => setDraftAttendees((current) => current.filter((_, itemIndex) => itemIndex !== index))} title={`Remove ${attendee.name}`}><X size={13} /></button>
                      </span>
                    ))}
                  </div>
                </section>}
                {selectedItem && "attendees" in selectedItem && draft.item_kind !== "context_window" && eventWorkEditor(selectedItem)}
                {draft.item_kind !== "context_window" && <section className="calendar-link-editor">
                  <h4>Organizations</h4>
                  <select defaultValue="" onChange={(event) => { addCalendarOrganization(event.target.value); event.target.value = ""; }}>
                    <option value="">Link an organization</option>
                    {organizationOptions.filter((organization) => !draftOrganizations.some((item) => item.id === organization.id)).map((organization) => (
                      <option key={organization.id} value={organization.id}>{organization.name}</option>
                    ))}
                  </select>
                  <div className="calendar-chip-list">
                    {draftOrganizations.map((organization) => (
                      <span className="calendar-chip organization" key={organization.id}>
                        <Building2 size={14} /> {organization.name}
                        <button type="button" onClick={() => setDraftOrganizations((current) => current.filter((item) => item.id !== organization.id))} title={`Remove ${organization.name}`}><X size={13} /></button>
                      </span>
                    ))}
                  </div>
                </section>}
                {selectedItem && "attendees" in selectedItem && selectedItem.conflicts.length > 0 && (
                  <section className="calendar-conflicts">
                    <h4><CircleAlert size={16} /> Scheduling conflicts</h4>
                    {selectedItem.conflicts.map((conflict) => (
                      <button type="button" key={conflict.id} onClick={() => setSelectedId(conflict.id)}>
                        <strong>{conflict.title}</strong>
                        <span>{formatDateTime(conflict.start_at)} / {domainLabels[conflict.domain_key ?? "global"] ?? conflict.domain_key}</span>
                      </button>
                    ))}
                  </section>
                )}
              </>
            )}

            {(creatingTodo || (selectedItem && "todo_type" in selectedItem)) && (
              <>
                {creatingTodo && <label>
                  Domain
                  <select value={todoDomainKey} onChange={(event) => setTodoDomainKey(event.target.value)}>
                    {Object.entries(domainLabels).filter(([key]) => key !== "global").map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                </label>}
                <label>
                  Title
                  <input value={draft.title ?? ""} onChange={(event) => updateDraft("title", event.target.value)} />
                </label>
                <label>
                  Description
                  <textarea value={draft.description ?? ""} onChange={(event) => updateDraft("description", event.target.value)} />
                </label>
                <div className="two-column-fields">
                  <label>
                    Due
                    <input type="datetime-local" value={calendarInputValue(draft.due_at)} onChange={(event) => updateDraft("due_at", event.target.value)} />
                  </label>
                  <label>
                    Priority
                    <input value={draft.priority ?? ""} onChange={(event) => updateDraft("priority", event.target.value)} />
                  </label>
                </div>
                <div className="two-column-fields">
                  <label>
                    Time estimate (minutes)
                    <input type="number" min="5" max="480" step="5" value={draft.estimated_minutes ?? ""} onChange={(event) => updateDraft("estimated_minutes", event.target.value)} placeholder="Maestro will estimate" />
                  </label>
                  <label>
                    Scheduled time
                    <input type="datetime-local" value={draft.scheduled_start_at ?? ""} onChange={(event) => updateDraft("scheduled_start_at", event.target.value)} />
                  </label>
                </div>
                <label className="toggle-row">
                  <input type="checkbox" checked={draft.agent_task === "true"} onChange={(event) => updateDraft("agent_task", event.target.checked ? "true" : "false")} />
                  Assign to Maestro agents in the background
                </label>
                {selectedItem && "todo_type" in selectedItem && selectedItem.agent_task && (
                  <div className="preview-meta">
                    <span>Agent task: {selectedItem.agent_task_status.replace(/_/g, " ")}</span>
                    {selectedItem.agent_task_error && <span>{selectedItem.agent_task_error}</span>}
                  </div>
                )}
                {selectedItem && "todo_type" in selectedItem && eventReferences(selectedItem.event_links)}
                <div className="two-column-fields">
                  <label>
                    Owner type
                    <input value={draft.owner_type ?? ""} onChange={(event) => updateDraft("owner_type", event.target.value)} />
                  </label>
                  <label>
                    Owner
                    <input value={draft.owner_ref ?? ""} onChange={(event) => updateDraft("owner_ref", event.target.value)} />
                  </label>
                </div>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
              </>
            )}

            {selectedItem && "email" in selectedItem && (
              <>
                <label>
                  Name
                  <input value={draft.name ?? ""} onChange={(event) => updateDraft("name", event.target.value)} />
                </label>
                <div className="two-column-fields">
                  <label>
                    Email
                    <input value={draft.email ?? ""} onChange={(event) => updateDraft("email", event.target.value)} />
                  </label>
                  <label>
                    Phone
                    <input value={draft.phone ?? ""} onChange={(event) => updateDraft("phone", event.target.value)} />
                  </label>
                </div>
                <label>
                  LinkedIn
                  <input value={draft.linkedin ?? ""} onChange={(event) => updateDraft("linkedin", event.target.value)} />
                </label>
                <label>
                  Aliases
                  <input
                    value={draft.aliases ?? ""}
                    onChange={(event) => updateDraft("aliases", event.target.value)}
                    placeholder="Chris F, C. Flournoy"
                  />
                </label>
                <label>
                  Summary
                  <textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} />
                </label>
                <label>
                  Origination
                  <textarea value={draft.origination ?? ""} onChange={(event) => updateDraft("origination", event.target.value)} />
                </label>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
                <div className="preview-meta">
                  <span>{selectedItem.affiliations[0]?.organization ?? "No linked organization"}</span>
                  <span>{selectedItem.scheduled_event_ids.length} scheduled contacts</span>
                </div>
                {selectedItem.affiliations.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Organizations</h4>
                    {selectedItem.affiliations.map((affiliation) => (
                      <div className="contact-evidence-row" key={affiliation.id}>
                        <strong>{affiliation.organization}</strong>
                        <span>{[affiliation.role, affiliation.domain_key].filter(Boolean).join(" / ") || affiliation.relationship_type}</span>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.interactions.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Interaction timeline</h4>
                    {selectedItem.interactions.map((interaction) => (
                      <div className="contact-evidence-row" key={interaction.id}>
                        <strong>{formatDateTime(interaction.occurred_at)}</strong>
                        <span>{[interaction.domain_key, interaction.channel, interaction.interaction_type].filter(Boolean).join(" / ")}</span>
                        <p>{interaction.summary}</p>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.upcoming_events.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Upcoming meetings</h4>
                    {selectedItem.upcoming_events.map((event) => (
                      <div className="contact-evidence-row" key={event.id}>
                        <strong>{event.title}</strong>
                        <span>{formatDateTime(event.start_at)}{event.location ? ` / ${event.location}` : ""}</span>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.relationships.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Relationships</h4>
                    {selectedItem.relationships.map((relationship) => (
                      <div className="contact-evidence-row" key={relationship.id}>
                        <strong>{relationship.name}</strong>
                        <span>{relationship.relationship_type.replace(/_/g, " ")}</span>
                        <p>{relationship.description}</p>
                      </div>
                    ))}
                  </section>
                )}
                <section className="contact-intelligence-section">
                  <h4>Merge duplicate</h4>
                  <div className="contact-merge-row">
                    <select value={mergeTargetId} onChange={(event) => setMergeTargetId(event.target.value)}>
                      <option value="">Select duplicate contact</option>
                      {items
                        .filter((item) => "email" in item && item.id !== selectedItem.id)
                        .map((item) => (
                          <option key={item.id} value={item.id}>{routedObjectTitle(item)}</option>
                        ))}
                    </select>
                    <button className="danger-action" type="button" disabled={!mergeTargetId || busy} onClick={mergeSelectedContact}>
                      Merge
                    </button>
                  </div>
                </section>
              </>
            )}

            {selectedItem && "website" in selectedItem && !("email" in selectedItem) && (
              <>
                <label>
                  Name
                  <input value={draft.name ?? ""} onChange={(event) => updateDraft("name", event.target.value)} />
                </label>
                <label>
                  Website
                  <input value={draft.website ?? ""} onChange={(event) => updateDraft("website", event.target.value)} />
                </label>
                <label>
                  Aliases
                  <input
                    value={draft.aliases ?? ""}
                    onChange={(event) => updateDraft("aliases", event.target.value)}
                    placeholder="Acme, Acme Corp"
                  />
                </label>
                <label>
                  Summary
                  <textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} />
                </label>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
                {selectedItem.identifiers.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Identifiers</h4>
                    {selectedItem.identifiers.map((identifier) => (
                      <div className="contact-evidence-row" key={identifier.id}>
                        <strong>{identifier.value}</strong>
                        <span>{identifier.type.replace(/_/g, " ")}</span>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.contacts.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>People</h4>
                    {selectedItem.contacts.map((contact) => (
                      <div className="contact-evidence-row" key={`${contact.id}-${contact.domain_key ?? "global"}-${contact.role}`}>
                        <strong>{contact.name}</strong>
                        <span>{[contact.role, contact.domain_key, contact.email].filter(Boolean).join(" / ")}</span>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.interactions.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Interaction timeline</h4>
                    {selectedItem.interactions.map((interaction) => (
                      <div className="contact-evidence-row" key={interaction.id}>
                        <strong>{interaction.contact_name} / {formatDateTime(interaction.occurred_at)}</strong>
                        <span>{[interaction.domain_key, interaction.channel, interaction.interaction_type].filter(Boolean).join(" / ")}</span>
                        <p>{interaction.summary}</p>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.relationships.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Connected organizations</h4>
                    {selectedItem.relationships.map((relationship) => (
                      <div className="contact-evidence-row" key={relationship.id}>
                        <strong>{relationship.organization}</strong>
                        <span>{relationship.relationship_type.replace(/_/g, " ")} / {relationship.direction}</span>
                        <p>{relationship.description}</p>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.events.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Calendar</h4>
                    {selectedItem.events.map((event) => (
                      <div className="contact-evidence-row" key={event.id}>
                        <strong>{event.title}</strong>
                        <span>{formatDateTime(event.start_at)} / {event.role}</span>
                      </div>
                    ))}
                  </section>
                )}
                {selectedItem.domain_notes.length > 0 && (
                  <section className="contact-intelligence-section">
                    <h4>Domain context</h4>
                    {selectedItem.domain_notes.map((note, index) => (
                      <div className="contact-evidence-row" key={`${note.domain_key ?? "global"}-${index}`}>
                        <strong>{domainLabels[note.domain_key ?? "global"] ?? note.domain_key ?? "Global"}</strong>
                        <p>{note.notes}</p>
                      </div>
                    ))}
                  </section>
                )}
                <section className="contact-intelligence-section">
                  <h4>Merge duplicate</h4>
                  <div className="contact-merge-row">
                    <select value={mergeTargetId} onChange={(event) => setMergeTargetId(event.target.value)}>
                      <option value="">Select duplicate organization</option>
                      {items
                        .filter((item) => "website" in item && !("email" in item) && item.id !== selectedItem.id)
                        .map((item) => <option key={item.id} value={item.id}>{routedObjectTitle(item)}</option>)}
                    </select>
                    <button className="danger-action" type="button" disabled={!mergeTargetId || busy} onClick={mergeSelectedOrganization}>Merge</button>
                  </div>
                </section>
              </>
            )}

            <div className="routed-detail-actions">
              {selectedItem && surface === "todos" && selectedItem.status !== "done" && (
                <button className="planner-action" onClick={markSelectedDone} disabled={busy}>
                  <CheckCircle2 size={16} />
                  Done
                </button>
              )}
              {selectedItem && surface === "calendar" && selectedItem.status !== "cancelled" && selectedItem.status !== "archived" && (
                <button className="danger-action" onClick={() => setSelectedEventStatus("cancelled")} disabled={busy}>
                  <X size={16} />
                  Cancel event
                </button>
              )}
              {selectedItem && surface === "calendar" && selectedItem.status === "cancelled" && (
                <button className="planner-action" onClick={() => setSelectedEventStatus("scheduled")} disabled={busy}>
                  <RefreshCw size={16} />
                  Restore event
                </button>
              )}
              <button className="planner-action" onClick={saveSelected} disabled={busy}>
                Save
              </button>
              {selectedItem && (
                <button className="danger-action" onClick={archiveSelected} disabled={busy}>
                  <Trash2 size={16} />
                  Archive
                </button>
              )}
            </div>

            {selectedItem && <div className="routed-object-meta">
              <div className="preview-meta">
                <span>{domainLabels[routedObjectDomain(selectedItem) ?? "global"] ?? routedObjectDomain(selectedItem) ?? "Global"}</span>
                <span>Created {formatDateTime(selectedItem.created_at)}</span>
                <span>{selectedItem.source_refs.length} source refs</span>
              </div>
              <details>
                <summary>Provenance</summary>
                <pre>{safeJson(selectedItem.provenance)}</pre>
              </details>
              <details>
                <summary>Metadata</summary>
                <pre>{safeJson(selectedItem.metadata)}</pre>
              </details>
              <details>
                <summary>Source refs</summary>
                <pre>{safeJson(selectedItem.source_refs)}</pre>
              </details>
            </div>}
            <p className="memory-status">{statusMessage}</p>
          </div>
        ) : (
          <p className="empty-state">{config.empty}</p>
        )}
      </section>
    </div>
  );
}

function NeedsAttentionPanel({
  schedulerDashboard,
  pendingToolApprovals,
  busyToolCallId,
  onApproveToolCall,
  onRejectToolCall,
  onSubmitAttentionResponse,
  onArchiveRun,
}: {
  schedulerDashboard: SchedulerDashboard | null;
  pendingToolApprovals: MaestroRun["tool_activity"];
  busyToolCallId: string | null;
  onApproveToolCall: (toolCallId: string) => Promise<void>;
  onRejectToolCall: (toolCallId: string) => Promise<void>;
  onSubmitAttentionResponse: (run: SchedulerRun, message: string) => Promise<void>;
  onArchiveRun: (runId: string) => Promise<void>;
}) {
  const [todos, setTodos] = useState<RoutedTodo[]>([]);
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [responseDrafts, setResponseDrafts] = useState<Record<string, string>>({});
  const [busyResponseRunId, setBusyResponseRunId] = useState<string | null>(null);
  const [busyTodoId, setBusyTodoId] = useState<string | null>(null);

  const refreshTodos = useCallback(async () => {
    const response = await apiJson<{ todos: RoutedTodo[] }>(
      "/memory/routed-objects/todos?status=needs_input&limit=20",
    );
    setTodos(response.todos);
    setStatusMessage("Ready");
  }, []);

  useEffect(() => {
    refreshTodos().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : "Unable to load attention items."),
    );
  }, [refreshTodos]);

  const blockedRuns =
    schedulerDashboard?.runs.filter((run) =>
      run.status === "blocked" ||
      run.queue_items.some((item) => ["blocked", "approval_required"].includes(item.status)),
    ) ?? [];
  const transientApprovalIds = new Set(
    pendingToolApprovals.flatMap((activity) => activity.tool_call_id ? [activity.tool_call_id] : []),
  );
  const runToolApprovals = blockedRuns.flatMap((run) => {
    const activity = Array.isArray(run.output_payload?.tool_activity)
      ? run.output_payload.tool_activity as MaestroRun["tool_activity"]
      : [];
    return activity
      .filter((item) => item.status === "approval_required" && item.tool_call_id && !transientApprovalIds.has(item.tool_call_id))
      .map((item) => ({ ...item, run }));
  });

  const submitResponse = async (run: SchedulerRun) => {
    const message = (responseDrafts[run.id] ?? "").trim();
    if (!message) return;
    setBusyResponseRunId(run.id);
    try {
      await onSubmitAttentionResponse(run, message);
      setResponseDrafts((drafts) => ({ ...drafts, [run.id]: "" }));
      setStatusMessage("Response sent to Maestro.");
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not send response.");
    } finally {
      setBusyResponseRunId(null);
    }
  };

  const updateAttentionTodo = async (todoId: string, status: "done" | "archived") => {
    setBusyTodoId(todoId);
    try {
      await apiJson(`/memory/routed-objects/todos/${todoId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: { status } }),
      });
      setStatusMessage(status === "done" ? "Attention item marked done." : "Attention item archived.");
      await refreshTodos();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Could not update attention item.");
    } finally {
      setBusyTodoId(null);
    }
  };

  return (
    <section className="attention-strip" aria-labelledby="attention-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Action items</p>
          <h3 id="attention-heading">Needs attention</h3>
        </div>
        <button className="icon-button" onClick={refreshTodos} title="Refresh attention items">
          <RefreshCw size={18} />
        </button>
      </div>
      <div className="attention-grid">
        {pendingToolApprovals.map((activity) => (
          <article className="attention-card" key={activity.tool_call_id ?? `${activity.agent_key}-${activity.tool_name}`}>
            <CircleAlert size={18} />
            <div>
              <span>{activity.tool_name}</span>
              <h4>{activity.agent_name} needs approval</h4>
              <p>{activity.details || "Review this tool request before Maestro continues."}</p>
              {activity.tool_call_id && (
                <div className="tool-approval-actions">
                  <button
                    className="planner-action"
                    onClick={() => onApproveToolCall(activity.tool_call_id!)}
                    disabled={busyToolCallId === activity.tool_call_id}
                    type="button"
                  >
                    Approve
                  </button>
                  <button
                    className="danger-action"
                    onClick={() => onRejectToolCall(activity.tool_call_id!)}
                    disabled={busyToolCallId === activity.tool_call_id}
                    type="button"
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          </article>
        ))}
        {runToolApprovals.map((activity) => (
          <article className="attention-card" key={activity.tool_call_id ?? `${activity.run.id}-${activity.tool_name}`}>
            <CircleAlert size={18} />
            <div>
              <span>{activity.tool_name}</span>
              <h4>{activity.agent_name} needs approval</h4>
              <p>{activity.details || activity.run.error_message || "Review this tool request before Maestro continues."}</p>
              {activity.tool_call_id && (
                <div className="tool-approval-actions">
                  <button
                    className="planner-action"
                    onClick={() => onApproveToolCall(activity.tool_call_id!)}
                    disabled={busyToolCallId === activity.tool_call_id}
                    type="button"
                  >
                    Approve
                  </button>
                  <button
                    className="danger-action"
                    onClick={() => onRejectToolCall(activity.tool_call_id!)}
                    disabled={busyToolCallId === activity.tool_call_id}
                    type="button"
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          </article>
        ))}
        {blockedRuns.map((run) => (
          <article className="attention-card" key={run.id}>
            <CircleAlert size={18} />
            <div>
              <span>{run.status}</span>
              <h4>{run.summary || "Workflow needs attention"}</h4>
              <p>{run.error_message || "Open Workflows to inspect blocked or failed work."}</p>
              <div className="attention-blocker-list">
                {run.queue_items
                  .filter((item) => ["blocked", "approval_required"].includes(item.status))
                  .map((item) => (
                    <div className="attention-blocker-row" key={item.id}>
                      <strong>{item.agent_name ?? item.agent_key ?? "Unassigned"}</strong>
                      <span>{item.status}</span>
                      <p>{item.error_message || item.objective}</p>
                    </div>
                  ))}
              </div>
              <div className="attention-response">
                <textarea
                  value={responseDrafts[run.id] ?? ""}
                  onChange={(event) =>
                    setResponseDrafts((drafts) => ({ ...drafts, [run.id]: event.target.value }))
                  }
                  placeholder="Answer the blocker or add a short instruction..."
                  rows={2}
                />
                <button
                  className="planner-action"
                  onClick={() => submitResponse(run)}
                  disabled={busyResponseRunId === run.id || !(responseDrafts[run.id] ?? "").trim()}
                  type="button"
                >
                  Send & resume
                </button>
                <button
                  className="danger-action"
                  onClick={() => onArchiveRun(run.id)}
                  type="button"
                >
                  Kill workflow
                </button>
              </div>
            </div>
          </article>
        ))}
        {todos.map((todo) => (
          <article className="attention-card" key={todo.id}>
            <ListTodo size={18} />
            <div>
              <span>{domainLabels[todo.domain_key ?? "global"] ?? todo.domain_key ?? "Global"}</span>
              <h4>{todo.title}</h4>
              <p>{todo.description}</p>
              <div className="attention-actions">
                <button
                  className="planner-action"
                  onClick={() => updateAttentionTodo(todo.id, "done")}
                  disabled={busyTodoId === todo.id}
                  type="button"
                >
                  <CheckCircle2 size={15} />
                  Done
                </button>
                <button
                  className="danger-action"
                  onClick={() => updateAttentionTodo(todo.id, "archived")}
                  disabled={busyTodoId === todo.id}
                  type="button"
                >
                  <Archive size={15} />
                  Archive
                </button>
              </div>
            </div>
          </article>
        ))}
        {pendingToolApprovals.length === 0 && runToolApprovals.length === 0 && blockedRuns.length === 0 && todos.length === 0 && (
          <p className="empty-state">No blocked workflows, approvals, or RFIs are waiting right now.</p>
        )}
      </div>
      <p className="memory-status">{statusMessage}</p>
    </section>
  );
}

function IssuesWorkspace() {
  const [issues, setIssues] = useState<ProductIssue[]>([]);
  const [projects, setProjects] = useState<ProductProject[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("open");
  const [projectFilter, setProjectFilter] = useState("all");
  const [statusMessage, setStatusMessage] = useState("Loading product issues...");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Partial<ProductIssue>>({});

  const load = useCallback(async () => {
    const [issueResponse, projectResponse] = await Promise.all([
      apiJson<{ issues: ProductIssue[] }>("/issues?limit=250"),
      apiJson<{ projects: ProductProject[] }>("/issues/projects"),
    ]);
    setIssues(issueResponse.issues);
    setProjects(projectResponse.projects);
    setSelectedId((current) => current && issueResponse.issues.some((item) => item.id === current) ? current : issueResponse.issues[0]?.id ?? null);
    setStatusMessage(`${issueResponse.issues.length} canonical product issues loaded.`);
  }, []);

  useEffect(() => {
    load().catch((error) => setStatusMessage(error instanceof Error ? error.message : "Issues failed to load."));
  }, [load]);

  const selected = issues.find((item) => item.id === selectedId) ?? null;
  useEffect(() => {
    setDraft(selected ? { ...selected, acceptance_criteria: [...selected.acceptance_criteria] } : {});
  }, [selected]);

  const visible = useMemo(() => issues.filter((issue) => {
    const matchesQuery = !query.trim() || [issue.title, issue.problem, issue.desired_outcome, issue.external_repo].some((value) => String(value ?? "").toLowerCase().includes(query.trim().toLowerCase()));
    const matchesStatus = statusFilter === "all" || (statusFilter === "open" ? !["completed", "cancelled", "superseded"].includes(issue.status) : issue.status === statusFilter);
    const matchesProject = projectFilter === "all" || issue.project_id === projectFilter;
    return matchesQuery && matchesStatus && matchesProject;
  }), [issues, projectFilter, query, statusFilter]);

  const save = async () => {
    if (!selected || !draft.title?.trim()) return;
    setBusy(true);
    try {
      const response = await apiJson<{ issue: ProductIssue }>(`/issues/${selected.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          updates: {
            title: draft.title,
            problem: draft.problem ?? "",
            desired_outcome: draft.desired_outcome ?? "",
            acceptance_criteria: draft.acceptance_criteria ?? [],
            notes: draft.notes ?? "",
            issue_type: draft.issue_type,
            priority: draft.priority,
            status: draft.status,
            estimated_minutes: draft.estimated_minutes,
            agent_task: draft.agent_task,
          },
        }),
      });
      setIssues((current) => current.map((item) => item.id === response.issue.id ? response.issue : item));
      setStatusMessage(`Saved ${response.issue.title}.`);
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Issue update failed.");
    } finally {
      setBusy(false);
    }
  };

  const syncRepository = async () => {
    if (!selected?.repository_id) return;
    setBusy(true);
    try {
      const response = await apiJson<{ result: Record<string, number | string> }>(`/issues/repositories/${selected.repository_id}/sync`, { method: "POST" });
      setStatusMessage(`GitHub sync complete: ${JSON.stringify(response.result)}.`);
      await load();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "GitHub sync failed.");
    } finally {
      setBusy(false);
    }
  };

  const renderIssueDetail = (mobile = false) => selected ? (
    <div className={mobile ? "mobile-routed-detail issue-mobile-detail" : "routed-object-detail"}>
      <div className={mobile ? "mobile-routed-detail-heading" : "section-heading"}>
        <div>
          {!mobile && <p className="eyebrow">Canonical issue</p>}
          <h3>{mobile ? selected.title : selected.external_number ? `#${selected.external_number}` : "Local issue"}</h3>
        </div>
        <div className="section-heading-actions">
          {selected.external_url && <a className="icon-button" href={selected.external_url} target="_blank" rel="noreferrer" title="Open in GitHub"><ExternalLink size={16} /></a>}
          {mobile && <button className="icon-button" type="button" title="Minimize details" onClick={() => setSelectedId(null)}><X size={16} /></button>}
        </div>
      </div>
      <label>Title<input value={draft.title ?? ""} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} /></label>
      <div className="two-column-fields">
        <label>Type<select value={draft.issue_type ?? "feature"} onChange={(event) => setDraft((current) => ({ ...current, issue_type: event.target.value }))}><option>feature</option><option>bug</option><option>story</option><option>architecture</option><option>research</option><option>chore</option><option>idea</option></select></label>
        <label>Status<select value={draft.status ?? "ready"} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value }))}><option>ready</option><option>active</option><option>blocked</option><option>review</option><option>completed</option><option>cancelled</option><option>superseded</option></select></label>
      </div>
      <label>Problem<textarea value={draft.problem ?? ""} onChange={(event) => setDraft((current) => ({ ...current, problem: event.target.value }))} /></label>
      <label>Desired outcome<textarea value={draft.desired_outcome ?? ""} onChange={(event) => setDraft((current) => ({ ...current, desired_outcome: event.target.value }))} /></label>
      <label>Acceptance criteria<textarea value={(draft.acceptance_criteria ?? []).join("\n")} onChange={(event) => setDraft((current) => ({ ...current, acceptance_criteria: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean) }))} placeholder="One criterion per line" /></label>
      <label>Notes<textarea value={draft.notes ?? ""} onChange={(event) => setDraft((current) => ({ ...current, notes: event.target.value }))} /></label>
      <div className="two-column-fields">
        <label>Priority<select value={draft.priority ?? "normal"} onChange={(event) => setDraft((current) => ({ ...current, priority: event.target.value }))}><option>low</option><option>normal</option><option>high</option><option>urgent</option></select></label>
        <label>Estimate (minutes)<input type="number" min="5" value={draft.estimated_minutes ?? ""} onChange={(event) => setDraft((current) => ({ ...current, estimated_minutes: event.target.value ? Number(event.target.value) : null }))} /></label>
      </div>
      <label className="checkbox-row"><input type="checkbox" checked={Boolean(draft.agent_task)} onChange={(event) => setDraft((current) => ({ ...current, agent_task: event.target.checked }))} /> Agent task</label>
      {selected.event_links.length > 0 && <section className="contact-intelligence-section event-reference-list">
        <h4>Calendar links</h4>
        {selected.event_links.map((link) => <div className="contact-evidence-row" key={link.id}>
          <strong>{link.event_title}</strong>
          <span>{link.relationship_type.replace("_", " ")} / {formatDateTime(link.start_at)}</span>
        </div>)}
      </section>}
      <div className="routed-detail-actions"><button className="planner-action" type="button" onClick={save} disabled={busy}>Save issue</button>{selected.repository_id && <button type="button" onClick={syncRepository} disabled={busy}><RefreshCw size={15} /> Sync GitHub</button>}</div>
      {selected.relations.length > 0 && <details><summary>Issue relationships ({selected.relations.length})</summary><pre>{JSON.stringify(selected.relations, null, 2)}</pre></details>}
    </div>
  ) : <p className="empty-state">Select an issue to inspect it.</p>;

  return (
    <section className="panel routed-object-workspace issue-workspace">
      <div className="routed-object-list-panel">
        <div className="section-heading">
          <div><p className="eyebrow">Product intelligence</p><h3>Issues</h3></div>
          <button className="icon-button" type="button" onClick={load} title="Refresh issues"><RefreshCw size={16} /></button>
        </div>
        <div className="routed-filter-row">
          <label className="contact-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search issues" /></label>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="Issue status filter">
            <option value="open">Open work</option><option value="all">All issues</option><option value="ready">Ready</option><option value="active">Active</option><option value="blocked">Blocked</option><option value="review">Review</option><option value="completed">Completed</option>
          </select>
          <select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)} aria-label="Project filter">
            <option value="all">All projects</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </div>
        <div className="routed-object-list">
          {visible.map((issue) => (
            <div className="routed-object-mobile-group" key={issue.id}>
              <button className={`routed-object-row${issue.id === selectedId ? " active" : ""}`} onClick={() => setSelectedId((current) => current === issue.id ? null : issue.id)} type="button">
                <GitPullRequest size={17} />
                <span><strong>{issue.title}</strong><small>{issue.project?.name ?? "Unassigned project"}{issue.repository ? ` / ${issue.repository.display_name}` : ""} · {issue.status}</small><small>{issue.external_number ? `GitHub #${issue.external_number}` : "Local"} · {issue.sync_status}</small></span>
              </button>
              {issue.id === selectedId && renderIssueDetail(true)}
            </div>
          ))}
          {visible.length === 0 && <p className="empty-state">No issues match these filters.</p>}
        </div>
        <p className="memory-status">{statusMessage}</p>
      </div>
      <div className="routed-object-detail-panel">
        {renderIssueDetail()}
      </div>
    </section>
  );
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activeDomain, setActiveDomain] = useState("Maestro");
  const [activeSurface, setActiveSurface] = useState<ActiveSurface>("dashboard");
  const [maestroNavOpen, setMaestroNavOpen] = useState(false);
  const [memoryNavOpen, setMemoryNavOpen] = useState(false);
  const [domainsNavOpen, setDomainsNavOpen] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const chatThreadRef = useRef<HTMLDivElement | null>(null);
  const [sessionHistory, setSessionHistory] = useState<MaestroSessionSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [schedulerDashboard, setSchedulerDashboard] = useState<SchedulerDashboard | null>(null);
  const [workflowRunLog, setWorkflowRunLog] = useState<WorkflowRunLogEntry[]>([]);
  const [workflowReports, setWorkflowReports] = useState<WorkflowReport[]>([]);
  const [selectedWorkflowReport, setSelectedWorkflowReport] = useState<WorkflowReport | null>(null);
  const [workflowOutputsStatus, setWorkflowOutputsStatus] = useState("");
  const [showSessionHistory, setShowSessionHistory] = useState(false);
  const [draftMessage, setDraftMessage] = useState("");
  const [maestroPlan, setMaestroPlan] = useState<MaestroPlan | null>(null);
  const [maestroRun, setMaestroRun] = useState<MaestroRun | null>(null);
  const [maestroStatus, setMaestroStatus] = useState("Ready");
  const [maestroInteractionMode, setMaestroInteractionMode] = useState<
    "knowledge" | "workflow_builder"
  >("knowledge");
  const executeMaestroLLM = true;
  const autoMaestroToolLoop = true;
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
  const [selectedDefinitionRuns, setSelectedDefinitionRuns] = useState<SchedulerRun[]>([]);

  useEffect(() => {
    const thread = chatThreadRef.current;
    if (!thread) return;
    thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" });
  }, [chatMessages, maestroBusy]);
  const [schedulerWorkerStatus, setSchedulerWorkerStatus] =
    useState<SchedulerWorkerStatus | null>(null);
  const [gmailTriggerStatus, setGmailTriggerStatus] = useState<GmailTriggerStatus | null>(null);
  const [calendarTriggerStatus, setCalendarTriggerStatus] = useState<CalendarTriggerStatus | null>(null);
  const [workflowTemplates, setWorkflowTemplates] = useState<WorkflowTemplate[]>([]);

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
      ["approval_required", "queued", "ready", "running", "retrying", "pending"].includes(
        item.status,
      ),
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

  const activeWorkflowCount = useMemo(
    () =>
      (schedulerDashboard?.runs ?? []).filter((run) =>
        ["queued", "ready", "running", "blocked", "approval_required", "retrying"].includes(run.status),
      ).length,
    [schedulerDashboard],
  );

  const planScheduleCandidate = useMemo(() => {
    const candidate = maestroPlan?.scheduler.schedule_candidate;
    return candidate && typeof candidate === "object"
      ? (candidate as Record<string, unknown>)
      : null;
  }, [maestroPlan]);
  const showPlanPreview =
    maestroInteractionMode === "workflow_builder" && shouldShowPlanPreview(maestroPlan);
  const showInlineRunPreview = shouldShowInlineRunPreview(maestroRun);

  const scheduledWorkflowDefinitions = useMemo(
    () =>
      (schedulerDashboard?.definitions ?? []).filter((definition) =>
        ["scheduled", "recurring"].includes(definition.trigger_type),
      ),
    [schedulerDashboard],
  );

  const triggerWorkflowDefinitions = useMemo(
    () =>
      (schedulerDashboard?.definitions ?? []).filter(
        (definition) => definition.trigger_type === "event",
      ),
    [schedulerDashboard],
  );

  const pendingToolApprovals = useMemo(
    () =>
      (maestroRun?.tool_activity ?? []).filter(
        (activity) => activity.status === "approval_required" && activity.tool_call_id,
      ),
    [maestroRun],
  );

  const conductingMessage = useMemo(() => {
    return workflowProgressLabel({
      plan: maestroPlan,
      run: maestroRun,
      busyToolCallId,
      maestroStatus,
    });
  }, [busyToolCallId, maestroPlan, maestroRun, maestroStatus]);

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
    const normalized = message.trim().toLowerCase().replace(/[^a-z0-9 ]+/g, " ").trim();
    if (["approved", "approve", "yes approved", "yes approve", "go ahead", "run it", "proceed"].includes(normalized)) {
      return true;
    }
    return /^(?:yes )?approve(?:d)?\b/.test(normalized) && !/\b(?:why|what|how|explain)\b/.test(normalized);
  };

  const loadSessionHistory = useCallback(async () => {
    const response = await apiJson<{ sessions: MaestroSessionSummary[] }>("/maestro/sessions");
    setSessionHistory(response.sessions);
  }, []);

  const applyConversation = useCallback((conversation: MaestroSessionSummary) => {
    setActiveConversationId(conversation.id);
    setChatMessages(conversation.messages ?? []);
    setMaestroPlan(shouldShowPlanPreview(conversation.active_plan ?? null) ? conversation.active_plan ?? null : null);
  }, []);

  const pollActiveChannel = useCallback(async () => {
    const response = await apiJson<{ conversation: MaestroSessionSummary }>(
      "/maestro/sessions/active",
    );
    setActiveConversationId(response.conversation.id);
    setChatMessages(response.conversation.messages ?? []);
    const responsePlan = response.conversation.active_plan ?? null;
    setMaestroPlan(shouldShowPlanPreview(responsePlan) ? responsePlan : null);
  }, []);

  const loadActiveSession = useCallback(async () => {
    const response = await apiJson<{ conversation: MaestroSessionSummary }>(
      "/maestro/sessions/active",
    );
    applyConversation(response.conversation);
  }, [applyConversation]);

  const loadSchedulerDashboard = useCallback(async () => {
    const [dashboardResult, templatesResult] = await Promise.allSettled([
      apiJson<SchedulerDashboard>("/scheduler/dashboard"),
      apiJson<{ templates: WorkflowTemplate[] }>("/scheduler/templates"),
    ]);
    if (dashboardResult.status === "fulfilled") {
      setSchedulerDashboard(dashboardResult.value);
      setSelectedSchedulerRun((selected) => {
        if (!selected) return selected;
        return dashboardResult.value.runs.find((run) => run.id === selected.id) ?? null;
      });
    }
    if (templatesResult.status === "fulfilled") {
      setWorkflowTemplates(templatesResult.value.templates);
    }
    if (dashboardResult.status === "rejected") {
      const message =
        dashboardResult.reason instanceof Error
          ? dashboardResult.reason.message
          : "Could not load workflows.";
      setSchedulerStatusMessage(message);
      throw dashboardResult.reason;
    }
    if (templatesResult.status === "rejected") {
      setSchedulerStatusMessage(
        "Durable workflows loaded, but workflow templates could not be refreshed.",
      );
    }
  }, []);

  const installWorkflowTemplate = async (templateKey: string) => {
    await apiJson(`/scheduler/templates/${templateKey}/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: false }),
    });
    setSchedulerStatusMessage("Durable workflow was installed paused for review.");
    await loadSchedulerDashboard();
  };

  const updateWorkflowDefinitionActivation = async (
    definitionId: string,
    isActive: boolean,
  ) => {
    await apiJson(`/scheduler/definitions/${definitionId}/activation`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: isActive }),
    });
    setSchedulerStatusMessage(
      isActive ? "Workflow definition activated." : "Workflow definition paused.",
    );
    await loadSchedulerDashboard();
  };

  const loadWorkflowOutputs = useCallback(async () => {
    const [runLogResponse, reportsResponse] = await Promise.all([
      apiJson<{ entries: WorkflowRunLogEntry[] }>("/workflow-outputs/run-log?limit=50"),
      apiJson<{ reports: WorkflowReport[] }>("/workflow-outputs/reports?limit=50"),
    ]);
    setWorkflowRunLog(runLogResponse.entries);
    setWorkflowReports(reportsResponse.reports);
    setWorkflowOutputsStatus("Workflow outputs refreshed.");
  }, []);

  const openWorkflowReport = async (reportId: string) => {
    const response = await apiJson<{ report: WorkflowReport }>(
      `/workflow-outputs/reports/${reportId}`,
    );
    setSelectedWorkflowReport(response.report);
    setActiveSurface("reports");
  };

  const archiveWorkflowReport = async (reportId: string) => {
    await apiJson<{ report: WorkflowReport }>(`/workflow-outputs/reports/${reportId}/archive`, {
      method: "PATCH",
    });
    if (selectedWorkflowReport?.id === reportId) {
      setSelectedWorkflowReport(null);
    }
    await loadWorkflowOutputs();
  };

  const archiveAllWorkflowReports = async () => {
    await apiJson<{ archived_count: number }>("/workflow-outputs/reports/archive", {
      method: "POST",
    });
    setSelectedWorkflowReport(null);
    await loadWorkflowOutputs();
  };

  const loadSchedulerWorkerStatus = useCallback(async () => {
    const response = await apiJson<{ worker: SchedulerWorkerStatus }>("/scheduler/worker/status");
    setSchedulerWorkerStatus(response.worker);
  }, []);

  const loadGmailTriggerStatus = useCallback(async () => {
    const response = await apiJson<GmailTriggerStatus>("/scheduler/triggers/gmail/status");
    setGmailTriggerStatus(response);
  }, []);

  const loadCalendarTriggerStatus = useCallback(async () => {
    const response = await apiJson<CalendarTriggerStatus>("/scheduler/triggers/calendar/status");
    setCalendarTriggerStatus(response);
  }, []);

  const updateDefinitionGmailWatch = async (definitionId: string, enabled: boolean) => {
    const response = await apiJson<{ worker: GmailTriggerStatus["worker"]; status: GmailTriggerStatus }>(
      `/scheduler/definitions/${definitionId}/gmail-watch`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    );
    setGmailTriggerStatus(response.status);
    setSchedulerStatusMessage(
      response.worker.enabled
        ? "This workflow is watching Gmail for new eligible messages."
        : "This workflow's Gmail watch is off.",
    );
    await loadSchedulerDashboard();
  };

  const updateDefinitionCalendarWatch = async (definitionId: string, enabled: boolean) => {
    const response = await apiJson<{ worker: CalendarTriggerStatus["worker"]; status: CalendarTriggerStatus }>(
      `/scheduler/definitions/${definitionId}/calendar-watch`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    );
    setCalendarTriggerStatus(response.status);
    setSchedulerStatusMessage(
      response.worker.enabled
        ? "This workflow is watching Google Calendar for changes."
        : "This workflow's Calendar watch is off.",
    );
    await loadSchedulerDashboard();
  };

  const updateDefinitionShadowMode = async (definitionId: string, enabled: boolean) => {
    await apiJson(`/scheduler/definitions/${definitionId}/shadow-mode`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    setSchedulerStatusMessage(
      enabled
        ? "Shadow mode enabled. Triage runs will record decisions without writing changes."
        : "Live mode enabled. Triage runs may write routed items, notifications, and read state.",
    );
    await loadSchedulerDashboard();
  };

  const pollGmailTriggers = async () => {
    await apiJson("/scheduler/triggers/gmail/poll", { method: "POST" });
    await Promise.all([loadGmailTriggerStatus(), loadSchedulerDashboard()]);
    setSchedulerStatusMessage("Gmail trigger poll completed.");
  };

  const resetGmailTriggerDomain = async (domainKey: string) => {
    await apiJson(`/scheduler/triggers/gmail/domains/${domainKey}/reset`, { method: "POST" });
    await loadGmailTriggerStatus();
    setSchedulerStatusMessage(`Reset ${domainKey} Gmail monitoring at the current inbox cursor.`);
  };

  const pollCalendarTriggers = async () => {
    await apiJson("/scheduler/triggers/calendar/poll", { method: "POST" });
    await Promise.all([loadCalendarTriggerStatus(), loadSchedulerDashboard()]);
    setSchedulerStatusMessage("Calendar trigger poll completed.");
  };

  const resetCalendarTriggerDomain = async (domainKey: string) => {
    await apiJson(`/scheduler/triggers/calendar/domains/${domainKey}/reset`, { method: "POST" });
    await loadCalendarTriggerStatus();
    setSchedulerStatusMessage(`Reset ${domainKey} Calendar monitoring at the current change cursor.`);
  };

  const replaySchedulerRun = async (runId: string) => {
    const response = await apiJson<{ run: SchedulerRun }>(`/scheduler/runs/${runId}/replay`, {
      method: "POST",
    });
    setSelectedSchedulerRun(response.run);
    setSchedulerStatusMessage("Workflow replay was queued with its original trigger payload.");
    await loadSchedulerDashboard();
  };

  const updateSchedulerWorkerStatus = async (updates: Partial<SchedulerWorkerStatus>) => {
    const response = await apiJson<{ worker: SchedulerWorkerStatus }>("/scheduler/worker/status", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    setSchedulerWorkerStatus(response.worker);
    setSchedulerStatusMessage(
      response.worker.enabled
        ? "Auto worker is on. Maestro will claim and run due queue items."
        : "Auto worker is paused. Queue items wait for manual worker runs.",
    );
  };

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

  const selectSchedulerDefinition = async (definition: SchedulerDefinition) => {
    const response = await apiJson<{
      definition: SchedulerDefinition;
      runs: SchedulerRun[];
    }>(`/scheduler/definitions/${definition.id}`);
    setSelectedSchedulerDefinition(response.definition);
    setSelectedDefinitionRuns(response.runs);
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
    const responsePlan = response.conversation.active_plan ?? maestroPlan;
    setMaestroPlan(shouldShowPlanPreview(responsePlan) ? responsePlan : null);
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
    await loadWorkflowOutputs();
  };

  const triggerSchedulerEvent = async () => {
    const response = await apiJson<{ runs: SchedulerRun[] }>("/scheduler/triggers/event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        event_type: schedulerDefinitionEvent,
        event_id: schedulerEventId || createClientId(),
        event_payload: {
          id: schedulerEventId || createClientId(),
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
    loadSchedulerWorkerStatus().catch(() => undefined);
    loadGmailTriggerStatus().catch(() => undefined);
    loadCalendarTriggerStatus().catch(() => undefined);
    loadWorkflowOutputs().catch(() => undefined);
  }, [
    loadActiveSession,
    loadSchedulerDashboard,
    loadSchedulerWorkerStatus,
    loadGmailTriggerStatus,
    loadCalendarTriggerStatus,
    loadSessionHistory,
    loadWorkflowOutputs,
  ]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      loadSchedulerDashboard().catch(() => undefined);
      loadSchedulerWorkerStatus().catch(() => undefined);
      loadGmailTriggerStatus().catch(() => undefined);
      loadCalendarTriggerStatus().catch(() => undefined);
      loadWorkflowOutputs().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [loadCalendarTriggerStatus, loadGmailTriggerStatus, loadSchedulerDashboard, loadSchedulerWorkerStatus, loadWorkflowOutputs]);

  useEffect(() => {
    let closed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;

    const connect = () => {
      socket = new WebSocket(websocketUrl("/maestro/channel/ws"));
      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as {
            type?: string;
            conversation?: MaestroSessionSummary;
          };
          if (payload.type === "conversation" && payload.conversation) {
            applyConversation(payload.conversation);
          }
        } catch {
          setMaestroStatus("Could not parse Maestro channel update.");
        }
      };
      socket.onclose = () => {
        if (!closed) {
          reconnectTimer = window.setTimeout(connect, 2000);
        }
      };
      socket.onerror = () => {
        socket?.close();
      };
    };

    connect();
    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [applyConversation]);

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
        setMaestroRun(shouldShowInlineRunPreview(response.run) ? response.run : null);
        setMaestroPlan(shouldShowPlanPreview(response.run.plan) ? response.run.plan : null);
      }
      await pollActiveChannel().catch(() => {
        setChatMessages((messages) => [
          ...messages,
          { id: createClientId(), sender: "maestro", content: response.message },
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
      await Promise.all([
        loadSchedulerDashboard().catch(() => undefined),
        loadWorkflowOutputs().catch(() => undefined),
      ]);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Tool approval failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: createClientId(), sender: "maestro", content: message },
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
          { id: createClientId(), sender: "maestro", content: response.message },
        ]);
      });
      setMaestroStatus("Tool rejected.");
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Tool rejection failed.";
      setChatMessages((messages) => [
        ...messages,
        { id: createClientId(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setBusyToolCallId(null);
    }
  };

  const submitAttentionResponse = async (run: SchedulerRun, message: string) => {
    setMaestroBusy(true);
    setMaestroStatus("Sending response to blocked workflow...");
    try {
      const response = await apiJson<MaestroRespond>("/maestro/respond", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Maestro-Async": "true" },
        body: JSON.stringify({
          message,
          active_plan_id: run.parent_task_id,
          conversation_id: run.conversation_id ?? activeConversationId,
          interaction_mode: "workflow_builder",
        }),
      });
      if (response.conversation) {
        setActiveConversationId(response.conversation.id);
        setChatMessages(response.conversation.messages ?? []);
      } else {
        setChatMessages((messages) => [
          ...messages,
          { id: createClientId(), sender: "user", content: message },
          { id: createClientId(), sender: "maestro", content: response.message },
        ]);
      }
      const responsePlan = response.plan ?? response.active_plan ?? null;
      setMaestroPlan(shouldShowPlanPreview(responsePlan) ? responsePlan : null);
      setMaestroRun(null);
      await apiJson("/scheduler/worker/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner: "maestro-ui-unblock",
          claim_limit: 3,
          execute_llm: executeMaestroLLM,
          auto_tool_loop: autoMaestroToolLoop,
        }),
      }).catch(() => undefined);
      setMaestroStatus("Response applied. Maestro attempted to resume ready workflow work.");
      await Promise.all([
        loadSchedulerDashboard().catch(() => undefined),
        loadWorkflowOutputs().catch(() => undefined),
        loadSessionHistory().catch(() => undefined),
      ]);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Could not send response.";
      setMaestroStatus(errorMessage);
      throw error;
    } finally {
      setMaestroBusy(false);
    }
  };

  const sendMaestroMessage = async () => {
    if (!draftMessage.trim()) return;
    const outgoingMessage: ChatMessage = {
      id: createClientId(),
      sender: "user",
      content: draftMessage.trim(),
    };
    const activePlanId =
      maestroInteractionMode === "workflow_builder" && maestroPlan
        ? maestroPlan.parent_task_id
        : null;
    setMaestroBusy(true);
    setMaestroRun(null);
    if (!activePlanId) setMaestroPlan(null);
    setMaestroStatus("Thinking through your message...");
    setChatMessages((messages) => [...messages, outgoingMessage]);
    setDraftMessage("");
    if (isApprovalMessage(outgoingMessage.content) && pendingToolApprovals.length > 0) {
      if (pendingToolApprovals.length === 1 && pendingToolApprovals[0].tool_call_id) {
        await approveToolCall(pendingToolApprovals[0].tool_call_id);
      } else {
        setChatMessages((messages) => [
          ...messages,
          {
            id: createClientId(),
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
          interaction_mode: maestroInteractionMode,
        }),
      });
      if (response.kind === "pending") {
        setMaestroStatus("Maestro is working in the background...");
        return;
      }
      const responsePlan = response.plan ?? response.active_plan ?? null;
      setMaestroPlan(
        maestroInteractionMode === "workflow_builder" && shouldShowPlanPreview(responsePlan)
          ? responsePlan
          : null,
      );
      setMaestroRun(null);
      if (response.conversation) setActiveConversationId(response.conversation.id);
      if (response.conversation?.messages?.length) {
        setChatMessages(response.conversation.messages);
      } else {
        setChatMessages((messages) => [
          ...messages,
          {
            id: createClientId(),
            sender: "maestro",
            content: response.message,
          },
        ]);
      }
      setMaestroStatus(
        response.classification === "knowledge_action"
          ? "Knowledge updated."
          : response.classification === "knowledge_chat"
            ? response.workflow_suggestion
              ? "This request needs Build workflow mode."
              : "Answered from Maestro knowledge."
        : response.kind === "chat_only"
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
        { id: createClientId(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setMaestroBusy(false);
    }
  };

  const runMaestroPlan = async () => {
    if (!maestroPlan) return;
    const isScheduledApproval = Boolean(planScheduleCandidate);
    const submittedPlan = maestroPlan;
    setMaestroBusy(true);
    setMaestroStatus(isScheduledApproval ? "Saving scheduled workflow..." : "Queueing workflow...");
    setMaestroPlan(null);
    setMaestroRun(null);
    setExpandedWorkflowNodeId(null);
    try {
      const response = await apiJson<{ run: MaestroRun }>(
        `/maestro/plans/${submittedPlan.parent_task_id}/run`,
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
      setMaestroRun(shouldShowInlineRunPreview(response.run) ? response.run : null);
      setMaestroPlan(null);
      setMaestroInteractionMode("knowledge");
      setChatMessages((messages) => {
        const alreadyShown = messages.some((message) => message.content === response.run.chat_summary);
        if (alreadyShown) return messages;
        return [
          ...messages,
          {
            id: createClientId(),
            sender: "maestro",
            content:
              response.run.status === "completed" || response.run.status === "scheduled"
                ? response.run.chat_summary
                : `The workflow finished with status ${response.run.status}.\n\n${response.run.chat_summary}`,
          },
        ];
      });
      setMaestroStatus(
        response.run.status === "scheduled"
          ? "Scheduled workflow saved."
          : response.run.status === "queued"
            ? "Workflow queued in Active Workflows."
            : `Workflow ${response.run.status}.`,
      );
      loadSessionHistory().catch(() => undefined);
      loadSchedulerDashboard().catch(() => undefined);
      loadWorkflowOutputs().catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Maestro workflow failed.";
      setMaestroPlan(shouldShowPlanPreview(submittedPlan) ? submittedPlan : null);
      setChatMessages((messages) => [
        ...messages,
        { id: createClientId(), sender: "maestro", content: message },
      ]);
      setMaestroStatus(message);
    } finally {
      setMaestroBusy(false);
    }
  };

  const clearMaestroPlan = async () => {
    if (!maestroPlan) return;
    const planId = maestroPlan.parent_task_id;
    setMaestroBusy(true);
    setMaestroStatus("Clearing candidate workflow...");
    try {
      await apiJson<{ plan: MaestroPlan }>(`/maestro/plans/${planId}/archive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reason: "Candidate workflow cleared from the Maestro UI.",
          conversation_id: activeConversationId,
        }),
      });
      setMaestroPlan(null);
      setMaestroRun(null);
      setExpandedWorkflowNodeId(null);
      setMaestroInteractionMode("knowledge");
      await Promise.all([
        pollActiveChannel().catch(() => undefined),
        loadSchedulerDashboard().catch(() => undefined),
        loadSessionHistory().catch(() => undefined),
      ]);
      setMaestroStatus("Candidate workflow cleared.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not clear candidate workflow.";
      setChatMessages((messages) => [
        ...messages,
        { id: createClientId(), sender: "maestro", content: message },
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
          <div className="brand-lockup">
            <img className="brand-mark" src="/maestro-mark.svg" alt="" />
            <div className="brand-wordmark">
              <h1>maestro</h1>
              <p>system command</p>
            </div>
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
          <div className="nav-group">
            <button
              className={
                ["dashboard", "run-log", "workflows", "reports"].includes(activeSurface)
                  ? "domain-button active"
                  : "domain-button"
              }
              onClick={() => setMaestroNavOpen((open) => !open)}
              type="button"
            >
              <Sparkles size={17} />
              <span>Maestro</span>
              {maestroNavOpen ? <ChevronDown className="nav-chevron" size={15} /> : <ChevronRight className="nav-chevron" size={15} />}
            </button>
            {maestroNavOpen && (
              <div className="nav-submenu">
                <button
                  className={activeSurface === "dashboard" ? "domain-button active" : "domain-button"}
                  onClick={() => {
                    setActiveDomain("Maestro");
                    setActiveSurface("dashboard");
                  }}
                  type="button"
                >
                  <MessageSquareText size={16} />
                  <span>Chat</span>
                </button>
                <button
                  className={activeSurface === "run-log" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("run-log")}
                  type="button"
                >
                  <Clock3 size={16} />
                  <span>Run Log</span>
                </button>
                <button
                  className={activeSurface === "workflows" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("workflows")}
                  type="button"
                >
                  <ListTodo size={16} />
                  <span>Workflows</span>
                </button>
                <button
                  className={activeSurface === "reports" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("reports")}
                  type="button"
                >
                  <FileText size={16} />
                  <span>Reports</span>
                </button>
              </div>
            )}
          </div>
          <div className="nav-group">
            <button
              className={
                ["memory", "calendar", "contacts", "todos", "organizations", "issues"].includes(activeSurface)
                  ? "domain-button active"
                  : "domain-button"
              }
              onClick={() => setMemoryNavOpen((open) => !open)}
              type="button"
            >
              <Database size={17} />
              <span>Memory</span>
              {memoryNavOpen ? <ChevronDown className="nav-chevron" size={15} /> : <ChevronRight className="nav-chevron" size={15} />}
            </button>
            {memoryNavOpen && (
              <div className="nav-submenu">
                <button
                  className={activeSurface === "memory" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("memory")}
                  type="button"
                >
                  <HardDriveUpload size={16} />
                  <span>Memory Manager</span>
                </button>
                <button
                  className={activeSurface === "calendar" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("calendar")}
                  type="button"
                >
                  <CalendarDays size={16} />
                  <span>Calendar</span>
                </button>
                <button
                  className={activeSurface === "contacts" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("contacts")}
                  type="button"
                >
                  <Users size={16} />
                  <span>Contacts</span>
                </button>
                <button
                  className={activeSurface === "todos" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("todos")}
                  type="button"
                >
                  <ListTodo size={16} />
                  <span>To Do List</span>
                </button>
                <button
                  className={activeSurface === "organizations" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("organizations")}
                  type="button"
                >
                  <Building2 size={16} />
                  <span>Organizations</span>
                </button>
                <button
                  className={activeSurface === "issues" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("issues")}
                  type="button"
                >
                  <GitPullRequest size={16} />
                  <span>Product Issues</span>
                </button>
              </div>
            )}
          </div>
          <button
            className={activeSurface === "tools" ? "domain-button active" : "domain-button"}
            onClick={() => setActiveSurface("tools")}
          >
            <Wrench size={17} />
            <span>Tools</span>
          </button>
          <button
            className={activeSurface === "skills" ? "domain-button active" : "domain-button"}
            onClick={() => setActiveSurface("skills")}
          >
            <FileText size={17} />
            <span>Skills</span>
          </button>
          <div className="nav-group">
            <button
              className={activeSurface === "domain" ? "domain-button active" : "domain-button"}
              onClick={() => setDomainsNavOpen((open) => !open)}
              type="button"
            >
              <Bot size={17} />
              <span>Domains</span>
              {domainsNavOpen ? <ChevronDown className="nav-chevron" size={15} /> : <ChevronRight className="nav-chevron" size={15} />}
            </button>
            {domainsNavOpen && (
              <div className="nav-submenu">
                {domains.map((domain) => (
                  <button
                    key={domain}
                    className={
                      activeSurface === "domain" && activeDomain === domain
                        ? "domain-button active"
                        : "domain-button"
                    }
                    onClick={() => {
                      setActiveDomain(domain);
                      setActiveSurface("domain");
                    }}
                    type="button"
                  >
                    <ChevronRight size={16} />
                    <span>{domain}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
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
                ? "Memory Manager"
                : activeSurface === "issues"
                  ? "Product Issues"
                : activeSurface === "run-log"
                  ? "Run Log"
                : activeSurface === "workflows"
                  ? "Workflows"
                : activeSurface === "reports"
                  ? "Reports"
                : activeSurface === "tools"
                  ? "Tools"
                : activeSurface === "skills"
                  ? "Skills"
                  : activeSurface in routedSurfaceConfig
                    ? routedSurfaceConfig[activeSurface as RoutedObjectSurface].title
                  : activeDomain}
            </h2>
          </div>
          <div className="status-strip" aria-label="Runtime status">
            {activeSurface === "memory" ? (
              <span>
                <Database size={16} />
                Memory pipeline
              </span>
            ) : ["run-log", "workflows", "reports"].includes(activeSurface) ? (
              <span>
                <FileText size={16} />
                Workflow outputs
              </span>
            ) : activeSurface === "issues" ? (
              <span>
                <GitPullRequest size={16} />
                Product intelligence
              </span>
            ) : activeSurface in routedSurfaceConfig ? (
              <span>
                <Database size={16} />
                Routed store
              </span>
            ) : activeSurface === "tools" ? (
              <span>
                <Wrench size={16} />
                Shared tool suite
              </span>
            ) : activeSurface === "skills" ? (
              <span>
                <FileText size={16} />
                Skill library
              </span>
            ) : (
              <span>
                <Clock3 size={16} />
                {activeWorkflowCount === 0
                  ? "No active workflows"
                  : `${activeWorkflowCount} active workflow${activeWorkflowCount === 1 ? "" : "s"}`}
              </span>
            )}
          </div>
        </header>

        {activeSurface === "memory" ? (
          <MemoryWorkspace />
        ) : activeSurface === "issues" ? (
          <IssuesWorkspace />
        ) : activeSurface === "run-log" ? (
          <RunLogWorkspace
            entries={workflowRunLog}
            statusMessage={workflowOutputsStatus}
            onRefresh={loadWorkflowOutputs}
            onOpenReport={openWorkflowReport}
          />
        ) : activeSurface === "reports" ? (
          <ReportsWorkspace
            reports={workflowReports}
            selectedReport={selectedWorkflowReport}
            statusMessage={workflowOutputsStatus}
            onRefresh={loadWorkflowOutputs}
            onOpenReport={openWorkflowReport}
            onArchiveReport={archiveWorkflowReport}
            onArchiveAllReports={archiveAllWorkflowReports}
          />
        ) : activeSurface === "workflows" ? (
          <WorkflowsWorkspace
            schedulerDashboard={schedulerDashboard}
            schedulerWorkerStatus={schedulerWorkerStatus}
            gmailTriggerStatus={gmailTriggerStatus}
            calendarTriggerStatus={calendarTriggerStatus}
            workflowTemplates={workflowTemplates}
            selectedSchedulerRun={selectedSchedulerRun}
            selectedSchedulerDefinition={selectedSchedulerDefinition}
            selectedDefinitionRuns={selectedDefinitionRuns}
            schedulerStatusMessage={schedulerStatusMessage}
            busyToolCallId={busyToolCallId}
            onRefresh={async () => {
              await loadSchedulerDashboard();
              await loadGmailTriggerStatus();
              await loadCalendarTriggerStatus();
              await loadWorkflowOutputs();
            }}
            onSelectRun={selectSchedulerRun}
            onArchiveRun={archiveSchedulerRun}
            onReplayRun={replaySchedulerRun}
            onReenterRun={reenterSchedulerRunSession}
            onSelectDefinition={selectSchedulerDefinition}
            onApproveToolCall={approveToolCall}
            onRejectToolCall={rejectToolCall}
            onToggleDefinitionGmailWatch={updateDefinitionGmailWatch}
            onToggleDefinitionCalendarWatch={updateDefinitionCalendarWatch}
            onToggleDefinitionShadowMode={updateDefinitionShadowMode}
            onToggleSchedulerWorker={(enabled) => updateSchedulerWorkerStatus({ enabled })}
            onPollGmailTriggers={pollGmailTriggers}
            onPollCalendarTriggers={pollCalendarTriggers}
            onResetGmailDomain={resetGmailTriggerDomain}
            onResetCalendarDomain={resetCalendarTriggerDomain}
            onInstallTemplate={installWorkflowTemplate}
            onToggleDefinition={updateWorkflowDefinitionActivation}
          />
        ) : activeSurface in routedSurfaceConfig ? (
          <RoutedObjectsWorkspace surface={activeSurface as RoutedObjectSurface} />
        ) : activeSurface === "tools" ? (
          <ToolsWorkspace />
        ) : activeSurface === "skills" ? (
          <SkillsWorkspace />
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

              <div className="thread" ref={chatThreadRef}>
                {chatMessages.length > 0 ? (
                  chatMessages.map((message) => (
                    <div
                      className={`message ${
                        message.sender === "user" ? "user-message" : "maestro-message"
                      }`}
                      key={message.id}
                    >
                      <span>{message.sender === "user" ? "You" : "Maestro"}</span>
                      {message.sender === "maestro" ? (
                        <MarkdownMessage content={message.content} />
                      ) : (
                        <p className="plain-message">{message.content}</p>
                      )}
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
                      {conductingMessage}
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
                  placeholder={
                    maestroInteractionMode === "knowledge"
                      ? "Ask Maestro about your world or update it..."
                      : "Describe the workflow outcome, schedule, and constraints..."
                  }
                  aria-label="Message Maestro"
                  rows={1}
                />
                <button
                  className="composer-send-button"
                  type="submit"
                  aria-busy={maestroBusy}
                  disabled={maestroBusy || !draftMessage.trim()}
                >
                  Send
                </button>
              </form>

              <div className="maestro-status-row">
                <div className="maestro-mode-control" role="group" aria-label="Maestro interaction mode">
                  <button
                    type="button"
                    className={maestroInteractionMode === "knowledge" ? "active" : ""}
                    aria-pressed={maestroInteractionMode === "knowledge"}
                    onClick={() => {
                      setMaestroInteractionMode("knowledge");
                      setMaestroStatus("Knowledge mode: reason, retrieve, and update your world.");
                    }}
                  >
                    <Database size={15} />
                    Knowledge
                  </button>
                  <button
                    type="button"
                    className={maestroInteractionMode === "workflow_builder" ? "active" : ""}
                    aria-pressed={maestroInteractionMode === "workflow_builder"}
                    onClick={() => {
                      setMaestroInteractionMode("workflow_builder");
                      setMaestroStatus("Build workflow mode: plan and delegate agent work.");
                    }}
                  >
                    <Sparkles size={15} />
                    Build workflow
                  </button>
                </div>
                <span>
                  {conductingMessage}
                </span>
              </div>

              {showPlanPreview && maestroPlan && (
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
                      {planScheduleCandidate ? "Save schedule" : "Run plan"}
                    </button>
                    <button
                      className="danger-action"
                      onClick={clearMaestroPlan}
                      disabled={maestroBusy}
                    >
                      <Trash2 size={16} />
                      Clear candidate
                    </button>
                  </div>
                  <p>{maestroPlan.summary}</p>
                  {planScheduleCandidate && (
                    <p className="evaluation-note">
                      This will save a scheduled workflow under Workflows instead of executing the agents now.
                    </p>
                  )}
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
                    {planScheduleCandidate && (
                      <span>
                        {triggerSummary(
                          String(planScheduleCandidate.trigger_type ?? "recurring"),
                          (planScheduleCandidate.trigger_config as Record<string, unknown>) ?? {},
                        )}
                      </span>
                    )}
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
                        {selectedWorkflowSubtask?.model_tier && (
                          <span>{selectedWorkflowSubtask.model_tier}</span>
                        )}
                        {selectedWorkflowSubtask?.model_profile && (
                          <span>model {selectedWorkflowSubtask.model_profile}</span>
                        )}
                      </div>
                      {selectedWorkflowItem.error_message && (
                        <p className="evaluation-note">{selectedWorkflowItem.error_message}</p>
                      )}
                      {selectedWorkflowSubtask?.rationale && (
                        <p className="evaluation-note">{selectedWorkflowSubtask.rationale}</p>
                      )}
                      {selectedWorkflowSubtask?.model_rationale && (
                        <p className="evaluation-note">{selectedWorkflowSubtask.model_rationale}</p>
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
                              {item.needs_agent && <span>{item.model_tier}</span>}
                              {item.needs_agent && <span>model {item.model_profile}</span>}
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

              {showInlineRunPreview && maestroRun && (
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

            <section className="planner-panel" aria-labelledby="review-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Renderer</p>
                  <h3 id="review-heading">Artifacts & reports</h3>
                </div>
                {selectedWorkflowReport || maestroRun ? (
                  <button
                    className="icon-button"
                    onClick={() => {
                      setSelectedWorkflowReport(null);
                      setMaestroRun(null);
                    }}
                    title="Close renderer"
                    type="button"
                  >
                    <X size={18} />
                  </button>
                ) : (
                  <FileText size={18} />
                )}
              </div>

              {selectedWorkflowReport ? (
                <div className="artifact-review-pane">
                  <div className="preview-meta">
                    <span>{selectedWorkflowReport.source_type}</span>
                    <span>
                      {selectedWorkflowReport.domain_key
                        ? domainLabels[selectedWorkflowReport.domain_key] ?? selectedWorkflowReport.domain_key
                        : "Global"}
                    </span>
                    <span>{formatDateTime(selectedWorkflowReport.created_at)}</span>
                  </div>
                  <article className="artifact-review-card">
                    <h4>{selectedWorkflowReport.title}</h4>
                    <MarkdownMessage
                      content={selectedWorkflowReport.body_markdown ?? selectedWorkflowReport.summary ?? ""}
                    />
                  </article>
                </div>
              ) : maestroRun ? (
                <div className="artifact-review-pane">
                  <div className="preview-meta">
                    <span>{maestroRun.status}</span>
                    <span>{maestroRun.child_runs.length} child runs</span>
                    {maestroRun.synthesis_report_id && <span>report {maestroRun.synthesis_report_id.slice(0, 8)}</span>}
                    {maestroRun.artifact_id && <span>artifact {maestroRun.artifact_id.slice(0, 8)}</span>}
                  </div>
                  {maestroRun.chat_summary && (
                    <article className="artifact-review-card">
                      <h4>Chat summary</h4>
                      <p>{maestroRun.chat_summary}</p>
                    </article>
                  )}
                  {maestroRun.staged_artifact_path && (
                    <article className="artifact-review-card">
                      <h4>Staged artifact</h4>
                      <p>{maestroRun.staged_artifact_path}</p>
                    </article>
                  )}
                  <details className="artifact-review-card">
                    <summary>Workflow synthesis</summary>
                    <pre>{maestroRun.synthesis}</pre>
                  </details>
                </div>
              ) : (
                <div className="empty-planner-state">
                  <FileText size={20} />
                  <p>
                    Ask Maestro to show a report, inspect a completed run, or open an artifact and
                    it will render here beside the conversation.
                  </p>
                </div>
              )}
              <NeedsAttentionPanel
                schedulerDashboard={schedulerDashboard}
                pendingToolApprovals={pendingToolApprovals}
                busyToolCallId={busyToolCallId}
                onApproveToolCall={approveToolCall}
                onRejectToolCall={rejectToolCall}
                onSubmitAttentionResponse={submitAttentionResponse}
                onArchiveRun={archiveSchedulerRun}
              />
            </section>
          </div>
        )}
      </section>
    </main>
  );
}

function RunLogWorkspace({
  entries,
  statusMessage,
  onRefresh,
  onOpenReport,
}: {
  entries: WorkflowRunLogEntry[];
  statusMessage: string;
  onRefresh: () => Promise<void>;
  onOpenReport: (reportId: string) => Promise<void>;
}) {
  return (
    <section className="surface-panel output-surface" aria-labelledby="run-log-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Workflow history</p>
          <h3 id="run-log-heading">Run Log</h3>
        </div>
        <button className="icon-button" onClick={onRefresh} title="Refresh run log" type="button">
          <RefreshCw size={18} />
        </button>
      </div>
      <div className="output-card-grid">
        {entries.map((entry) => (
          <article className="workflow-summary-card" key={entry.id}>
            <span>{entry.status}</span>
            <h4>{entry.title}</h4>
            <p>{entry.summary}</p>
            <div className="preview-meta">
              <span>{entry.domain_key ? domainLabels[entry.domain_key] ?? entry.domain_key : "Global"}</span>
              <span>{entry.agent_work.length} agent item(s)</span>
              <span>{entry.report_ids.length} report(s)</span>
              <span>{entry.artifact_ids.length} artifact(s)</span>
              {entry.run_completed_at && <span>{formatDateTime(entry.run_completed_at)}</span>}
            </div>
            {entry.report_ids.length > 0 && (
              <div className="scheduler-action-row compact-actions">
                {entry.report_ids.slice(0, 3).map((reportId) => (
                  <button key={reportId} type="button" onClick={() => onOpenReport(reportId)}>
                    Open report {reportId.slice(0, 8)}
                  </button>
                ))}
              </div>
            )}
            <details>
              <summary>Agent work</summary>
              <div className="workflow-detail-grid">
                {entry.agent_work.map((item, index) => (
                  <article className="mini-row" key={`${entry.id}-agent-${index}`}>
                    <span>
                      {String(item.status ?? "unknown")} / {String(item.agent_name ?? item.agent_key ?? "agent")}
                    </span>
                    <p>{String(item.objective ?? "")}</p>
                  </article>
                ))}
              </div>
            </details>
          </article>
        ))}
        {entries.length === 0 && (
          <p className="empty-state">Completed workflow runs will appear here after the worker finishes them.</p>
        )}
      </div>
      {statusMessage && <p className="memory-status">{statusMessage}</p>}
    </section>
  );
}

function ReportsWorkspace({
  reports,
  selectedReport,
  statusMessage,
  onRefresh,
  onOpenReport,
  onArchiveReport,
  onArchiveAllReports,
}: {
  reports: WorkflowReport[];
  selectedReport: WorkflowReport | null;
  statusMessage: string;
  onRefresh: () => Promise<void>;
  onOpenReport: (reportId: string) => Promise<void>;
  onArchiveReport: (reportId: string) => Promise<void>;
  onArchiveAllReports: () => Promise<void>;
}) {
  return (
    <section className="reports-workspace" aria-labelledby="reports-workspace-heading">
      <aside className="reports-list-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Report box</p>
            <h3 id="reports-workspace-heading">Reports</h3>
          </div>
          <div className="inline-actions">
            <button className="icon-button" onClick={onRefresh} title="Refresh reports" type="button">
              <RefreshCw size={18} />
            </button>
            <button className="icon-button" onClick={onArchiveAllReports} title="Archive all reports" type="button">
              <Archive size={18} />
            </button>
          </div>
        </div>
        {reports.map((report) => (
          <article
            className={`report-list-item ${selectedReport?.id === report.id ? "active" : ""}`}
            key={report.id}
          >
            <button className="card-reset" onClick={() => onOpenReport(report.id)} type="button">
              <span>{report.domain_key ? domainLabels[report.domain_key] ?? report.domain_key : "Global"}</span>
              <strong>{report.title}</strong>
              {report.summary && <small>{report.summary}</small>}
            </button>
            <button
              className="session-archive-button"
              onClick={() => onArchiveReport(report.id)}
              type="button"
            >
              Archive
            </button>
          </article>
        ))}
        {reports.length === 0 && <p className="empty-state">No reports have been generated yet.</p>}
        {statusMessage && <p className="memory-status">{statusMessage}</p>}
      </aside>
      <section className="report-renderer-panel" aria-label="Selected report">
        {selectedReport ? (
          <>
            <div className="section-heading">
              <div>
                <p className="eyebrow">{selectedReport.source_type}</p>
                <h3>{selectedReport.title}</h3>
              </div>
              <FileText size={18} />
            </div>
            <button
              className="session-archive-button"
              onClick={() => onArchiveReport(selectedReport.id)}
              type="button"
            >
              Archive report
            </button>
            <div className="preview-meta">
              <span>{selectedReport.domain_key ? domainLabels[selectedReport.domain_key] ?? selectedReport.domain_key : "Global"}</span>
              <span>{formatDateTime(selectedReport.created_at)}</span>
            </div>
            <div className="report-markdown">
              <MarkdownMessage content={selectedReport.body_markdown ?? selectedReport.summary ?? ""} />
            </div>
          </>
        ) : (
          <div className="empty-planner-state">
            <FileText size={20} />
            <p>Select a report to render it here.</p>
          </div>
        )}
      </section>
    </section>
  );
}

function WorkflowsWorkspace({
  schedulerDashboard,
  schedulerWorkerStatus,
  gmailTriggerStatus,
  calendarTriggerStatus,
  workflowTemplates,
  selectedSchedulerRun,
  selectedSchedulerDefinition,
  selectedDefinitionRuns,
  schedulerStatusMessage,
  busyToolCallId,
  onRefresh,
  onSelectRun,
  onArchiveRun,
  onReplayRun,
  onReenterRun,
  onSelectDefinition,
  onApproveToolCall,
  onRejectToolCall,
  onToggleDefinitionGmailWatch,
  onToggleDefinitionCalendarWatch,
  onToggleDefinitionShadowMode,
  onToggleSchedulerWorker,
  onPollGmailTriggers,
  onPollCalendarTriggers,
  onResetGmailDomain,
  onResetCalendarDomain,
  onInstallTemplate,
  onToggleDefinition,
}: {
  schedulerDashboard: SchedulerDashboard | null;
  schedulerWorkerStatus: SchedulerWorkerStatus | null;
  gmailTriggerStatus: GmailTriggerStatus | null;
  calendarTriggerStatus: CalendarTriggerStatus | null;
  workflowTemplates: WorkflowTemplate[];
  selectedSchedulerRun: SchedulerRun | null;
  selectedSchedulerDefinition: SchedulerDefinition | null;
  selectedDefinitionRuns: SchedulerRun[];
  schedulerStatusMessage: string;
  busyToolCallId: string | null;
  onRefresh: () => Promise<void>;
  onSelectRun: (runId: string) => Promise<void>;
  onArchiveRun: (runId: string) => Promise<void>;
  onReplayRun: (runId: string) => Promise<void>;
  onReenterRun: (run: SchedulerRun) => Promise<void>;
  onSelectDefinition: (definition: SchedulerDefinition) => Promise<void>;
  onApproveToolCall: (toolCallId: string) => Promise<void>;
  onRejectToolCall: (toolCallId: string) => Promise<void>;
  onToggleDefinitionGmailWatch: (definitionId: string, enabled: boolean) => Promise<void>;
  onToggleDefinitionCalendarWatch: (definitionId: string, enabled: boolean) => Promise<void>;
  onToggleDefinitionShadowMode: (definitionId: string, enabled: boolean) => Promise<void>;
  onToggleSchedulerWorker: (enabled: boolean) => Promise<void>;
  onPollGmailTriggers: () => Promise<void>;
  onPollCalendarTriggers: () => Promise<void>;
  onResetGmailDomain: (domainKey: string) => Promise<void>;
  onResetCalendarDomain: (domainKey: string) => Promise<void>;
  onInstallTemplate: (templateKey: string) => Promise<void>;
  onToggleDefinition: (definitionId: string, isActive: boolean) => Promise<void>;
}) {
  const [selectedQueueItemId, setSelectedQueueItemId] = useState<string | null>(null);
  const runs = schedulerDashboard?.runs ?? [];
  const definitions = schedulerDashboard?.definitions ?? [];
  const scheduledDefinitions = definitions.filter((definition) =>
    ["scheduled", "recurring"].includes(definition.trigger_type),
  );
  const triggerDefinitions = definitions.filter((definition) => definition.trigger_type === "event");
  const selectedToolActivity = Array.isArray(selectedSchedulerRun?.output_payload?.tool_activity)
    ? selectedSchedulerRun.output_payload.tool_activity as MaestroRun["tool_activity"]
    : [];
  const selectedRunStages = useMemo(() => {
    const groups = new Map<number, SchedulerQueueItem[]>();
    for (const item of selectedSchedulerRun?.queue_items ?? []) {
      const stage = item.stage_index ?? 0;
      groups.set(stage, [...(groups.get(stage) ?? []), item]);
    }
    return Array.from(groups.entries())
      .sort(([first], [second]) => first - second)
      .map(([stageIndex, items]) => ({
        stageIndex,
        items: [...items].sort((first, second) => first.position - second.position),
      }));
  }, [selectedSchedulerRun]);
  const selectedQueueItem =
    selectedSchedulerRun?.queue_items.find((item) => item.id === selectedQueueItemId) ??
    selectedSchedulerRun?.queue_items[0] ??
    null;
  const selectedAgentRun = selectedQueueItem ? queueItemAgentRun(selectedQueueItem) : null;
  const selectedToolCalls = selectedQueueItem ? queueItemToolCalls(selectedQueueItem) : [];
  return (
    <section className="surface-panel output-surface" aria-labelledby="workflows-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Execution control</p>
          <h3 id="workflows-heading">Workflows</h3>
        </div>
        <div className="scheduler-action-row compact-actions">
          <label className="worker-toggle">
            Auto worker
            <input
              type="checkbox"
              checked={schedulerWorkerStatus?.enabled ?? false}
              onChange={(event) => onToggleSchedulerWorker(event.target.checked)}
            />
          </label>
          <button className="icon-button" onClick={onRefresh} title="Refresh workflows" type="button">
            <RefreshCw size={18} />
          </button>
        </div>
      </div>
      <div className="output-card-grid">
        <section>
          <div className="section-title-row">
            <h4>Active</h4>
            <span className="count-badge">{runs.length}</span>
          </div>
          {runs.map((run) => {
            const completed = run.queue_items.filter((item) => item.status === "completed").length;
            const blocked = run.queue_items.filter((item) => item.status === "blocked").length;
            return (
              <article className="workflow-summary-card compact-run-card" key={run.id}>
                <button type="button" className="card-reset" onClick={() => onSelectRun(run.id)}>
                  <span>{run.status}</span>
                  <h4>{run.summary || "Maestro workflow"}</h4>
                </button>
                <div className="preview-meta">
                  <span>{run.priority}</span>
                  <span>{run.queue_items.length} queue items</span>
                  <span>{completed} complete</span>
                  {blocked > 0 && <span>{blocked} blocked</span>}
                </div>
                <div className="scheduler-action-row compact-actions">
                  <button type="button" onClick={() => onSelectRun(run.id)}>
                    Inspect
                  </button>
                  {run.conversation_id && (
                    <button type="button" onClick={() => onReenterRun(run)}>
                      Re-enter chat
                    </button>
                  )}
                  <button type="button" onClick={() => onArchiveRun(run.id)}>
                    Kill workflow
                  </button>
                  {run.workflow_definition_id && (
                    <button type="button" onClick={() => onReplayRun(run.id)}>
                      Replay
                    </button>
                  )}
                </div>
              </article>
            );
          })}
          {runs.length === 0 && <p className="empty-state">No active workflow runs are queued.</p>}
        </section>
        <section>
          <div className="section-title-row">
            <h4>Scheduled</h4>
            <span className="count-badge">{scheduledDefinitions.length}</span>
          </div>
          {scheduledDefinitions.map((definition) => (
            <article className="workflow-summary-card compact-run-card" key={definition.id}>
              <button type="button" className="card-reset" onClick={() => onSelectDefinition(definition)}>
                <span>{definition.trigger_type}</span>
                <h4>{definition.name}</h4>
              </button>
              <div className="preview-meta">
                <span>{definition.is_active ? "active" : "paused"}</span>
                <span>{triggerSummary(definition.trigger_type, definition.trigger_config)}</span>
                <span>{definition.fairness_group || definition.domain_key || "global"}</span>
              </div>
            </article>
          ))}
          {scheduledDefinitions.length === 0 && <p className="empty-state">No scheduled workflows yet.</p>}
        </section>
        <section>
          <div className="section-title-row">
            <h4>Durable triggers</h4>
            <span className="count-badge">{triggerDefinitions.length}</span>
          </div>
          {workflowTemplates.filter((template) => !template.installed).map((template) => (
            <article className="workflow-summary-card compact-run-card" key={template.key}>
              <span>available template</span>
              <h4>{template.name}</h4>
              <p>{template.description}</p>
              <div className="preview-meta">
                <span>{template.readiness.ready ? "ready to install" : "setup needed"}</span>
                {!template.readiness.ready && (
                  <span>{template.readiness.missing.join("; ")}</span>
                )}
              </div>
              <div className="scheduler-action-row compact-actions">
                <button type="button" onClick={() => onInstallTemplate(template.key)}>
                  Install paused
                </button>
              </div>
            </article>
          ))}
          {triggerDefinitions.map((definition) => {
            const template = workflowTemplates.find(
              (item) => item.definition_id === definition.id,
            );
            const isGmailTrigger =
              definition.trigger_config.event_type === "gmail.message.received";
            const isCalendarTrigger =
              definition.trigger_config.event_type === "google.calendar.event.changed";
            const configuredWatch = definition.trigger_config.gmail_watch_enabled;
            const gmailWatchEnabled =
              configuredWatch === true
              || (configuredWatch === undefined && definition.key !== "praxis-email-triage");
            const shadowMode = definition.workflow_spec?.shadow_mode === true;
            const gmailDomainStatus = gmailTriggerStatus?.domains.find(
              (domain) => domain.domain_key === definition.domain_key,
            );
            const calendarWatchEnabled = definition.trigger_config.calendar_watch_enabled === true;
            const calendarDomainStatus = calendarTriggerStatus?.domains.find(
              (domain) => domain.domain_key === definition.domain_key,
            );
            const triggerDomainStatus = isCalendarTrigger ? calendarDomainStatus : gmailDomainStatus;
            return (
              <article
                className="workflow-summary-card compact-run-card durable-workflow-card"
                key={definition.id}
              >
                <button type="button" className="card-reset" onClick={() => onSelectDefinition(definition)}>
                  <span>Durable {definition.trigger_type} workflow</span>
                  <h4>{definition.name}</h4>
                </button>
                <div className="preview-meta">
                  <span>{definition.is_active ? "active" : "paused"}</span>
                  <span>{triggerSummary(definition.trigger_type, definition.trigger_config)}</span>
                  <span>{definition.fairness_group || definition.domain_key || "global"}</span>
                  {template && (
                    <span>{template.readiness.ready ? "prerequisites ready" : template.readiness.missing.join("; ")}</span>
                  )}
                  {triggerDomainStatus?.last_polled_at && (
                    <span>polled {formatDateTime(triggerDomainStatus.last_polled_at)}</span>
                  )}
                  {triggerDomainStatus?.status === "error" && triggerDomainStatus.last_error && (
                    <span className="warning-pill">Last poll failed</span>
                  )}
                </div>
                {triggerDomainStatus?.status === "error" && triggerDomainStatus.last_error && (
                  <details className="inline-error-details">
                    <summary>{isCalendarTrigger ? "Calendar" : "Gmail"} watcher diagnostic</summary>
                    <p>{triggerDomainStatus.last_error}</p>
                  </details>
                )}
                <div className="scheduler-action-row compact-actions">
                  <label className="worker-toggle">
                    Workflow active
                    <input
                      type="checkbox"
                      checked={definition.is_active}
                      disabled={!definition.is_active && template?.readiness.ready === false}
                      onChange={(event) => onToggleDefinition(definition.id, event.target.checked)}
                    />
                  </label>
                  {isGmailTrigger && (
                    <label className="worker-toggle">
                      Gmail watch
                      <input
                        type="checkbox"
                        checked={gmailWatchEnabled}
                        disabled={!definition.is_active}
                        onChange={(event) => onToggleDefinitionGmailWatch(
                          definition.id,
                          event.target.checked,
                        )}
                      />
                    </label>
                  )}
                  {isCalendarTrigger && (
                    <label className="worker-toggle">
                      Calendar watch
                      <input
                        type="checkbox"
                        checked={calendarWatchEnabled}
                        disabled={!definition.is_active}
                        onChange={(event) => onToggleDefinitionCalendarWatch(
                          definition.id,
                          event.target.checked,
                        )}
                      />
                    </label>
                  )}
                  {(isGmailTrigger || isCalendarTrigger) && (
                    <label className="worker-toggle">
                      Shadow mode
                      <input
                        type="checkbox"
                        checked={shadowMode}
                        onChange={(event) => onToggleDefinitionShadowMode(
                          definition.id,
                          event.target.checked,
                        )}
                      />
                    </label>
                  )}
                  {isGmailTrigger && gmailWatchEnabled && (
                    <button
                      className="icon-button"
                      onClick={onPollGmailTriggers}
                      title="Poll this Gmail account now"
                      type="button"
                    >
                      <Inbox size={18} />
                    </button>
                  )}
                  {isGmailTrigger && gmailDomainStatus && (
                    <button type="button" onClick={() => onResetGmailDomain(gmailDomainStatus.domain_key)}>
                      Reset account cursor
                    </button>
                  )}
                  {isCalendarTrigger && calendarWatchEnabled && (
                    <button
                      className="icon-button"
                      onClick={onPollCalendarTriggers}
                      title="Poll this Google Calendar now"
                      type="button"
                    >
                      <CalendarDays size={18} />
                    </button>
                  )}
                  {isCalendarTrigger && calendarDomainStatus && (
                    <button type="button" onClick={() => onResetCalendarDomain(calendarDomainStatus.domain_key)}>
                      Reset calendar cursor
                    </button>
                  )}
                </div>
              </article>
            );
          })}
          {triggerDefinitions.length === 0
            && workflowTemplates.every((template) => template.installed) && (
              <p className="empty-state">No trigger workflows yet.</p>
            )}
        </section>
      </div>
      {selectedSchedulerRun && (
        <section className="workflow-detail-panel scheduler-detail-panel">
          <div className="workflow-detail-heading">
            <div>
              <span>{selectedSchedulerRun.status}</span>
              <h4>{selectedSchedulerRun.summary || "Workflow run"}</h4>
            </div>
            <div className="scheduler-action-row compact-actions">
              {selectedSchedulerRun.conversation_id && (
                <button type="button" onClick={() => onReenterRun(selectedSchedulerRun)}>
                  Re-enter chat
                </button>
              )}
              <button className="danger-action" type="button" onClick={() => onArchiveRun(selectedSchedulerRun.id)}>
                Kill workflow
              </button>
            </div>
          </div>
          <div className="preview-meta">
            <span>{selectedSchedulerRun.priority}</span>
            <span>{selectedSchedulerRun.queue_items.length} queue items</span>
            {selectedSchedulerRun.created_at && <span>{formatDateTime(selectedSchedulerRun.created_at)}</span>}
            {selectedSchedulerRun.error_message && <span>{selectedSchedulerRun.error_message}</span>}
          </div>
          {selectedRunStages.length > 0 && (
            <div className="workflow-map" aria-label="Selected workflow dependency map">
              <h4>Workflow map</h4>
              <div className="workflow-map-scroll">
                {selectedRunStages.map((stage, index) => (
                  <div className="workflow-map-stage" key={`selected-stage-${stage.stageIndex}`}>
                    <div className="workflow-map-heading">
                      <span>Stage {stage.stageIndex}</span>
                      <span>{stage.items.length > 1 ? "Parallel" : "Single"}</span>
                    </div>
                    <div className="workflow-map-items">
                      {stage.items.map((item) => (
                        <button
                          className={`workflow-node node-${item.status} ${
                            selectedQueueItem?.id === item.id ? "node-selected" : ""
                          }`}
                          key={item.id}
                          type="button"
                          onClick={() => setSelectedQueueItemId(item.id)}
                        >
                          <span>{item.status}</span>
                          <strong>{item.agent_name ?? item.agent_key ?? "Unassigned"}</strong>
                          <small>{item.external_key}</small>
                        </button>
                      ))}
                    </div>
                    {index < selectedRunStages.length - 1 && (
                      <ChevronRight className="workflow-arrow" size={18} />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
          {selectedQueueItem && (
            <div className="workflow-detail-panel nested-workflow-detail">
              <div className="workflow-detail-heading">
                <div>
                  <span>
                    Stage {selectedQueueItem.stage_index} / {selectedQueueItem.status} /{" "}
                    {domainLabels[selectedQueueItem.domain_key ?? "global"] ??
                      selectedQueueItem.domain_key ??
                      "Global"}
                  </span>
                  <h4>{selectedQueueItem.agent_name ?? selectedQueueItem.agent_key ?? "Unassigned"}</h4>
                </div>
              </div>
              <p>{selectedQueueItem.objective}</p>
              <div className="preview-meta">
                <span>{selectedQueueItem.priority}</span>
                <span>{selectedQueueItem.external_key}</span>
                {selectedQueueItem.dependency_keys.length > 0 && (
                  <span>Waits for {selectedQueueItem.dependency_keys.join(", ")}</span>
                )}
                {selectedQueueItem.model_profile && <span>model {selectedQueueItem.model_profile}</span>}
                {(selectedQueueItem.required_skills ?? []).length > 0 && (
                  <span>skills {(selectedQueueItem.required_skills ?? []).join(", ")}</span>
                )}
                {selectedQueueItem.lease_owner && <span>lease {selectedQueueItem.lease_owner}</span>}
                {selectedQueueItem.error_message && <span>{selectedQueueItem.error_message}</span>}
              </div>
              {selectedAgentRun && (
                <div className="artifact-review-card">
                  <h4>Agent run</h4>
                  <div className="preview-meta">
                    <span>{selectedAgentRun.status}</span>
                    {selectedAgentRun.report_id && <span>report {selectedAgentRun.report_id.slice(0, 8)}</span>}
                    {selectedAgentRun.artifact_id && <span>artifact {selectedAgentRun.artifact_id.slice(0, 8)}</span>}
                    {selectedAgentRun.staged_artifact_path && <span>staged</span>}
                  </div>
                  {selectedAgentRun.execution_note && <p>{selectedAgentRun.execution_note}</p>}
                  {selectedAgentRun.output_preview && (
                    <details>
                      <summary>Output preview</summary>
                      <pre>{selectedAgentRun.output_preview}</pre>
                    </details>
                  )}
                  {selectedAgentRun.error_message && (
                    <p className="evaluation-note">{selectedAgentRun.error_message}</p>
                  )}
                  {selectedAgentRun.email_triage_decision && (
                    <div className="triage-decision-card">
                      <h4>Email triage decision</h4>
                      <div className="preview-meta">
                        {selectedAgentRun.email_triage_shadow_mode !== null
                          && selectedAgentRun.email_triage_shadow_mode !== undefined && (
                          <span>{selectedAgentRun.email_triage_shadow_mode ? "shadow run" : "live run"}</span>
                        )}
                        <span>{selectedAgentRun.email_triage_decision.classification}</span>
                        <span>{Math.round(selectedAgentRun.email_triage_decision.confidence * 100)}% confidence</span>
                        <span>{selectedAgentRun.email_triage_decision.requires_chris_response ? "Chris response required" : "No Chris response"}</span>
                        <span>{selectedAgentRun.email_triage_decision.notification.should_notify ? "Notification" : "No notification"}</span>
                        <span>{selectedAgentRun.email_triage_decision.read_state_action.replace("_", " ")}</span>
                      </div>
                      <strong>{selectedAgentRun.email_triage_decision.subject}</strong>
                      <p>{selectedAgentRun.email_triage_decision.summary}</p>
                      <div className="preview-meta">
                        <span>{selectedAgentRun.email_triage_decision.routed_candidates.length} routed candidates</span>
                        {selectedAgentRun.email_triage_decision.routed_candidates.map((candidate, index) => (
                          <span key={`${candidate.route_type}-${candidate.title}-${index}`}>
                            {candidate.route_type}: {candidate.title}
                          </span>
                        ))}
                      </div>
                      <details>
                        <summary>Decision rationale</summary>
                        <p>From {selectedAgentRun.email_triage_decision.sender}</p>
                        <p>{selectedAgentRun.email_triage_decision.rationale}</p>
                        {selectedAgentRun.email_triage_decision.notification.should_notify && (
                          <p>
                            Notification: {selectedAgentRun.email_triage_decision.notification.message}
                          </p>
                        )}
                        {selectedAgentRun.email_triage_decision.routed_candidates.map((candidate, index) => (
                          <p key={`decision-${candidate.route_type}-${candidate.title}-${index}`}>
                            {candidate.title}: {candidate.content} ({candidate.rationale})
                          </p>
                        ))}
                      </details>
                      {selectedAgentRun.email_triage_decision.linked_documents.length > 0 && (
                        <details>
                          <summary>Linked documents</summary>
                          {selectedAgentRun.email_triage_decision.linked_documents.map((document, index) => (
                            <p key={`${document.url}-${index}`}>
                              {document.title || document.kind}: {document.access_status}. {document.summary}
                            </p>
                          ))}
                        </details>
                      )}
                    </div>
                  )}
                </div>
              )}
              {selectedToolCalls.length > 0 && (
                <div className="tool-activity-list">
                  <h4>Agent tool calls</h4>
                  {selectedToolCalls.map((call, index) => (
                    <article
                      className={`tool-activity-item tool-activity-${call.status}`}
                      key={`${call.tool_name}-${call.id ?? index}`}
                    >
                      <strong>{call.tool_name}</strong>
                      <span>{call.status}</span>
                      {call.error_message && <p>{call.error_message}</p>}
                      {call.output_payload && (
                        <details>
                          <summary>Tool output</summary>
                          <pre>{JSON.stringify(call.output_payload, null, 2)}</pre>
                        </details>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          )}
          <div className="workflow-detail-grid">
            {selectedSchedulerRun.queue_items.map((item) => (
              <article className="mini-row" key={item.id}>
                <button className="card-reset" type="button" onClick={() => setSelectedQueueItemId(item.id)}>
                  <span>
                    Stage {item.stage_index} / {item.status} /{" "}
                    {domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"}
                  </span>
                  <p>{item.objective}</p>
                </button>
                <div className="preview-meta">
                  <span>{item.agent_name ?? item.agent_key ?? "Unassigned"}</span>
                  {item.dependency_keys.length > 0 && <span>Waits for {item.dependency_keys.join(", ")}</span>}
                </div>
              </article>
            ))}
          </div>
          {selectedToolActivity.length > 0 && (
            <div className="tool-activity-list">
              <h4>Tool activity</h4>
              {selectedToolActivity.map((activity, index) => (
                <article
                  className={`tool-activity-item tool-activity-${activity.status}`}
                  key={`${activity.agent_key}-${activity.tool_name}-${activity.tool_call_id ?? index}`}
                >
                  <strong>{activity.agent_name}</strong>
                  <span>{activity.tool_name}</span>
                  <p>
                    {activity.status}
                    {activity.details ? ` - ${activity.details}` : ""}
                    {activity.error_message ? ` - ${activity.error_message}` : ""}
                  </p>
                  {activity.status === "approval_required" && activity.tool_call_id && (
                    <div className="tool-approval-actions">
                      <button
                        className="planner-action"
                        onClick={() => onApproveToolCall(activity.tool_call_id!)}
                        disabled={busyToolCallId === activity.tool_call_id}
                        type="button"
                      >
                        Approve
                      </button>
                      <button
                        className="danger-action"
                        onClick={() => onRejectToolCall(activity.tool_call_id!)}
                        disabled={busyToolCallId === activity.tool_call_id}
                        type="button"
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>
      )}
      {selectedSchedulerDefinition && (
        <section className="workflow-detail-panel scheduler-detail-panel">
          <div className="workflow-detail-heading">
            <div>
              <span>Durable workflow</span>
              <h4>{selectedSchedulerDefinition.name}</h4>
            </div>
          </div>
          <div className="preview-meta">
            <span>{selectedSchedulerDefinition.is_active ? "active" : "paused"}</span>
            <span>{selectedSchedulerDefinition.workflow_spec?.shadow_mode === true ? "shadow mode" : "live mode"}</span>
            <span>{selectedDefinitionRuns.length} recent runs</span>
          </div>
          <div className="output-card-grid">
            {selectedDefinitionRuns.map((run) => (
              <article className="workflow-summary-card compact-run-card" key={run.id}>
                <span>{run.status}</span>
                <h4>{run.summary || "Email triage run"}</h4>
                <div className="preview-meta">
                  {run.created_at && <span>{formatDateTime(run.created_at)}</span>}
                  <span>{run.queue_items.length} queue items</span>
                </div>
                <button type="button" onClick={() => onSelectRun(run.id)}>
                  Inspect run
                </button>
              </article>
            ))}
            {selectedDefinitionRuns.length === 0 && (
              <p className="empty-state">No runs have been recorded for this workflow.</p>
            )}
          </div>
        </section>
      )}
      {schedulerStatusMessage && <p className="memory-status">{schedulerStatusMessage}</p>}
    </section>
  );
}

function DomainWorkspace({ domainLabel }: { domainLabel: string }) {
  const domainKey = domainKeysByLabel[domainLabel] ?? "maestro-development";
  const [domains, setDomains] = useState<DomainContext[]>([]);
  const [agents, setAgents] = useState<AgentSpec[]>([]);
  const [tools, setTools] = useState<ToolRegistryItem[]>([]);
  const [skills, setSkills] = useState<SkillRegistryItem[]>([]);
  const [globalContext, setGlobalContext] = useState("");
  const [domainContext, setDomainContext] = useState("");
  const [selectedAgentKey, setSelectedAgentKey] = useState<string | null>(null);
  const [newAgentName, setNewAgentName] = useState("");
  const [newAgentRole, setNewAgentRole] = useState("");
  const [roleSummary, setRoleSummary] = useState("");
  const [rolePrompt, setRolePrompt] = useState("");
  const [currentAction, setCurrentAction] = useState("");
  const [toolPermissions, setToolPermissions] = useState<Record<string, string>>({});
  const [skillPermissions, setSkillPermissions] = useState<Record<string, string>>({});
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
    const [globalResponse, domainResponse, agentResponse, toolResponse, skillResponse] = await Promise.all([
      apiJson<{ global_context: { context: string } }>("/agents/global-context"),
      apiJson<{ domains: DomainContext[] }>("/agents/domains"),
      apiJson<{ agents: AgentSpec[] }>("/agents"),
      apiJson<{ tools: ToolRegistryItem[] }>("/agents/tools"),
      apiJson<{ skills: SkillRegistryItem[] }>("/agents/skills"),
    ]);
    setGlobalContext(globalResponse.global_context.context);
    setDomains(domainResponse.domains);
    setAgents(agentResponse.agents);
    setTools(toolResponse.tools);
    setSkills(skillResponse.skills);
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
    setSkillPermissions(
      Object.fromEntries(selectedAgent.allowed_skills.map((skill) => [skill.key, "use"])),
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
          skill_permissions: Object.fromEntries(
            Object.keys(skillPermissions).map((key) => [
              key,
              {
                permission: "use",
                description: skills.find((skill) => skill.key === key)?.description ?? "",
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

  const toggleSkill = (skillKey: string, checked: boolean) => {
    setSkillPermissions((current) => {
      const next = { ...current };
      if (checked) next[skillKey] = next[skillKey] ?? "use";
      else delete next[skillKey];
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
            <label>
              Skill access
              <div className="tool-picker">
                {skills
                  .filter((skill) => !skill.domain_key || skill.domain_key === domainKey)
                  .map((skill) => (
                    <div className="tool-picker-row" key={skill.key}>
                      <label>
                        <input
                          type="checkbox"
                          checked={skill.key in skillPermissions}
                          onChange={(event) => toggleSkill(skill.key, event.target.checked)}
                        />
                        <span>{skill.name}</span>
                      </label>
                      <small>{skill.description || skill.instruction}</small>
                    </div>
                  ))}
                {skills.filter((skill) => !skill.domain_key || skill.domain_key === domainKey).length === 0 && (
                  <p className="empty-state">Create reusable skills from the Skills tab.</p>
                )}
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
    google: true,
  });
  const [connectionDomain, setConnectionDomain] = useState("praxis");
  const [connectionName, setConnectionName] = useState("Praxis memory retrieval");
  const [connectionAuthType, setConnectionAuthType] = useState("service");
  const [connectionConfig, setConnectionConfig] = useState("{}");
  const [statusMessage, setStatusMessage] = useState("Ready");

  const selectedTool = tools.find((tool) => tool.key === selectedToolKey) ?? tools[0] ?? null;
  const providerToolKeys = useMemo(
    () => new Set(tools.filter((tool) => !tool.key.includes(".")).map((tool) => tool.key)),
    [tools],
  );
  const selectedConnectionToolKey = selectedTool
    ? selectedTool.key.startsWith("gmail.")
      ? "google"
      : selectedTool.key.includes(".") && providerToolKeys.has(selectedTool.key.split(".")[0])
        ? selectedTool.key.split(".")[0]
        : selectedTool.key
    : "memory.context_bundle";
  const toolFamilies = useMemo(() => {
    const providerKeys = new Set(
      tools
        .filter((tool) => !tool.key.includes(".") && tool.key !== "gmail")
        .map((tool) => tool.key),
    );
    const families = tools
      .filter((tool) => providerKeys.has(tool.key))
      .map((provider) => ({
        provider,
        children: tools.filter(
          (tool) =>
            tool.key.startsWith(`${provider.key}.`) ||
            (provider.key === "google" && tool.key.startsWith("gmail.")),
        ),
      }));
    const childKeys = new Set(
      families.flatMap((family) => family.children.map((tool) => tool.key)),
    );
    const standalone = tools.filter(
      (tool) =>
        tool.key !== "gmail" &&
        !childKeys.has(tool.key) && !families.some((family) => family.provider.key === tool.key),
    );
    return { families, standalone };
  }, [tools]);
  const selectedToolConnections = connections.filter(
    (connection) => connection.tool_key === selectedConnectionToolKey,
  );
  const selectedToolAgents = useMemo(() => {
    if (!selectedTool) return [];
    if (providerToolKeys.has(selectedTool.key)) {
      const familyAgents = tools
        .filter(
          (tool) =>
            tool.key.startsWith(`${selectedTool.key}.`) ||
            (selectedTool.key === "google" && tool.key.startsWith("gmail.")),
        )
        .flatMap((tool) => tool.authorized_agents);
      const unique = new Map<string, ToolRegistryItem["authorized_agents"][number]>();
      familyAgents.forEach((agent) => {
        unique.set(`${agent.domain_key}-${agent.agent_key}`, agent);
      });
      return Array.from(unique.values()).sort((a, b) =>
        `${a.domain_key}-${a.agent_key}`.localeCompare(`${b.domain_key}-${b.agent_key}`),
      );
    }
    return selectedTool.authorized_agents;
  }, [providerToolKeys, selectedTool, tools]);
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
    const isGoogle = selectedConnectionToolKey === "google";
    setConnectionName(
      `${domainLabels[connectionDomain] ?? connectionDomain} ${
        isGitHub ? "GitHub" : isGoogle ? "Google Workspace" : selectedTool.name
      }`,
    );
    setConnectionAuthType(isGitHub ? "gh_cli" : isGoogle ? "oauth" : "service");
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
        : isGoogle
          ? JSON.stringify(
              {
                user_id: "me",
                client_id_env: "",
                client_secret_env: "",
                refresh_token_env: "",
                default_query: "",
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
            {selectedTool.key === "gmail" && (
              <p className="memory-status">
                Gmail now uses the shared Google Workspace OAuth config. Select the Google family
                to edit the domain connection used by Gmail, Drive, Docs, and Slides tools.
              </p>
            )}
            {selectedTool.key.startsWith("gmail.") && (
              <p className="memory-status">
                Gmail tools inherit the domain <strong>Google Workspace</strong> connection. Save
                user id plus refresh-token OAuth env config once on the Google family, then Gmail,
                Drive, Docs, and Slides tools can use it.
              </p>
            )}
            {selectedTool.key === "google" && (
              <p className="memory-status">
                Edit the shared Google Workspace OAuth config here. Drive, Docs, Slides, and related
                child tools inherit this domain connection. Use refresh-token OAuth env vars for
                durable scheduled workflows.
              </p>
            )}
            {selectedTool.key.startsWith("google.") && (
              <p className="memory-status">
                Google Workspace tools share one domain connection named{" "}
                <strong>Google Workspace</strong>. Save refresh-token OAuth env config once here,
                then every Google child tool can inherit it.
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
                  placeholder='{"user_id":"me","client_id_env":"GOOGLE_CLIENT_ID","client_secret_env":"GOOGLE_CLIENT_SECRET","refresh_token_env":"PRAXIS_GMAIL_REFRESH_TOKEN"}'
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

function SkillsWorkspace() {
  const [skills, setSkills] = useState<SkillRegistryItem[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<SkillRegistryItem | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [form, setForm] = useState({
    key: "",
    name: "",
    category: "general",
    domain_key: "",
    description: "",
    instruction: "",
  });

  const loadSkills = useCallback(async () => {
    const response = await apiJson<{ skills: SkillRegistryItem[] }>("/agents/skills");
    setSkills(response.skills);
    setStatusMessage("Skills refreshed.");
  }, []);

  useEffect(() => {
    loadSkills().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : "Unable to load skills."),
    );
  }, [loadSkills]);

  const selectSkill = (skill: SkillRegistryItem) => {
    setSelectedSkill(skill);
    setForm({
      key: skill.key,
      name: skill.name,
      category: skill.category,
      domain_key: skill.domain_key ?? "",
      description: skill.description ?? "",
      instruction: skill.instruction,
    });
  };

  const saveSkill = async () => {
    const response = await apiJson<{ skill: SkillRegistryItem }>("/agents/skills", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: form.key,
        name: form.name,
        category: form.category,
        domain_key: form.domain_key || null,
        description: form.description || null,
        instruction: form.instruction,
      }),
    });
    setSelectedSkill(response.skill);
    setStatusMessage("Skill saved.");
    await loadSkills();
  };

  const applyPlaybookTemplate = () => {
    const skillName = form.name.trim() || "Skill Name";
    setForm((current) => ({
      ...current,
      instruction: skillPlaybookTemplate(skillName),
    }));
  };

  return (
    <section className="admin-grid" aria-labelledby="skills-heading">
      <div className="admin-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Reusable instruction library</p>
            <h3 id="skills-heading">Skills</h3>
          </div>
          <button className="icon-button" onClick={loadSkills} title="Refresh skills" type="button">
            <RefreshCw size={18} />
          </button>
        </div>
        <div className="tool-registry-list">
          {skills.map((skill) => (
            <button
              className={`tool-registry-row selectable ${selectedSkill?.id === skill.id ? "active" : ""}`}
              key={skill.id}
              onClick={() => selectSkill(skill)}
              type="button"
            >
              <span>{skill.category}</span>
              <strong>{skill.name}</strong>
              <p>{skill.description || skill.instruction.slice(0, 160)}</p>
              <div className="preview-meta">
                <span>{skill.domain_key ? domainLabels[skill.domain_key] ?? skill.domain_key : "Global"}</span>
                <span>{skill.authorized_agents.length} agent(s)</span>
              </div>
            </button>
          ))}
          {skills.length === 0 && <p className="empty-state">No skills have been created yet.</p>}
        </div>
      </div>
      <div className="admin-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Skill editor</p>
            <h3>{selectedSkill ? selectedSkill.name : "New skill"}</h3>
          </div>
          <FileText size={18} />
        </div>
        <div className="admin-form">
          <label>
            <span>Key</span>
            <input
              value={form.key}
              onChange={(event) => setForm((current) => ({ ...current, key: event.target.value }))}
            />
          </label>
          <label>
            <span>Name</span>
            <input
              value={form.name}
              onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
            />
          </label>
          <label>
            <span>Category</span>
            <input
              value={form.category}
              onChange={(event) => setForm((current) => ({ ...current, category: event.target.value }))}
            />
          </label>
          <label>
            <span>Domain</span>
            <select
              value={form.domain_key}
              onChange={(event) => setForm((current) => ({ ...current, domain_key: event.target.value }))}
            >
              <option value="">Global</option>
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
            <span>Description</span>
            <textarea
              value={form.description}
              onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))}
              rows={3}
            />
          </label>
          <label>
            <span>Instruction</span>
            <textarea
              value={form.instruction}
              onChange={(event) => setForm((current) => ({ ...current, instruction: event.target.value }))}
              rows={16}
            />
          </label>
          <div className="skill-template-panel">
            <div>
              <strong>Playbook scaffold</strong>
              <p>
                Skills work best as concise operating procedures with use cases, validation rules,
                output contracts, and examples.
              </p>
            </div>
            <button type="button" onClick={applyPlaybookTemplate}>
              Apply template
            </button>
          </div>
          <button type="button" onClick={saveSkill} disabled={!form.key.trim() || !form.name.trim() || !form.instruction.trim()}>
            Save skill
          </button>
        </div>
        {selectedSkill && selectedSkill.authorized_agents.length > 0 && (
          <div className="prompt-preview">
            <h4>Authorized agents</h4>
            <div className="preview-meta">
              {selectedSkill.authorized_agents.map((agent) => (
                <span key={`${agent.domain_key}-${agent.agent_key}`}>
                  {agent.agent_name} / {domainLabels[agent.domain_key] ?? agent.domain_key}
                </span>
              ))}
            </div>
          </div>
        )}
        {statusMessage && <p className="memory-status">{statusMessage}</p>}
      </div>
    </section>
  );
}

function skillPlaybookTemplate(skillName: string) {
  return `## Purpose
Describe what ${skillName} helps an agent or Maestro accomplish.

## Use When
- List the situations where this skill should be applied.
- Name source types, domains, or work-item patterns that trigger it.

## Do Not Use When
- List confusing adjacent cases this skill should avoid.
- State what should become workflow work, routed memory, or direct chat instead.

## Required Inputs
- Source content:
- Domain:
- Relevant tool results or memory:

## Procedure
1. Step one.
2. Step two.
3. Step three.

## Output Contract
State exactly what the agent should produce. Include tool calls, routed candidate types, report sections, or artifact expectations when relevant.

## Validation Rules
- Rule that prevents common bad output.
- Rule that preserves provenance.
- Rule for when to ask Chris for clarification.

## Examples
- Good:
- Bad:
`;
}

function MemoryWorkspace() {
  const [domains, setDomains] = useState<DropboxDomain[]>(dropboxDomainDefaults);
  const [selectedDomain, setSelectedDomain] = useState("perti-laboratories");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previews, setPreviews] = useState<MemoryPreview[]>([]);
  const [pending, setPending] = useState<PendingProposal[]>([]);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [sources, setSources] = useState<MemorySource[]>([]);
  const [artifacts, setArtifacts] = useState<MemoryArtifact[]>([]);
  const [sourceTargetDomain, setSourceTargetDomain] = useState("personal");
  const [selectedPreviewFilename, setSelectedPreviewFilename] = useState<string | null>(null);
  const [retrievalDomain, setRetrievalDomain] = useState("");
  const [retrievalQuery, setRetrievalQuery] = useState("");
  const [retrievalMode, setRetrievalMode] = useState<"balanced" | "strict" | "broad">("balanced");
  const [semanticRetrieval, setSemanticRetrieval] = useState(true);
  const [retrievalResults, setRetrievalResults] = useState<FederatedRetrievedDocument[]>([]);
  const [retrievalTotal, setRetrievalTotal] = useState(0);
  const [retrievalFiltered, setRetrievalFiltered] = useState(0);
  const [semanticStatus, setSemanticStatus] = useState("not requested");
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [lastProcessSummary, setLastProcessSummary] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [ingestionHealth, setIngestionHealth] = useState<IngestionHealth | null>(null);

  const refreshMemory = useCallback(async () => {
    const [
      statusResult,
      previewResult,
      pendingResult,
      itemResult,
      sourceResult,
      artifactResult,
      ingestionResult,
    ] =
      await Promise.allSettled([
        apiJson<{ domains: DropboxDomain[] }>("/memory/dropbox/status"),
        apiJson<{ previews: MemoryPreview[] }>("/memory/dropbox/previews"),
        apiJson<{ proposals: PendingProposal[] }>("/memory/proposals/pending"),
        apiJson<{ items: MemoryItem[] }>("/memory/items?limit=8"),
        apiJson<{ sources: MemorySource[] }>("/memory/sources?limit=8"),
        apiJson<{ artifacts: MemoryArtifact[] }>("/memory/artifacts?limit=12"),
        apiJson<IngestionHealth>("/memory/ingestion/status"),
      ]);
    if (statusResult.status !== "fulfilled") {
      throw statusResult.reason;
    }
    const status = statusResult.value;
    const previewResponse = previewResult.status === "fulfilled" ? previewResult.value : { previews: [] };
    const pendingResponse = pendingResult.status === "fulfilled" ? pendingResult.value : { proposals: [] };
    const itemResponse = itemResult.status === "fulfilled" ? itemResult.value : { items: [] };
    const sourceResponse = sourceResult.status === "fulfilled" ? sourceResult.value : { sources: [] };
    const artifactResponse = artifactResult.status === "fulfilled" ? artifactResult.value : { artifacts: [] };
    setDomains(status.domains);
    const sortedPreviews = [...previewResponse.previews].sort(
      (first, second) => previewTime(second) - previewTime(first),
    );
    setPreviews(sortedPreviews.slice(0, 10));
    setPending(pendingResponse.proposals);
    setItems(itemResponse.items);
    setSources(sourceResponse.sources);
    setArtifacts(artifactResponse.artifacts);
    if (ingestionResult.status === "fulfilled") {
      setIngestionHealth(ingestionResult.value);
    }
    if (artifactResult.status === "rejected") {
      setStatusMessage("Memory loaded. Restart the backend to enable run artifact audit.");
    }
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

  const pollContextMailbox = async () => {
    setBusy(true);
    try {
      const result = await apiJson<{
        counts: Record<string, number>;
      }>("/memory/ingestion/context-mailbox/poll", { method: "POST" });
      const staged = result.counts.staged ?? 0;
      const duplicate = result.counts.duplicate ?? 0;
      const quarantined = result.counts.quarantined ?? 0;
      const failed = result.counts.failed ?? 0;
      setStatusMessage(
        `Mailbox checked: ${staged} staged, ${duplicate} duplicate, ${quarantined} quarantined, ${failed} failed.`,
      );
      await refreshMemory();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Mailbox check failed.");
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
        use_semantic: semanticRetrieval ? "true" : "false",
        max_items: "10",
        max_chars: "5000",
      });
      if (retrievalDomain) {
        params.set("domain_key", retrievalDomain);
      }
      if (retrievalQuery.trim()) {
        params.set("query_text", retrievalQuery.trim());
      }
      const response = await apiJson<{
        dropped_count: number;
        policy_filtered_count: number;
        semantic_status: string;
        results: FederatedRetrievedDocument[];
      }>(`/memory/federated-retrieve?${params.toString()}`);
      setRetrievalResults(response.results);
      setRetrievalTotal(response.results.length + response.dropped_count);
      setRetrievalFiltered(response.dropped_count + response.policy_filtered_count);
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
              <option value="">All domains</option>
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
          <button className="planner-action" onClick={pollContextMailbox} disabled={busy}>
            <Mail size={17} />
            Check mailbox
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
        {ingestionHealth && (
          <div className="ingestion-health" aria-label="Ingestion health">
            <div className="dropbox-stats">
              <span>Sources {ingestionHealth.source_registrations}</span>
              <span>Tracked {Object.values(ingestionHealth.records).reduce((a, b) => a + b, 0)}</span>
              <span>Duplicates skipped {ingestionHealth.duplicates_skipped}</span>
              <span>Failures {ingestionHealth.records.failed ?? 0}</span>
            </div>
            <p className="memory-status">
              Source policy and provenance travel with derived memory and are enforced during
              prompt assembly.
            </p>
            {ingestionHealth.context_mailbox && (
              <p className="memory-status">
                Context mailbox {ingestionHealth.context_mailbox.mailbox ?? "not configured"}: {" "}
                {ingestionHealth.context_mailbox.status}
                {ingestionHealth.context_mailbox.last_counts.staged !== undefined &&
                  ` / last check ${ingestionHealth.context_mailbox.last_counts.staged ?? 0} staged, ${
                    ingestionHealth.context_mailbox.last_counts.quarantined ?? 0
                  } quarantined, ${ingestionHealth.context_mailbox.last_counts.failed ?? 0} failed`}
                {ingestionHealth.context_mailbox.last_error &&
                  ` / ${ingestionHealth.context_mailbox.last_error}`}
              </p>
            )}
          </div>
        )}
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
                {item.domain_key ?? "global"} / {item.store} / score {item.score.toFixed(2)}
              </span>
              <h4>{item.title}</h4>
              <p>{item.content}</p>
              <div className="preview-meta">
                <span>lexical {(item.lexical_score * 100).toFixed(0)}%</span>
                <span>
                  semantic{" "}
                  {item.semantic_similarity === null
                    ? "n/a"
                    : `${(item.semantic_similarity * 100).toFixed(0)}%`}
                </span>
                <span>trust {(item.trust_score * 100).toFixed(0)}%</span>
                <span>recency {(item.recency_score * 100).toFixed(0)}%</span>
              </div>
              <p className="evaluation-note">{item.score_reasons.join(" | ")}</p>
              <div className="preview-meta">
                <span>{item.document_key}</span>
                <span>{item.source_timestamp ? new Date(item.source_timestamp).toLocaleDateString() : "no source date"}</span>
                <span>{String(item.policy.egress_policy ?? "external_allowed")}</span>
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

      <section className="memory-panel" aria-labelledby="memory-artifacts-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">System sources</p>
            <h3 id="memory-artifacts-heading">Run artifacts</h3>
          </div>
          <FileText size={18} />
        </div>
        <div className="source-list">
          {artifacts.map((artifact) => (
            <article className="source-row" key={artifact.id}>
              <div>
                <span>
                  {domainLabels[artifact.domain_key] ?? artifact.domain_key} / {artifact.artifact_type}
                </span>
                <h4>{artifact.name}</h4>
                <p>
                  {artifact.memory_count} memories / {artifact.proposal_count} proposals
                </p>
                <div className="preview-meta">
                  {artifact.report_id && <span>report {artifact.report_id.slice(0, 8)}</span>}
                  {artifact.task_id && <span>task {artifact.task_id.slice(0, 8)}</span>}
                  {artifact.created_at && <span>{formatDateTime(artifact.created_at)}</span>}
                </div>
              </div>
              <details>
                <summary>Source path</summary>
                <p>{artifact.uri}</p>
              </details>
            </article>
          ))}
          {artifacts.length === 0 && (
            <p className="empty-state">
              Canonical workflow and session artifacts will appear here after runs are staged.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
