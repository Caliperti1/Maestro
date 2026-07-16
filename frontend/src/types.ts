export type ChatMessage = {
  id: string;
  sender: "user" | "maestro";
  content: string;
  metadata?: Record<string, unknown>;
};

export type MaestroSessionSummary = {
  id: string;
  title: string;
  active_topic?: {
    id: string;
    title: string;
    started_at?: string | null;
    updated_at?: string | null;
  } | null;
  messages: ChatMessage[];
  message_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  stagedArtifactPath: string | null;
  active_plan?: MaestroPlan | null;
  archived?: boolean;
  archived_at?: string | null;
};

export type SchedulerQueueItem = {
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
  required_skills?: string[];
  model_profile?: string | null;
  model_tier?: string;
  model_rationale?: string | null;
  lease_owner: string | null;
  error_message: string | null;
  output_payload?: Record<string, unknown>;
};

export type SchedulerRun = {
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

export type SchedulerEvent = {
  id: string;
  event_type: string;
  message: string;
  queue_item_id: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
};

export type SchedulerDefinition = {
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

export type SchedulerDashboard = {
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

export type SchedulerWorkerAgentRun = {
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

export type SchedulerWorkerStatus = {
  enabled: boolean;
  interval_seconds: number;
  claim_limit: number;
  execute_llm: boolean;
  auto_tool_loop: boolean;
  source: string;
};

export type DropboxDomain = {
  key: string;
  inbox: number;
  processing: number;
  processed: number;
  failed: number;
  previews: number;
};

export type MemoryPreview = {
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

export type PreviewResult = NonNullable<MemoryPreview["payload"]["results"]>[number];

export type PendingProposal = {
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

export type MemoryItem = {
  id: string;
  scope: string;
  memory_type: string;
  title: string;
  content: string;
  impact_level: string;
  importance: number;
  created_at: string | null;
};

export type MemorySource = {
  id: string;
  name: string;
  status: string;
  domain_key: string;
  memory_count: number;
  proposal_count: number;
  processed_at: string | null;
};

export type MemoryArtifact = {
  id: string;
  name: string;
  artifact_type: string;
  uri: string;
  mime_type: string | null;
  domain_key: string;
  task_id: string | null;
  report_id: string | null;
  seed_package_id: string | null;
  memory_count: number;
  proposal_count: number;
  canonical: boolean;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedItem = {
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

export type RoutedEvent = {
  id: string;
  domain_key: string | null;
  title: string;
  summary: string | null;
  start_at: string | null;
  end_at: string | null;
  location: string | null;
  attendees: unknown[];
  supporting_refs?: Array<Record<string, unknown>>;
  source_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedTodo = {
  id: string;
  domain_key: string | null;
  title: string;
  description: string;
  todo_type: string;
  owner_type: string;
  owner_ref: string | null;
  due_at: string | null;
  priority: string;
  status: string;
  source_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedContact = {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  linkedin: string | null;
  organization_entity_id: string | null;
  summary: string | null;
  origination: string | null;
  last_contact_at: string | null;
  scheduled_event_ids: string[];
  source_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedEntity = {
  id: string;
  name: string;
  website: string | null;
  summary: string | null;
  source_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedIdea = {
  id: string;
  domain_key: string | null;
  title: string;
  content: string;
  status: string;
  source_refs: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
  metadata: Record<string, unknown>;
  created_at: string | null;
};

export type RoutedObjectSurface = "calendar" | "contacts" | "todos" | "organizations" | "ideas";
export type ActiveSurface =
  | "dashboard"
  | "run-log"
  | "workflows"
  | "reports"
  | "skills"
  | "domain"
  | "memory"
  | "tools"
  | RoutedObjectSurface;
export type RoutedObjectRecord = RoutedEvent | RoutedTodo | RoutedContact | RoutedEntity | RoutedIdea;

export type WorkflowRunLogEntry = {
  id: string;
  workflow_run_id: string;
  workflow_definition_id: string | null;
  parent_task_id: string | null;
  conversation_id: string | null;
  domain_id: string | null;
  domain_key: string | null;
  status: string;
  title: string;
  summary: string;
  run_started_at: string | null;
  run_completed_at: string | null;
  agent_work: Array<Record<string, unknown>>;
  report_ids: string[];
  routed_item_ids: string[];
  artifact_ids: string[];
  notification_ids: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type WorkflowReport = {
  id: string;
  task_id: string;
  domain_id: string | null;
  domain_key: string | null;
  title: string;
  summary: string | null;
  source_type: string;
  archived: boolean;
  body_markdown?: string;
  created_at: string;
  updated_at: string;
};

export type RetrievedMemory = MemoryItem & {
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

export type AgentTool = {
  key: string;
  name: string;
  permission: string;
  description: string;
  connection_id: string | null;
  auth_type: string | null;
};

export type AgentSkill = {
  key: string;
  name: string;
  description: string;
  category: string;
  instruction: string;
  domain_key: string | null;
};

export type AgentSpec = {
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
  allowed_skills: AgentSkill[];
  is_active: boolean;
  current_action: string | null;
  scheduled_actions: Array<Record<string, unknown>>;
};

export type AgentTask = {
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

export type DomainContext = {
  id: string;
  key: string;
  name: string;
  context: string;
  is_active: boolean;
};

export type ToolRegistryItem = {
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

export type ToolConnection = {
  id: string;
  domain_key: string;
  tool_key: string;
  display_name: string;
  auth_type: string;
  config: Record<string, unknown>;
  is_active: boolean;
};

export type SkillRegistryItem = {
  id: string;
  key: string;
  name: string;
  description: string | null;
  category: string;
  instruction: string;
  domain_key: string | null;
  is_active: boolean;
  authorized_agents: Array<{
    agent_key: string;
    agent_name: string;
    domain_key: string;
    permission: string;
  }>;
};

export type PromptPackage = {
  assembled_prompt: string;
  memory_context: {
    included_count: number;
    semantic_status: string;
  };
};

export type AgentRun = {
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

export type MaestroIntent = {
  type: string;
  summary: string;
  target: string;
  domain_key: string | null;
  priority: string;
  action: string | null;
};

export type MaestroSubtask = {
  agent_key: string;
  agent_name: string;
  domain_key: string;
  objective: string;
  expected_output: string;
  priority: string;
  rationale: string | null;
  work_item_ids: string[] | null;
  depends_on_work_item_ids: string[] | null;
  required_skills?: string[] | null;
  model_profile?: string | null;
  model_tier?: string;
  model_rationale?: string | null;
};

export type MaestroWorkItem = {
  id: string;
  type: string;
  title: string;
  description: string;
  domain_key: string | null;
  priority: string;
  required_capabilities: string[];
  required_tools: string[];
  required_skills: string[];
  model_profile: string | null;
  model_tier: string;
  model_rationale: string;
  dependencies: string[];
  needs_agent: boolean;
  needs_user_input: boolean;
  blocks_execution: boolean;
  can_log_directly: boolean;
  suggested_agent_keys: string[];
  expected_output: string;
  rationale: string;
};

export type MaestroQueueItem = {
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

export type MaestroPlan = {
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
  is_routing_only: boolean;
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

export type MaestroRun = {
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

export type MaestroRespond = {
  kind: "chat_only" | "planned" | "refined" | "rfi_answered" | "routed" | "pending";
  classification: string;
  message: string;
  plan: MaestroPlan | null;
  chat_plan: MaestroPlan | null;
  active_plan: MaestroPlan | null;
  channel_context?: {
    scope: string;
    topic_id?: string | null;
    topic_title?: string | null;
    started_new_topic?: boolean;
    reason?: string;
  };
  conversation: MaestroSessionSummary | null;
};

export type MaestroToolCallResponse = {
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
