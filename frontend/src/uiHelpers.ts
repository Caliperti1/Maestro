import type {
  MemoryPreview,
  PreviewResult,
  RoutedObjectRecord,
  SchedulerDefinition,
} from "./types";

export function resultLabel(result?: PreviewResult) {
  if (!result) return "Preview only";
  if (result.memory_item_id) return "Written to memory";
  if (result.outcome === "duplicate_skipped") return "Duplicate skipped";
  if (result.outcome === "reinforced") return "Reinforced existing memory";
  if (result.outcome === "rejected") return "Rejected by memory manager";
  if (result.outcome === "pending_user_approval") return "Needs approval";
  if (result.proposal_status) return `Proposal ${result.proposal_status}`;
  return result.outcome ?? "Processed";
}

export function resultClass(result?: PreviewResult) {
  if (!result) return "preview-only";
  if (result.memory_item_id) return "written";
  if (result.outcome === "duplicate_skipped" || result.outcome === "reinforced") return "deduped";
  if (result.outcome === "rejected") return "rejected";
  if (result.outcome === "pending_user_approval") return "pending";
  return "processed";
}

export function candidateResultLabel(preview: MemoryPreview, index: number) {
  const result = preview.payload.results?.[index];
  if (result) return resultLabel(result);
  if (preview.is_processing) return "Queued for write";
  return "Preview only";
}

export function candidateResultClass(preview: MemoryPreview, index: number) {
  const result = preview.payload.results?.[index];
  if (result) return resultClass(result);
  if (preview.is_processing) return "processing";
  return "preview-only";
}

export function previewTime(preview: MemoryPreview) {
  const time = preview.generated_at ? Date.parse(preview.generated_at) : 0;
  return Number.isFinite(time) ? time : 0;
}

export function definitionQueueItems(definition: SchedulerDefinition) {
  return Array.isArray(definition.workflow_spec?.queue_items)
    ? (definition.workflow_spec.queue_items as Array<Record<string, unknown>>)
    : [];
}

export function unassignedDefinitionItemCount(definition: SchedulerDefinition) {
  return definitionQueueItems(definition).filter((item) => !item.agent_key).length;
}

export function triggerSummary(triggerType: string, triggerConfig: Record<string, unknown>) {
  if (triggerType === "event") {
    return `When ${String(triggerConfig.event_type ?? "event arrives")}`;
  }
  if (typeof triggerConfig.time_of_day === "string") {
    return `Daily at ${triggerConfig.time_of_day}`;
  }
  if (typeof triggerConfig.next_run_at === "string") {
    return `Next ${triggerConfig.next_run_at}`;
  }
  if (typeof triggerConfig.interval_minutes === "number") {
    return `Every ${triggerConfig.interval_minutes} minutes`;
  }
  return triggerType;
}

export function formatDateTime(value?: string | null) {
  if (!value) return "Not set";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function formatDateOnly(value?: string | null) {
  if (!value) return "Unscheduled";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

export function safeJson(value: unknown) {
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function routedObjectTitle(item: RoutedObjectRecord) {
  if ("title" in item) return item.title;
  return item.name;
}

export function routedObjectDomain(item: RoutedObjectRecord) {
  return "domain_key" in item ? item.domain_key : null;
}

export function messageReferencesActivePlan(message: string) {
  const normalized = message.trim().toLowerCase();
  if (!normalized) return false;
  return [
    "this plan",
    "that plan",
    "the plan",
    "current plan",
    "active plan",
    "this workflow",
    "that workflow",
    "the workflow",
    "current workflow",
    "active workflow",
    "merge the pr",
    "merge pr",
    "merge it",
    "merge that",
    "hot reload",
    "reload the app",
    "make it live",
    "ship it",
    "approved",
    "approve",
    "reject",
    "run it",
    "save it",
    "save schedule",
    "change the plan",
    "update the plan",
    "refine",
    "instead",
    "also include",
    "remove ",
    "drop ",
    "only ",
    "belongs in",
    "move this",
    "do this first",
    "do that first",
  ].some((token) => normalized.includes(token));
}
