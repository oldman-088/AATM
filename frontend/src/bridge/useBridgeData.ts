import { useCallback, useEffect, useRef, useState } from 'react'
import { LIVE_EVENT } from '../views/DashboardViews'
import { apiFetch } from '../lib/api'

export type BridgeData = {
  agents: any[]
  employees: Record<string, any>
  home: any
  projects: any[]
  projectId: string | null
  timeline: any
  prs: any[]
  approvals: any[]
  chats: any[]
  arena: any
  arenaProfiles: Record<string, string>
  health: any
  loading: boolean
  error: string | null
}

const EMPTY: BridgeData = {
  agents: [],
  employees: {},
  home: null,
  projects: [],
  projectId: null,
  timeline: null,
  prs: [],
  approvals: [],
  chats: [],
  arena: null,
  arenaProfiles: {},
  health: null,
  loading: true,
  error: null,
}

const REFRESH_EVENTS = new Set([
  'task.updated',
  'task.created',
  'agent.heartbeat',
  'approval.requested',
  'approval.merge_result',
  'approval.scale_result',
  'chat.message',
  'scaling.proposed',
  'scaling.workers_added',
  'scaling.workers_retired',
  'arena.needs_human',
  'arena.resumed',
  'project.completed',
  'github.webhook',
])
const REFRESH_THROTTLE_MS = 1200

/** Everything the Bridge renders comes from these real endpoints — nothing is synthesised. */
export function useBridgeData() {
  const [data, setData] = useState<BridgeData>(EMPTY)
  const selectedProject = useRef<string | null>(null)
  const lastLoad = useRef(0)

  const load = useCallback(async () => {
    lastLoad.current = Date.now()
    try {
      const [agents, employees, home, projects, arena, health, prs, approvals, chats, profileCards] = await Promise.all([
        apiFetch<any[]>('/agents').catch(() => []),
        apiFetch<any>('/dashboard/employees').catch(() => null),
        apiFetch<any>('/dashboard/home').catch(() => null),
        apiFetch<any[]>('/projects').catch(() => []),
        apiFetch<any>('/arena/connector/status').catch(() => null),
        apiFetch<any>('/api/health').catch(() => null),
        apiFetch<any[]>('/reviews/pull-requests').catch(() => []),
        apiFetch<any[]>('/approvals?status=pending').catch(() => []),
        apiFetch<any[]>('/chat').catch(() => []),
        apiFetch<any>('/arena/profiles').catch(() => []),
      ])

      const projectList = Array.isArray(projects) ? projects : []
      const projectId =
        (selectedProject.current && projectList.some((p) => p.id === selectedProject.current) ? selectedProject.current : null) ??
        projectList[0]?.id ??
        null
      selectedProject.current = projectId
      const timeline = projectId ? await apiFetch<any>(`/projects/${projectId}/timeline`).catch(() => null) : null

      const employeeMap: Record<string, any> = {}
      for (const employee of Array.isArray(employees?.employees) ? employees.employees : []) {
        employeeMap[employee.id] = employee
      }
      const profileMap: Record<string, string> = {}
      // `/arena/profiles` returns `{gate, profiles}`; accept a bare list too so both shapes work.
      const profileList = Array.isArray(profileCards)
        ? profileCards
        : Array.isArray(profileCards?.profiles)
          ? profileCards.profiles
          : []
      for (const card of profileList) {
        if (card?.agent_id && card?.profile_id) profileMap[card.agent_id] = card.profile_id
      }

      setData({
        // Paused/disabled agents stay on the bridge (their seat shows `paused`); only retired ones leave.
        agents: Array.isArray(agents) ? agents.filter((agent) => agent?.status !== 'retired') : [],
        employees: employeeMap,
        home,
        projects: projectList,
        projectId,
        timeline,
        prs: Array.isArray(prs) ? prs : [],
        approvals: Array.isArray(approvals) ? approvals : [],
        chats: Array.isArray(chats) ? chats : [],
        arena,
        arenaProfiles: profileMap,
        health,
        loading: false,
        error: null,
      })
    } catch (error: any) {
      setData((previous) => ({ ...previous, loading: false, error: error?.message || 'load failed' }))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const onEvent = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (!detail || !REFRESH_EVENTS.has(String(detail.type))) return
      if (Date.now() - lastLoad.current < REFRESH_THROTTLE_MS) return
      void load()
    }
    window.addEventListener(LIVE_EVENT, onEvent)
    return () => window.removeEventListener(LIVE_EVENT, onEvent)
  }, [load])

  const setProjectId = useCallback(
    (projectId: string | null) => {
      selectedProject.current = projectId
      void load()
    },
    [load],
  )

  const modelStatus = data.health?.model_status
  const arenaGate = data.arena?.gate || data.arena || null
  const arenaLabel = arenaGate?.real_enabled
    ? 'approved'
    : arenaGate?.enabled
      ? 'simulated'
      : arenaGate
        ? 'unavailable'
        : 'unknown'
  const hasEngine = Boolean(modelStatus?.configured)
  const hasProject = data.projects.length > 0
  const hasDirective = data.chats.some((message) => message?.sender_type === 'user')
  const missionName = data.projects.find((project) => project.id === data.projectId)?.name || null

  return {
    ...data,
    reload: load,
    setProjectId,
    hasEngine,
    hasProject,
    hasDirective,
    missionName,
    modelStatus,
    arenaGate,
    arenaLabel,
  }
}
