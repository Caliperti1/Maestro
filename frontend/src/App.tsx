import {
  Bot,
  Building2,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  HardDriveUpload,
  Inbox,
  ListTodo,
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
} from "lucide-react";
import { Calendar, dateFnsLocalizer, View, Views } from "react-big-calendar";
import { format, getDay, parse, startOfWeek } from "date-fns";
import { enUS } from "date-fns/locale/en-US";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { API_BASE_URL, apiJson, websocketUrl } from "./api";
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
  DomainContext,
  DropboxDomain,
  MaestroPlan,
  MaestroRespond,
  MaestroRun,
  MaestroSessionSummary,
  MaestroSubtask,
  MaestroToolCallResponse,
  MaestroQueueItem,
  MemoryItem,
  MemoryPreview,
  MemorySource,
  PendingProposal,
  PromptPackage,
  RetrievedMemory,
  RoutedEvent,
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
  ToolConnection,
  ToolRegistryItem,
} from "./types";
import {
  candidateResultClass,
  candidateResultLabel,
  definitionQueueItems,
  formatDateOnly,
  formatDateTime,
  messageReferencesActivePlan,
  previewTime,
  routedObjectDomain,
  routedObjectTitle,
  safeJson,
  triggerSummary,
  unassignedDefinitionItemCount,
} from "./uiHelpers";

const calendarLocalizer = dateFnsLocalizer({
  format,
  parse,
  startOfWeek,
  getDay,
  locales: { "en-US": enUS },
});

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

function MarkdownMessage({ content }: { content: string }) {
  const lines = content.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let orderedItems: string[] = [];
  let codeLines: string[] = [];
  let inCodeBlock = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const text = paragraph.join(" ");
    blocks.push(<p key={`p-${blocks.length}`}>{renderInlineMarkdown(text, `p-${blocks.length}`)}</p>);
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

  return <div className="markdown-message">{blocks.length > 0 ? blocks : <p>{content}</p>}</div>;
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
    responseKey: "events" | "contacts" | "todos" | "entities" | "ideas";
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
  ideas: {
    title: "Think Tank",
    eyebrow: "Routed ideas",
    endpoint: "/memory/routed-objects/ideas",
    responseKey: "ideas",
    icon: Sparkles,
    empty: "No think tank ideas yet.",
  },
};

function routedDraftFor(item: RoutedObjectRecord | null): Record<string, string> {
  if (!item) return {};
  if ("attendees" in item) {
    return {
      title: item.title ?? "",
      summary: item.summary ?? "",
      start_at: item.start_at ?? "",
      end_at: item.end_at ?? "",
      location: item.location ?? "",
      status: item.status ?? "scheduled",
    };
  }
  if ("todo_type" in item) {
    return {
      title: item.title ?? "",
      description: item.description ?? "",
      due_at: item.due_at ?? "",
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
      summary: item.summary ?? "",
      origination: item.origination ?? "",
      status: item.status ?? "active",
    };
  }
  if ("content" in item) {
    return {
      title: item.title ?? "",
      content: item.content ?? "",
      status: item.status ?? "open",
    };
  }
  return {
    name: item.name ?? "",
    website: item.website ?? "",
    summary: item.summary ?? "",
    status: item.status ?? "active",
  };
}

function RoutedObjectsWorkspace({ surface }: { surface: RoutedObjectSurface }) {
  const config = routedSurfaceConfig[surface];
  const Icon = config.icon;
  const [items, setItems] = useState<RoutedObjectRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [domainFilter, setDomainFilter] = useState("all");
  const [showArchived, setShowArchived] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [calendarView, setCalendarView] = useState<View>(Views.WEEK);
  const [calendarDate, setCalendarDate] = useState(new Date());
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [statusMessage, setStatusMessage] = useState("Ready");
  const [busy, setBusy] = useState(false);

  const supportsDomainFilter = surface === "calendar" || surface === "todos" || surface === "ideas";
  const supportsLifecycleFilters = surface === "calendar" || surface === "todos" || surface === "ideas";
  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
        if (!showArchived && item.status === "archived") return false;
        if (!showDone && item.status === "done") return false;
        return true;
      }),
    [items, showArchived, showDone],
  );
  const selectedItem =
    visibleItems.find((item) => item.id === selectedId) ?? visibleItems[0] ?? null;
  const calendarItems = useMemo(
    () =>
      visibleItems
        .filter((item): item is RoutedEvent => "start_at" in item && Boolean(item.start_at))
        .map((item) => {
          const start = new Date(item.start_at!);
          const end = item.end_at ? new Date(item.end_at) : new Date(start.getTime() + 60 * 60 * 1000);
          return {
            id: item.id,
            title: item.title,
            start,
            end,
            resource: item,
          };
        }),
    [visibleItems],
  );
  const unscheduledCalendarItems = visibleItems.filter(
    (item): item is RoutedEvent => "start_at" in item && !item.start_at,
  );

  const refreshItems = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" });
    if (supportsDomainFilter && domainFilter !== "all") {
      params.set("domain_key", domainFilter);
    }
    const response = await apiJson<Record<string, RoutedObjectRecord[]>>(
      `${config.endpoint}?${params.toString()}`,
    );
    const nextItems = response[config.responseKey] ?? [];
    setItems(nextItems);
    setSelectedId((current) =>
      current && nextItems.some((item) => item.id === current) ? current : (nextItems[0]?.id ?? null),
    );
    setStatusMessage("Ready");
  }, [config.endpoint, config.responseKey, domainFilter, supportsDomainFilter]);

  useEffect(() => {
    refreshItems().catch((error) =>
      setStatusMessage(error instanceof Error ? error.message : `Unable to load ${config.title}.`),
    );
  }, [refreshItems, config.title]);

  useEffect(() => {
    setDraft(routedDraftFor(selectedItem));
  }, [selectedItem?.id]);

  const updateDraft = (key: string, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const saveSelected = async () => {
    if (!selectedItem) return;
    setBusy(true);
    try {
      await apiJson(`${config.endpoint}/${selectedItem.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          updates: Object.fromEntries(
            Object.entries(draft).map(([key, value]) => [key, value.trim() || null]),
          ),
        }),
      });
      setStatusMessage(`${config.title} item saved.`);
      await refreshItems();
    } catch (error) {
      setStatusMessage(error instanceof Error ? error.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const archiveSelected = async () => {
    if (!selectedItem) return;
    const objectType =
      surface === "calendar"
        ? "event"
        : surface === "todos"
          ? "todo"
          : surface === "contacts"
            ? "contact"
            : surface === "ideas"
              ? "idea"
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
    if (!selectedItem || !(surface === "calendar" || surface === "todos" || surface === "ideas")) return;
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

  return (
    <div className="routed-object-workspace">
      <section className="memory-panel routed-object-list-panel" aria-labelledby={`${surface}-heading`}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">{config.eyebrow}</p>
            <h3 id={`${surface}-heading`}>{config.title}</h3>
          </div>
          <button className="icon-button" onClick={refreshItems} title={`Refresh ${config.title}`}>
            <RefreshCw size={18} />
          </button>
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
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={showDone}
                onChange={(event) => setShowDone(event.target.checked)}
              />
              Show done
            </label>
          </div>
        )}

        {surface === "calendar" && (
          <div className="calendar-shell">
            <Calendar
              localizer={calendarLocalizer}
              events={calendarItems}
              startAccessor="start"
              endAccessor="end"
              view={calendarView}
              date={calendarDate}
              views={[Views.MONTH, Views.WEEK, Views.DAY, Views.AGENDA]}
              onView={(view) => setCalendarView(view)}
              onNavigate={(date) => setCalendarDate(date)}
              onSelectEvent={(event) => setSelectedId(event.id)}
              eventPropGetter={(event) => ({
                className:
                  event.resource.status === "done"
                    ? "calendar-event-done"
                    : event.resource.status === "archived"
                      ? "calendar-event-archived"
                      : "",
              })}
            />
          </div>
        )}

        {surface === "calendar" && unscheduledCalendarItems.length > 0 && (
          <div className="unscheduled-list">
            <span>Unscheduled</span>
            {unscheduledCalendarItems.map((item) => (
              <button
                className={item.id === selectedItem?.id ? "routed-object-row active" : "routed-object-row"}
                key={item.id}
                onClick={() => setSelectedId(item.id)}
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

        <div className="routed-object-list">
          {visibleItems.map((item) => (
            <button
              className={item.id === selectedItem?.id ? "routed-object-row active" : "routed-object-row"}
              key={item.id}
              onClick={() => setSelectedId(item.id)}
              type="button"
            >
              {item.status === "done" ? <CheckCircle2 size={18} /> : <Icon size={18} />}
              <span>
                <strong>{routedObjectTitle(item)}</strong>
                <small>
                  {surface === "calendar" && "start_at" in item
                    ? `${formatDateOnly(item.start_at)} / ${item.status}`
                    : surface === "todos" && "due_at" in item
                      ? `${domainLabels[item.domain_key ?? "global"] ?? item.domain_key ?? "Global"} / ${item.status} / ${item.priority}`
                      : item.status}
                </small>
              </span>
            </button>
          ))}
          {visibleItems.length === 0 && <p className="empty-state">{config.empty}</p>}
        </div>
      </section>

      <section className="memory-panel routed-object-detail-panel" aria-labelledby={`${surface}-detail`}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">Details</p>
            <h3 id={`${surface}-detail`}>{selectedItem ? routedObjectTitle(selectedItem) : config.title}</h3>
          </div>
          <Icon size={18} />
        </div>

        {selectedItem ? (
          <div className="routed-object-detail">
            {"attendees" in selectedItem && (
              <>
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
                    <input value={draft.start_at ?? ""} onChange={(event) => updateDraft("start_at", event.target.value)} />
                  </label>
                  <label>
                    End
                    <input value={draft.end_at ?? ""} onChange={(event) => updateDraft("end_at", event.target.value)} />
                  </label>
                </div>
                <label>
                  Location
                  <input value={draft.location ?? ""} onChange={(event) => updateDraft("location", event.target.value)} />
                </label>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
                <details>
                  <summary>Attendees and supporting content</summary>
                  <pre>{safeJson({ attendees: selectedItem.attendees, supporting_refs: selectedItem.supporting_refs })}</pre>
                </details>
              </>
            )}

            {"todo_type" in selectedItem && (
              <>
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
                    <input value={draft.due_at ?? ""} onChange={(event) => updateDraft("due_at", event.target.value)} />
                  </label>
                  <label>
                    Priority
                    <input value={draft.priority ?? ""} onChange={(event) => updateDraft("priority", event.target.value)} />
                  </label>
                </div>
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

            {"email" in selectedItem && (
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
                  <span>{selectedItem.organization_entity_id ? `Organization ${selectedItem.organization_entity_id.slice(0, 8)}` : "No linked organization"}</span>
                  <span>{selectedItem.scheduled_event_ids.length} scheduled contacts</span>
                </div>
              </>
            )}

            {"website" in selectedItem && !("email" in selectedItem) && (
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
                  Summary
                  <textarea value={draft.summary ?? ""} onChange={(event) => updateDraft("summary", event.target.value)} />
                </label>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
              </>
            )}

            {"content" in selectedItem && (
              <>
                <label>
                  Title
                  <input value={draft.title ?? ""} onChange={(event) => updateDraft("title", event.target.value)} />
                </label>
                <label>
                  Idea
                  <textarea value={draft.content ?? ""} onChange={(event) => updateDraft("content", event.target.value)} />
                </label>
                <label>
                  Status
                  <input value={draft.status ?? ""} onChange={(event) => updateDraft("status", event.target.value)} />
                </label>
              </>
            )}

            <div className="routed-detail-actions">
              {(surface === "calendar" || surface === "todos" || surface === "ideas") && selectedItem.status !== "done" && (
                <button className="planner-action" onClick={markSelectedDone} disabled={busy}>
                  <CheckCircle2 size={16} />
                  Done
                </button>
              )}
              <button className="planner-action" onClick={saveSelected} disabled={busy}>
                Save
              </button>
              <button className="danger-action" onClick={archiveSelected} disabled={busy}>
                <Trash2 size={16} />
                Archive
              </button>
            </div>

            <div className="routed-object-meta">
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
            </div>
            <p className="memory-status">{statusMessage}</p>
          </div>
        ) : (
          <p className="empty-state">{config.empty}</p>
        )}
      </section>
    </div>
  );
}

function NeedsAttentionPanel({ schedulerDashboard }: { schedulerDashboard: SchedulerDashboard | null }) {
  const [todos, setTodos] = useState<RoutedTodo[]>([]);
  const [statusMessage, setStatusMessage] = useState("Ready");

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
      run.status === "failed" ||
      run.queue_items.some((item) => ["blocked", "approval_required", "failed"].includes(item.status)),
    ) ?? [];

  return (
    <section className="attention-panel dashboard-wide" aria-labelledby="attention-heading">
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
        {blockedRuns.map((run) => (
          <article className="attention-card" key={run.id}>
            <CircleAlert size={18} />
            <div>
              <span>{run.status}</span>
              <h4>{run.summary || "Workflow needs attention"}</h4>
              <p>{run.error_message || "Open the Queue panel to inspect blocked or failed work."}</p>
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
            </div>
          </article>
        ))}
        {blockedRuns.length === 0 && todos.length === 0 && (
          <p className="empty-state">No blocked workflows, approvals, or RFIs are waiting right now.</p>
        )}
      </div>
      <p className="memory-status">{statusMessage}</p>
    </section>
  );
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [activeDomain, setActiveDomain] = useState("Maestro");
  const [activeSurface, setActiveSurface] = useState<ActiveSurface>("dashboard");
  const [memoryNavOpen, setMemoryNavOpen] = useState(true);
  const [domainsNavOpen, setDomainsNavOpen] = useState(true);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const chatThreadRef = useRef<HTMLDivElement | null>(null);
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

  useEffect(() => {
    const thread = chatThreadRef.current;
    if (!thread) return;
    thread.scrollTo({ top: thread.scrollHeight, behavior: "smooth" });
  }, [chatMessages, maestroBusy]);
  const [schedulerWorkerStatus, setSchedulerWorkerStatus] =
    useState<SchedulerWorkerStatus | null>(null);

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

  const planScheduleCandidate = useMemo(() => {
    const candidate = maestroPlan?.scheduler.schedule_candidate;
    return candidate && typeof candidate === "object"
      ? (candidate as Record<string, unknown>)
      : null;
  }, [maestroPlan]);

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

  const loadSchedulerWorkerStatus = useCallback(async () => {
    const response = await apiJson<{ worker: SchedulerWorkerStatus }>("/scheduler/worker/status");
    setSchedulerWorkerStatus(response.worker);
  }, []);

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
    loadSchedulerWorkerStatus().catch(() => undefined);
  }, [loadActiveSession, loadSchedulerDashboard, loadSchedulerWorkerStatus, loadSessionHistory]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      loadSchedulerDashboard().catch(() => undefined);
      loadSchedulerWorkerStatus().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(interval);
  }, [loadSchedulerDashboard, loadSchedulerWorkerStatus]);

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
    const activePlanId =
      maestroPlan && messageReferencesActivePlan(outgoingMessage.content)
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
    const isScheduledApproval = Boolean(planScheduleCandidate);
    setMaestroBusy(true);
    setMaestroStatus(isScheduledApproval ? "Saving scheduled workflow..." : "Running workflow...");
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
      setChatMessages((messages) => {
        const alreadyShown = messages.some((message) => message.content === response.run.chat_summary);
        if (alreadyShown) return messages;
        return [
          ...messages,
          {
            id: crypto.randomUUID(),
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
          : response.run.status === "completed"
            ? "Workflow completed."
            : "Workflow finished.",
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
          <div className="nav-group">
            <button
              className={
                ["memory", "calendar", "contacts", "todos", "organizations", "ideas"].includes(activeSurface)
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
                  className={activeSurface === "ideas" ? "domain-button active" : "domain-button"}
                  onClick={() => setActiveSurface("ideas")}
                  type="button"
                >
                  <Sparkles size={16} />
                  <span>Think Tank</span>
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
                : activeSurface === "tools"
                  ? "Tools"
                  : activeSurface in routedSurfaceConfig
                    ? routedSurfaceConfig[activeSurface as RoutedObjectSurface].title
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
        ) : activeSurface in routedSurfaceConfig ? (
          <RoutedObjectsWorkspace surface={activeSurface as RoutedObjectSurface} />
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
                  {conductingMessage}
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
                      {planScheduleCandidate ? "Save schedule" : "Run plan"}
                    </button>
                  </div>
                  <p>{maestroPlan.summary}</p>
                  {planScheduleCandidate && (
                    <p className="evaluation-note">
                      This will save a scheduled workflow in Queue instead of executing the agents now.
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

            <section className="planner-panel" aria-labelledby="review-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Review</p>
                  <h3 id="review-heading">Generated output</h3>
                </div>
                <FileText size={18} />
              </div>

              {maestroRun ? (
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
                    Workflow reports, staged artifacts, PR summaries, and generated documents will
                    appear here when Maestro has something for review.
                  </p>
                </div>
              )}
            </section>

            <section className="reports-panel" aria-labelledby="reports-heading">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Scheduler</p>
                  <h3 id="reports-heading">Queue</h3>
                </div>
                <Clock3 size={18} />
              </div>
              {false && (
              <div className="scheduler-control-panel">
                <div className="workflow-detail-heading">
                  <div>
                    <span>Control</span>
                    <h4>Workflow triggers</h4>
                  </div>
                  <label className="worker-toggle">
                    <input
                      type="checkbox"
                      checked={Boolean(schedulerWorkerStatus?.enabled)}
                      onChange={(event) =>
                        updateSchedulerWorkerStatus({ enabled: event.target.checked })
                      }
                    />
                    <span>{schedulerWorkerStatus?.enabled ? "Auto worker on" : "Auto worker off"}</span>
                  </label>
                  {!schedulerWorkerStatus?.enabled && (
                    <>
                      <button type="button" onClick={runSchedulerTick}>
                        Run tick
                      </button>
                      <button type="button" onClick={runSchedulerWorker}>
                        Run worker
                      </button>
                    </>
                  )}
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
                {!schedulerWorkerStatus?.enabled && (
                  <p className="scheduler-help-text">
                    Run tick only enqueues and claims ready items; Run worker enqueues, claims,
                    and executes once.
                  </p>
                )}
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
                  <textarea
                    value={schedulerDefinitionObjective}
                    onChange={(event) => setSchedulerDefinitionObjective(event.target.value)}
                    rows={3}
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
              )}
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
                  No active workflow selected. Proposed, queued, or running workflows can be
                  inspected here when they need attention.
                </p>
              )}
              {schedulerDashboard && schedulerDashboard.runs.length > 0 && (
                <div className="scheduler-run-list" aria-label="Durable scheduler runs">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>Durable queue</span>
                      <h4>Active workflow queue</h4>
                    </div>
                    <span>{schedulerDashboard.runnable_batches.length} runnable batch(es)</span>
                  </div>
                  {schedulerDashboard.runs.slice(0, 5).map((run) => {
                    const completed = run.queue_items.filter((item) => item.status === "completed").length;
                    const blocked = run.queue_items.filter((item) => item.status === "blocked").length;
                    const unassigned = run.queue_items.filter((item) => !item.agent_key).length;
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
                          {unassigned > 0 && <span className="warning-pill">{unassigned} need agent</span>}
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
              {schedulerDashboard && schedulerDashboard.runs.length === 0 && (
                <p className="empty-state">
                  No active queued work. Completed runs are kept as history instead of staying in
                  the queue.
                </p>
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
                          {!item.agent_key && <span className="warning-pill">Needs agent assignment</span>}
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
              {scheduledWorkflowDefinitions.length > 0 && (
                <div className="scheduler-run-list" aria-label="Scheduled workflow definitions">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>Schedules</span>
                      <h4>Scheduled workflows</h4>
                    </div>
                    <span>{scheduledWorkflowDefinitions.length} configured</span>
                  </div>
                  {scheduledWorkflowDefinitions.slice(0, 5).map((definition) => {
                    const unassigned = unassignedDefinitionItemCount(definition);
                    return (
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
                          <span>{triggerSummary(definition.trigger_type, definition.trigger_config)}</span>
                          <span>{definition.priority}</span>
                          <span>{definition.fairness_group || definition.domain_key || "global"} fairness</span>
                          {unassigned > 0 && <span className="warning-pill">{unassigned} need agent</span>}
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
                    );
                  })}
                </div>
              )}
              {triggerWorkflowDefinitions.length > 0 && (
                <div className="scheduler-run-list" aria-label="Trigger workflow definitions">
                  <div className="workflow-detail-heading">
                    <div>
                      <span>Triggers</span>
                      <h4>Trigger workflows</h4>
                    </div>
                    <span>{triggerWorkflowDefinitions.length} configured</span>
                  </div>
                  {triggerWorkflowDefinitions.slice(0, 5).map((definition) => {
                    const unassigned = unassignedDefinitionItemCount(definition);
                    return (
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
                          <span>{triggerSummary(definition.trigger_type, definition.trigger_config)}</span>
                          <span>{definition.priority}</span>
                          <span>{definition.fairness_group || definition.domain_key || "global"} fairness</span>
                          {unassigned > 0 && <span className="warning-pill">{unassigned} need agent</span>}
                          {typeof definition.trigger_config.event_type === "string" && (
                            <span>{definition.trigger_config.event_type}</span>
                          )}
                        </div>
                        <div className="scheduler-action-row compact-actions">
                          <button type="button" onClick={() => selectSchedulerDefinition(definition)}>
                            Edit trigger
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>

            <NeedsAttentionPanel schedulerDashboard={schedulerDashboard} />
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
    gmail: true,
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
  const selectedConnectionToolKey =
    selectedTool?.key.includes(".") && providerToolKeys.has(selectedTool.key.split(".")[0])
      ? selectedTool.key.split(".")[0]
      : (selectedTool?.key ?? "memory.context_bundle");
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
    if (providerToolKeys.has(selectedTool.key)) {
      const familyAgents = tools
        .filter((tool) => tool.key.startsWith(`${selectedTool.key}.`))
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
    const isGmail = selectedConnectionToolKey === "gmail";
    setConnectionName(
      `${domainLabels[connectionDomain] ?? connectionDomain} ${
        isGitHub ? "GitHub" : isGmail ? "Gmail" : selectedTool.name
      }`,
    );
    setConnectionAuthType(isGitHub ? "gh_cli" : isGmail ? "oauth" : "service");
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
        : isGmail
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
                Edit the shared Gmail OAuth config here. Every Gmail child tool in this domain
                inherits it unless a more specific override is added later. Use refresh-token
                OAuth env vars for durable scheduled workflows.
              </p>
            )}
            {selectedTool.key.startsWith("gmail.") && (
              <p className="memory-status">
                Gmail tools share one domain connection named <strong>Gmail</strong>. Save user id
                plus refresh-token OAuth env config once here, then every Gmail tool can inherit it.
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
