/**
 * NEXUS Command Center (owner spec §4): eight overview cards, the live operations feed,
 * the workforce overview and system health. Every value comes from a backend endpoint;
 * anything the backend does not measure says "Data unavailable".
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Bot,
  CheckCircle2,
  ClipboardCheck,
  Cpu,
  FolderKanban,
  HeartPulse,
  Maximize2,
  Pause,
  Play,
  Radio,
  Timer,
  XCircle,
} from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Button, cn } from '../../components/ui'
import {
  agentState,
  fill,
  PageHeader,
  SectionCard,
  STATE_TONE,
  StatCard,
  StatusDot,
  Unavailable,
  useRelativeTime,
} from '../../components/nexus'
import { LIVE_EVENT } from '../DashboardViews'
import type { HealthState } from '../../shell/NexusShell'
import type { Route } from '../../shell/sections'

const RUNNING = ['in_progress', 'assigned', 'in_review', 'pr_open', 'review']
const DONE = ['completed', 'done', 'merged', 'approved']
const FAILED = ['failed', 'changes_requested']

interface FeedEvent { id: string; kind: keyof ReturnType<typeof useI18n>['t']['nexus']['events']; text: string; at: string; tone: 'cyan' | 'success' | 'error' | 'warning' | 'violet' | 'muted' }

/** Turn a backend event (WebSocket) or trace (audit log) into a feed line. */
export function describeEvent(raw: any, agentsById: Record<string, any>): Omit<FeedEvent, 'id' | 'at'> | null {
  const type = String(raw?.type ?? raw?.action ?? '')
  const p = raw?.payload ?? raw ?? {}
  const agent = agentsById[p.agent_id ?? p.assigned_agent_id ?? raw?.agent_id ?? '']?.name ?? p.agent_id ?? raw?.agent_id ?? ''
  const subject = p.title ?? p.task_title ?? p.task_id ?? raw?.resource_id ?? ''
  const text = [agent, subject].filter(Boolean).join(' · ')
  if (type === 'agent.heartbeat' || type === 'browser.snapshot' || type === 'agent.iteration' || type === 'agent.code' || type === 'qa.self_healing') return null
  if (type === 'task.updated' || type === 'task.update') {
    const status = String(p.status ?? '')
    if (DONE.includes(status)) return { kind: 'taskCompleted', text, tone: 'success' }
    if (FAILED.includes(status)) return { kind: 'taskFailed', text: [text, p.error].filter(Boolean).join(' — '), tone: 'error' }
    if (p.assigned_agent_id || status === 'assigned') return { kind: 'taskAssigned', text, tone: 'cyan' }
    if (status === 'in_progress') return { kind: 'agentStarted', text, tone: 'cyan' }
    return { kind: 'taskUpdated', text: [text, status].filter(Boolean).join(' — '), tone: 'muted' }
  }
  if (type === 'task.created' || type === 'task.create') return { kind: 'taskAssigned', text, tone: 'cyan' }
  if (type.startsWith('approval.')) return { kind: 'approvalRequested', text: subject || type, tone: 'warning' }
  if (type.startsWith('project.') || type.startsWith('merge_queue.')) return { kind: 'projectUpdated', text: subject || type, tone: 'violet' }
  if (type === 'arena.needs_human' || type === 'arena.escalated' || type === 'security.emergency_stop' || raw?.severity === 'warning' || raw?.severity === 'critical') {
    return { kind: 'systemWarning', text: [text, p.reason ?? p.message].filter(Boolean).join(' — ') || type, tone: 'warning' }
  }
  if (type === 'chat.message' || type === 'manager.activity' || type === 'manager.review') return { kind: 'managerReply', text: text || type, tone: 'violet' }
  if (type.startsWith('arena.session') && p.phase === 'idle') return { kind: 'workerIdle', text, tone: 'muted' }
  return { kind: 'other', text: [type, text].filter(Boolean).join(' · '), tone: 'muted' }
}

function LiveFeed({ agents }: { agents: any[] }) {
  const { t } = useI18n()
  const c = t.nexus.cc
  const ago = useRelativeTime()
  const [events, setEvents] = useState<FeedEvent[]>([])
  const [paused, setPaused] = useState(false)
  const agentsById = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a])), [agents])

  useEffect(() => {
    let cancelled = false
    // The audit log is dominated by periodic self-healing entries, so read a wider window.
    apiFetch<any>('/traces?limit=200').then((data) => {
      if (cancelled) return
      const rows: FeedEvent[] = []
      for (const trace of data?.traces ?? []) {
        const d = describeEvent(trace, agentsById)
        if (d) rows.push({ ...d, id: trace.id, at: trace.timestamp })
      }
      setEvents((current) => (current.length ? current : rows.slice(0, 40)))
    }).catch(() => {})
    return () => { cancelled = true }
  }, [agentsById])

  useEffect(() => {
    if (paused) return
    const onEvent = (e: Event) => {
      const raw = (e as CustomEvent).detail
      const d = describeEvent(raw, agentsById)
      if (!d) return
      setEvents((list) => [{ ...d, id: raw?.event_id ?? `${Date.now()}-${Math.random()}`, at: raw?.timestamp ?? new Date().toISOString() }, ...list].slice(0, 60))
    }
    window.addEventListener(LIVE_EVENT, onEvent)
    return () => window.removeEventListener(LIVE_EVENT, onEvent)
  }, [paused, agentsById])

  return (
    <SectionCard testId="live-feed" className="flex min-h-[22rem] flex-col xl:col-span-2" bodyClassName="flex-1 min-h-0 p-0"
      title={<span className="flex items-center gap-2"><Radio className="h-4 w-4 text-nexus-cyan" />{c.feedTitle}</span>}
      actions={<>
        <StatusDot tone={paused ? 'muted' : 'cyan'} pulse={!paused} label={paused ? c.feedPaused : c.feedLive} />
        <Button size="sm" variant="ghost" onClick={() => setPaused((p) => !p)} aria-label={paused ? c.feedResume : c.feedPause} data-testid="feed-toggle">
          {paused ? <Play className="h-3.5 w-3.5 rtl:rotate-180" /> : <Pause className="h-3.5 w-3.5" />}
        </Button>
      </>}>
      {events.length === 0 ? (
        <p className="p-4 text-sm text-nexus-muted" data-testid="feed-empty">{c.feedEmpty}</p>
      ) : (
        <ol className="max-h-[26rem] divide-y divide-nexus-border/50 overflow-y-auto" aria-live="polite">
          {events.map((ev) => (
            <li key={ev.id} className="flex items-start gap-3 px-4 py-2.5 text-sm animate-nx-fade-in">
              <span className="mt-1.5"><StatusDot tone={ev.tone} /></span>
              <div className="min-w-0 flex-1">
                <p className="font-medium text-nexus-text">{t.nexus.events[ev.kind]}</p>
                {ev.text && <p className="truncate text-xs text-nexus-muted" title={ev.text}>{ev.text}</p>}
              </div>
              <time className="shrink-0 text-[11px] text-nexus-dim" dateTime={ev.at}>{ago(ev.at)}</time>
            </li>
          ))}
        </ol>
      )}
    </SectionCard>
  )
}

function WorkforceOverview({ agents, onNavigate }: { agents: any[]; onNavigate: (r: Route) => void }) {
  const { t, formatNumber } = useI18n()
  const states = ['working', 'idle', 'waiting', 'blocked', 'failed', 'offline'] as const
  const counts = useMemo(() => {
    const out: Record<string, number> = Object.fromEntries(states.map((s) => [s, 0]))
    for (const a of agents) {
      const s = agentState(a)
      out[s === 'paused' ? 'offline' : s] += 1
    }
    return out
  }, [agents])
  const total = Math.max(1, agents.length)
  const bar: Record<string, string> = {
    working: 'bg-nexus-cyan', idle: 'bg-nexus-success', waiting: 'bg-nexus-violet', blocked: 'bg-nexus-warning', failed: 'bg-nexus-error', offline: 'bg-nexus-dim',
  }
  return (
    <SectionCard testId="workforce-overview" title={<span className="flex items-center gap-2"><Bot className="h-4 w-4 text-nexus-cyan" />{t.nexus.cc.workforceTitle}</span>}
      actions={<Button size="sm" variant="ghost" onClick={() => onNavigate({ section: 'workforce', sub: 'agents' })}>{t.nexus.common.openSection}</Button>}>
      {agents.length === 0 ? <p className="text-sm text-nexus-muted">{t.nexus.cc.workforceEmpty}</p> : (
        <>
          <div className="mb-4 flex h-2.5 overflow-hidden rounded-full bg-nexus-raised" aria-hidden>
            {states.map((s) => counts[s] > 0 && <div key={s} className={bar[s]} style={{ width: `${(counts[s] / total) * 100}%` }} />)}
          </div>
          <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {states.map((s) => (
              <li key={s} className="flex items-center justify-between gap-2 rounded-lg border border-nexus-border/60 bg-nexus-bg/40 px-3 py-2" data-testid={`workforce-${s}`}>
                <StatusDot tone={STATE_TONE[s]} label={t.nexus.states[s]} />
                <span className="text-sm font-semibold tabular-nums text-nexus-text">{formatNumber(counts[s])}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </SectionCard>
  )
}

interface HealthCheck { id: string; label: string; ok: boolean | null; value: React.ReactNode }

function SystemHealth({ checks }: { checks: HealthCheck[] }) {
  const { t } = useI18n()
  const c = t.nexus.cc
  return (
    <SectionCard testId="system-health" title={<span className="flex items-center gap-2"><HeartPulse className="h-4 w-4 text-nexus-cyan" />{c.healthTitle}</span>}>
      <ul className="divide-y divide-nexus-border/50">
        {checks.map((check) => (
          <li key={check.id} className="flex items-center justify-between gap-3 py-2 text-sm" data-testid={`health-${check.id}`}>
            <span className="flex min-w-0 items-center gap-2">
              <StatusDot tone={check.ok === null ? 'muted' : check.ok ? 'success' : 'error'} />
              <span className="truncate text-nexus-text">{check.label}</span>
            </span>
            <span className="min-w-0 truncate text-end text-xs text-nexus-muted">{check.value}</span>
          </li>
        ))}
      </ul>
    </SectionCard>
  )
}

export function CommandCenter({ overview, agents, tasks, projects, approvals, devices, resources, health, onNavigate }: {
  overview: any; agents: any[]; tasks: any[]; projects: any[]; approvals: any[]; devices: any[]; resources?: any
  health: { state: HealthState; data: any }; onNavigate: (r: Route) => void
}) {
  const { t, formatNumber } = useI18n()
  const c = t.nexus.cc
  const [scaling, setScaling] = useState<any>(null)
  const [connector, setConnector] = useState<any>(null)

  const loadInfra = useCallback(() => {
    apiFetch<any>('/scaling/status').then(setScaling).catch(() => setScaling(null))
    apiFetch<any>('/arena/connector/status').then(setConnector).catch(() => setConnector(null))
  }, [])
  useEffect(() => {
    loadInfra()
    const id = window.setInterval(loadInfra, 30_000)
    return () => window.clearInterval(id)
  }, [loadInfra])

  const workers = agents.filter((a) => a.role !== 'manager')
  const working = workers.filter((a) => agentState(a) === 'working').length
  const idle = workers.filter((a) => agentState(a) === 'idle').length
  const running = tasks.filter((x) => RUNNING.includes(x.status)).length
  const queued = tasks.filter((x) => x.status === 'queued').length
  const done = tasks.filter((x) => DONE.includes(x.status)).length
  const failed = tasks.filter((x) => FAILED.includes(x.status)).length
  const completionRate = tasks.length ? Math.round((done / tasks.length) * 100) : 0
  const pending = approvals.filter((a) => String(a.status).toLowerCase() === 'pending').length
  const activeProjects = projects.filter((p) => (p.status ?? 'active') === 'active').length
  const kpis = overview?.kpis

  const bridgeProfiles: any[] = connector?.bridge?.profiles ?? []
  const bridgeConnected = bridgeProfiles.filter((p) => p.connected).length
  const local = devices.find((d) => d.device_type === 'local') ?? devices[0]

  const checks: HealthCheck[] = [
    { id: 'api', label: c.healthApi, ok: health.state === 'unknown' ? null : health.state !== 'down',
      value: health.data?.version ? `v${health.data.version}` : health.state === 'down' ? c.down : '—' },
    { id: 'database', label: c.healthDatabase, ok: overview ? true : health.state === 'down' ? false : null,
      value: scaling?.database_backend ?? (overview ? c.healthy : <Unavailable />) },
    { id: 'queue', label: c.healthQueue, ok: scaling ? (scaling.queue?.dead_lettered ?? 0) === 0 : null,
      value: scaling ? fill(c.queueValue, { backend: scaling.queue?.backend ?? '—', n: scaling.queue?.dead_lettered ?? 0 }, formatNumber) : <Unavailable /> },
    { id: 'coordination', label: c.healthCoordination, ok: scaling ? !!scaling.coordination?.backend : null,
      value: scaling?.coordination?.backend ?? <Unavailable /> },
    { id: 'sandbox', label: c.healthSandbox, ok: health.data?.sandbox ? !!health.data.sandbox.available : null,
      value: health.data?.sandbox ? `${health.data.sandbox.status} · ${health.data.sandbox.mode}` : <Unavailable /> },
    { id: 'browsers', label: c.healthBrowsers, ok: kpis ? kpis.running_browsers > 0 || kpis.total_browsers === 0 : null,
      value: kpis ? fill(c.browsersValue, { a: kpis.running_browsers, b: kpis.total_browsers }, formatNumber) : <Unavailable /> },
    { id: 'models', label: c.healthModels, ok: kpis ? kpis.healthy_providers > 0 : null,
      value: kpis ? fill(c.providersValue, { a: kpis.healthy_providers, b: kpis.total_providers }, formatNumber) : <Unavailable /> },
    { id: 'bridge', label: c.healthBridge, ok: connector ? bridgeProfiles.length === 0 || bridgeConnected === bridgeProfiles.length : null,
      value: connector ? fill(c.bridgeValue, { a: bridgeConnected, b: bridgeProfiles.length }, formatNumber) : <Unavailable /> },
    // Host CPU/memory come from /qa/summary (psutil on the backend host); before it answers, device capacity is shown.
    { id: 'cpu', label: c.healthCpu, ok: resources ? resources.cpu_percent < 90 : null,
      value: resources ? fill(c.cpuValue, { p: resources.cpu_percent, n: resources.cpu_count }, formatNumber)
        : <Unavailable text={local?.cpu_cores ? `${t.nexus.common.dataUnavailable} · ${formatNumber(local.cpu_cores)} cores` : undefined} /> },
    { id: 'memory', label: c.healthMemory, ok: resources ? resources.ram_percent < 90 : null,
      value: resources ? fill(c.memoryValue, { a: resources.ram_used_gb, b: resources.ram_total_gb, p: resources.ram_percent }, formatNumber)
        : <Unavailable text={local?.ram_gb ? `${t.nexus.common.dataUnavailable} · ${formatNumber(local.ram_gb)} GB` : undefined} /> },
  ]
  const measured = checks.filter((x) => x.ok !== null)
  const healthyCount = measured.filter((x) => x.ok).length
  const healthTone = measured.length === 0 ? 'muted' : healthyCount === measured.length ? 'success' : healthyCount >= measured.length / 2 ? 'warning' : 'error'
  const healthWord = healthTone === 'success' ? c.healthy : healthTone === 'warning' ? c.degraded : healthTone === 'error' ? c.down : t.nexus.common.loading

  return (
    <div className="space-y-6" data-testid="command-center">
      <PageHeader title={c.title} subtitle={c.subtitle} icon={<Activity className="h-6 w-6" />}
        actions={<Button variant="secondary" onClick={() => onNavigate({ section: 'command', sub: 'bridge' })} data-testid="open-bridge" title={c.bridgeHint}>
          <Maximize2 className="h-4 w-4" />{c.openBridge}
        </Button>} />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" data-testid="kpi-grid">
        <StatCard testId="kpi-total-agents" label={c.kpi.totalAgents} value={formatNumber(agents.length)} icon={<Bot className="h-4 w-4" />}
          hint={fill(c.kpiHint.totalAgents, { n: agents.filter((a) => a.is_enabled !== false).length }, formatNumber)}
          onClick={() => onNavigate({ section: 'workforce', sub: 'agents' })} />
        <StatCard testId="kpi-active-workers" label={c.kpi.activeWorkers} value={formatNumber(working)} tone="cyan" icon={<Activity className="h-4 w-4" />}
          hint={fill(c.kpiHint.activeWorkers, { n: idle }, formatNumber)} status={working > 0 ? <StatusDot tone="cyan" pulse /> : undefined}
          onClick={() => onNavigate({ section: 'workforce', sub: 'agents' })} />
        <StatCard testId="kpi-running-tasks" label={c.kpi.runningTasks} value={formatNumber(running)} tone="violet" icon={<Timer className="h-4 w-4" />}
          hint={fill(c.kpiHint.runningTasks, { n: queued }, formatNumber)} onClick={() => onNavigate({ section: 'projects', sub: 'tasks' })} />
        <StatCard testId="kpi-completed-tasks" label={c.kpi.completedTasks} value={formatNumber(done)} tone="success" icon={<CheckCircle2 className="h-4 w-4" />}
          hint={fill(c.kpiHint.completedTasks, { n: completionRate }, formatNumber)} onClick={() => onNavigate({ section: 'projects', sub: 'tasks' })} />
        <StatCard testId="kpi-failed-tasks" label={c.kpi.failedTasks} value={formatNumber(failed)} tone={failed ? 'error' : 'muted'} icon={<XCircle className="h-4 w-4" />}
          hint={failed ? c.kpiHint.failedTasks : c.kpiHint.failedTasksNone} onClick={() => onNavigate({ section: 'projects', sub: 'tasks' })} />
        <StatCard testId="kpi-pending-approvals" label={c.kpi.pendingApprovals} value={formatNumber(pending)} tone={pending ? 'warning' : 'muted'} icon={<ClipboardCheck className="h-4 w-4" />}
          hint={pending ? c.kpiHint.pendingApprovals : c.kpiHint.pendingApprovalsNone} onClick={() => onNavigate({ section: 'executive', sub: 'approvals' })} />
        <StatCard testId="kpi-active-projects" label={c.kpi.activeProjects} value={formatNumber(activeProjects)} tone="violet" icon={<FolderKanban className="h-4 w-4" />}
          hint={fill(c.kpiHint.activeProjects, { n: projects.length }, formatNumber)} onClick={() => onNavigate({ section: 'projects', sub: 'projectsList' })} />
        <StatCard testId="kpi-system-health" label={c.kpi.systemHealth} value={<span className={cn(healthTone === 'success' ? 'text-nexus-success' : healthTone === 'warning' ? 'text-nexus-warning' : healthTone === 'error' ? 'text-nexus-error' : 'text-nexus-muted')}>{healthWord}</span>}
          tone={healthTone as any} icon={<Cpu className="h-4 w-4" />}
          hint={fill(c.kpiHint.systemHealth, { ok: healthyCount, total: measured.length }, formatNumber)} onClick={() => onNavigate({ section: 'integrations', sub: 'integrationsOverview' })} />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <LiveFeed agents={agents} />
        <div className="space-y-4">
          <WorkforceOverview agents={workers.length ? workers : agents} onNavigate={onNavigate} />
        </div>
      </div>
      <SystemHealth checks={checks} />
    </div>
  )
}
