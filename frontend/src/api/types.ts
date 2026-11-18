/**
 * Shared TypeScript types for AATM's data-driven screens.
 *
 * These mirror the JSON payloads served by the FastAPI backend
 * (see `backend/app/api/routes/`). They exist so the upcoming
 * command-bridge / profiles / reports screens share one typed contract
 * instead of re-declaring shapes (or falling back to `any`) per screen.
 *
 * Only types live here — screen implementations belong to their own
 * pages/components in later waves.
 */

// ---------------------------------------------------------------------------
// Dashboard / overview
// ---------------------------------------------------------------------------

export interface DashboardKpis {
  active_workers: number
  idle_workers: number
  total_agents: number
  total_tasks: number
  completed_tasks: number
  completion_rate: number
  healthy_providers: number
  total_providers: number
  running_browsers: number
  total_browsers: number
  pending_approvals: number
}

export interface DivisionProgress {
  code: string
  name: string
  progress_percent: number
  completed_tasks: number
  total_tasks: number
}

export interface ResourceSnapshot {
  cpu_count: number
  cpu_percent: number
  ram_used_gb: number
  ram_total_gb: number
  ram_percent: number
  disk_free_gb: number
  disk_percent: number
}

export interface SandboxStatus {
  status: string
  available: boolean
  mode: string | null
  image: string | null
  reason: string | null
}

export interface DashboardOverview {
  kpis: DashboardKpis
  divisions: DivisionProgress[]
  resources: ResourceSnapshot
  recommendations: string[]
  recent_events: Record<string, unknown>[]
  sandbox: SandboxStatus
}

// ---------------------------------------------------------------------------
// Agents / employees
// ---------------------------------------------------------------------------

export type AgentStatus = 'active' | 'idle' | 'paused' | 'retired' | string

export interface AgentRecord {
  id: string
  name: string
  status: AgentStatus
  role: string
  active_tasks?: number
  last_seen?: string | null
  model?: string | null
  capabilities?: string[]
  profile_id?: string | null
  tasks?: AgentTask[]
}

export interface EmployeeRecord {
  id: string
  agent_id: string
  name: string
  role: string
  skills: string[]
  cost_usd: number
  active_tasks?: number
  last_seen?: string | null
}

export interface EmployeeListResponse {
  employees: EmployeeRecord[]
  counters?: {
    active_employees: number
    total_employees: number
    tasks_by_status: Record<string, number>
    open_prs: number
    cost_today_usd: number
    daily_budget_usd: number
    needs_human: number
  }
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed' | string

export interface AgentTask {
  id: string
  agent_id: string
  title: string
  status: TaskStatus
  created_at?: string
  output?: string
}

// ---------------------------------------------------------------------------
// Projects / pull requests
// ---------------------------------------------------------------------------

export interface ProjectRecord {
  id: string
  name: string
  status: string
  description?: string
  created_at?: string
  updated_at?: string
}

export interface ProjectTimeline {
  project_id: string
  phases?: Record<string, unknown>[]
  events?: Record<string, unknown>[]
}

export interface PullRequestRecord {
  id: number
  number: number
  title: string
  state: string
  source: string
  repo: string
  url?: string
  updated_at?: string
}

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'executed' | 'failed' | string

export interface ApprovalRequest {
  id: string
  type: string
  status: ApprovalStatus
  requester?: string | null
  payload?: Record<string, unknown>
  created_at?: string
}

// ---------------------------------------------------------------------------
// Home / activity feed
// ---------------------------------------------------------------------------

export interface HomeOverview {
  events: HomeEvent[]
  prs: PullRequestRecord[]
  budget?: {
    cost_today_usd: number
    daily_budget_usd: number
  }
}

export interface HomeEvent {
  id: string
  type: string
  message: string
  timestamp: string
  agent_id?: string | null
  project_id?: string | null
}

// ---------------------------------------------------------------------------
// Health / platform status
// ---------------------------------------------------------------------------

export interface HealthStatus {
  status: string
  model_status?: {
    configured: boolean
    models: string[]
  } | null
  version?: string
}

export interface ProviderRecord {
  id: string
  name: string
  kind: string
  status: string
  monthly_budget_usd: number
  spend_usd: number
  enabled: boolean
}

export interface BrowserRecord {
  id: string
  name: string
  status: string
  active_tasks: number
  last_seen?: string | null
}

// ---------------------------------------------------------------------------
// Command bridge (shared shapes for the upcoming bridge screens)
// ---------------------------------------------------------------------------

export type BridgeStationKind = 'console' | 'focus' | string

export interface BridgeStationState {
  station: BridgeStationKind
  connected: boolean
  payload?: Record<string, unknown>
}

export interface BridgeLiveFrame {
  id: string
  type: string
  content: string
  timestamp: string
  agent_id?: string | null
}

export interface BridgeChatMessage {
  id: string
  sender_type: 'user' | 'agent' | string
  sender_name?: string | null
  content: string
  timestamp: string
}

export interface BridgeArenaStatus {
  enabled: boolean
  real_enabled: boolean
  gate?: string | null
  status?: string | null
}

/** Aggregated snapshot consumed by the bridge scene. */
export interface BridgeSceneData {
  agents: AgentRecord[]
  employees: EmployeeRecord[]
  home: HomeOverview | null
  projects: ProjectRecord[]
  projectId: string | null
  timeline: ProjectTimeline | null
  prs: PullRequestRecord[]
  approvals: ApprovalRequest[]
  chats: BridgeChatMessage[]
  arena: BridgeArenaStatus | null
  health: HealthStatus | null
  loading: boolean
  error: string | null
}

// ---------------------------------------------------------------------------
// Profiles / reports (shared shapes for the upcoming screens)
// ---------------------------------------------------------------------------

export interface AgentProfile {
  profile_id: string
  agent_id: string
  label: string
  model?: string | null
  skills?: string[]
  description?: string | null
}

export interface ProfileGate {
  gate: string
  profiles: AgentProfile[]
}

export interface ReportRecord {
  id: string
  name: string
  kind: string
  status: string
  created_at?: string
  summary?: string | null
  metrics?: Record<string, number>
}

// ---------------------------------------------------------------------------
// Generic list response envelope
// ---------------------------------------------------------------------------

export interface ListResponse<T> {
  items: T[]
}
