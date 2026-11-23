/**
 * Shared typed API client for AATM's data-driven screens.
 *
 * Thin, type-only wrapper around the existing `apiFetch` helper in
 * `../lib/api` — it adds the shared endpoint methods the upcoming
 * command-bridge / profiles / reports screens will use, so every screen
 * calls the backend through one place and gets typed responses instead
 * of re-writing fetch logic (or reaching for `any`) per screen.
 *
 * Only the client lives here — screen implementations belong to their
 * own pages/components in later waves.
 */
import { ApiError, apiFetch } from '../lib/api'
import type {
  AgentRecord,
  ApprovalRequest,
  BridgeArenaStatus,
  BridgeChatMessage,
  BridgeLiveFrame,
  BridgeSceneData,
  DashboardOverview,
  HealthStatus,
  HomeOverview,
  EmployeeListResponse,
  ProjectRecord,
  ProjectTimeline,
  ProfileGate,
  PullRequestRecord,
  ProviderRecord,
  BrowserRecord,
  ReportRecord,
} from './types'

export { ApiError }

function errorFromResponse(status: number, message: string): ApiError {
  return new ApiError(message, status)
}

/** Shared dashboard/overview endpoint. */
export const dashboardApi = {
  overview: () => apiFetch<DashboardOverview>('/dashboard/overview'),
  home: () => apiFetch<HomeOverview>('/dashboard/home'),
  employees: () => apiFetch<EmployeeListResponse>('/dashboard/employees'),
}

/** Shared agents endpoint. */
export const agentsApi = {
  list: () => apiFetch<AgentRecord[]>('/agents'),
}

/** Shared project + pull-request endpoints. */
export const projectsApi = {
  list: () => apiFetch<ProjectRecord[]>('/projects'),
  timeline: (projectId: string) => apiFetch<ProjectTimeline>(`/projects/${projectId}/timeline`),
}

export const pullRequestsApi = {
  list: () => apiFetch<PullRequestRecord[]>('/reviews/pull-requests'),
}

/** Shared approvals endpoint. */
export const approvalsApi = {
  list: (status = 'pending') => apiFetch<ApprovalRequest[]>(`/approvals?status=${status}`),
}

/** Shared platform-health endpoint. */
export const healthApi = {
  status: () => apiFetch<HealthStatus>('/api/health'),
}

/** Shared arena (gateway/profile) endpoints for the bridge. */
export const arenaApi = {
  status: () => apiFetch<BridgeArenaStatus>('/arena/connector/status'),
  profiles: () => apiFetch<ProfileGate>('/arena/profiles'),
}

/** Shared chat endpoint for the bridge. */
export const chatApi = {
  list: () => apiFetch<BridgeChatMessage[]>('/chat'),
}

/** Shared infra endpoints (providers, browsers) for infra/reports screens. */
export const infraApi = {
  providers: () => apiFetch<ProviderRecord[]>('/providers'),
  browsers: () => apiFetch<BrowserRecord[]>('/browsers'),
}

/** Shared reports endpoint for the upcoming reports screen. */
export const reportsApi = {
  list: () => apiFetch<ReportRecord[]>('/reports'),
}

/**
 * Build the aggregate bridge scene snapshot from the shared endpoint
 * methods. Each source fails open (an empty list / null) so one broken
 * panel never blanks the whole bridge — callers decide how to render
 * the empty/error states.
 */
export async function loadBridgeScene(): Promise<BridgeSceneData> {
  const [agents, employees, home, projects, arena, health, prs, approvals, chats, profiles] = await Promise.all([
    agentsApi.list().catch(() => [] as AgentRecord[]),
    dashboardApi.employees().catch(() => null),
    dashboardApi.home().catch(() => null),
    projectsApi.list().catch(() => [] as ProjectRecord[]),
    arenaApi.status().catch(() => null),
    healthApi.status().catch(() => null),
    pullRequestsApi.list().catch(() => [] as PullRequestRecord[]),
    approvalsApi.list().catch(() => [] as ApprovalRequest[]),
    chatApi.list().catch(() => [] as BridgeChatMessage[]),
    arenaApi.profiles().catch(() => ({ gate: 'unavailable', profiles: [] }) as ProfileGate),
  ])

  const employeeList = employees?.employees ?? []
  const projectList = Array.isArray(projects) ? projects : []
  const projectId = projectList[0]?.id ?? null
  const timeline = projectId ? await projectsApi.timeline(projectId).catch(() => null) : null
  const chatList = Array.isArray(chats) ? chats : []

  return {
    agents: Array.isArray(agents) ? agents.filter((agent) => agent?.status !== 'retired') : [],
    employees: employeeList,
    home,
    projects: projectList,
    projectId,
    timeline,
    prs,
    approvals,
    chats: chatList,
    arena,
    health,
    loading: false,
    error: null,
  }
}

/** Convenience typed fetch that converts non-ok responses to {@link ApiError}. */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    return await apiFetch<T>(path, init)
  } catch (error: any) {
    const status = typeof error?.status === 'number' ? error.status : 0
    throw error instanceof ApiError ? error : errorFromResponse(status, error?.message ?? 'Request failed')
  }
}

export type { BridgeLiveFrame }
