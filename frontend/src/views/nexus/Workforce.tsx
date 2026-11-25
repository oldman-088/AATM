/**
 * NEXUS AI Workforce (owner spec §5): a profile card per agent and a details drawer with working actions.
 *   View profile  -> drawer (profile tab)
 *   Assign task   -> POST /arena/agents/{id}/assign-task   (queued implementation tasks of active projects)
 *   Edit          -> PATCH /agents/{id}  (name, model)
 *   Pause/Resume  -> PATCH /agents/{id}  {is_enabled}   (the scheduler gives no work to a disabled agent)
 *   Activity      -> GET /traces?agent_id=
 *   Performance   -> tasks_completed / tasks_failed / performance_score from /agents and cost from /dashboard/employees
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Bot, Crown, Pause, Pencil, Play, Search, SendHorizontal, UserRound } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Button, cn, Input, Select } from '../../components/ui'
import {
  agentState,
  Drawer,
  Field,
  fill,
  PageHeader,
  Ring,
  STATE_TONE,
  StatusDot,
  SubTabs,
  useAction,
  useRelativeTime,
} from '../../components/nexus'

export function departmentOf(agent: any, templates: any[]): string {
  const tpl = templates.find((x) => x.id === agent?.role_template)
  return tpl?.title ?? agent?.specialty?.split(',')[0]?.trim() ?? ''
}

function initials(name: string) {
  return name.replace(/[—–-].*$/, '').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase() || '·'
}

export function AgentAvatar({ agent, size = 'md' }: { agent: any; size?: 'sm' | 'md' | 'lg' }) {
  const state = agentState(agent)
  const ring = { working: 'ring-nexus-cyan', idle: 'ring-nexus-success', waiting: 'ring-nexus-violet', blocked: 'ring-nexus-warning', failed: 'ring-nexus-error', offline: 'ring-nexus-dim', paused: 'ring-nexus-dim' }[state]
  const dims = size === 'lg' ? 'h-14 w-14 text-base' : size === 'sm' ? 'h-8 w-8 text-[11px]' : 'h-11 w-11 text-sm'
  return (
    <span className={cn('relative inline-flex shrink-0 items-center justify-center rounded-xl font-semibold text-white ring-2 ring-offset-2 ring-offset-nexus-card', dims, ring)}
      style={{ background: `linear-gradient(135deg, ${agent?.color ?? '#00D4FF'}, #8B5CF6)` }} aria-hidden>
      {agent?.role === 'manager' ? <Crown className="h-1/2 w-1/2" /> : initials(agent?.name ?? '')}
    </span>
  )
}

function successRate(agent: any): number | null {
  const done = Number(agent?.tasks_completed ?? 0)
  const failed = Number(agent?.tasks_failed ?? 0)
  return done + failed > 0 ? Math.round((done / (done + failed)) * 100) : null
}

function AgentCard({ agent, task, department, onOpen, onAssign, onTogglePause, pausing }: {
  agent: any; task: any; department: string; onOpen: (tab?: string) => void; onAssign: () => void; onTogglePause: () => void; pausing: boolean
}) {
  const { t, formatNumber } = useI18n()
  const w = t.nexus.workforce
  const ago = useRelativeTime()
  const state = agentState(agent)
  const rate = successRate(agent)
  const isManager = agent.role === 'manager'
  return (
    <article data-testid={`agent-card-${agent.id}`} className="flex flex-col rounded-xl border border-nexus-border/80 bg-nexus-card p-4 shadow-card transition-colors hover:border-nexus-cyan/40">
      <div className="flex items-start gap-3">
        <AgentAvatar agent={agent} />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold text-nexus-text" title={agent.name}>{agent.name}</h3>
          <p className="truncate text-xs text-nexus-muted">{isManager ? w.manager : w.worker}{department ? ` · ${department}` : ''}</p>
          <div className="mt-1.5"><StatusDot tone={STATE_TONE[state]} pulse={state === 'working'} label={t.nexus.states[state]} /></div>
        </div>
      </div>
      <div className="mt-3 rounded-lg border border-nexus-border/60 bg-nexus-bg/40 px-3 py-2 text-xs">
        <span className="text-nexus-dim">{w.currentTask}: </span>
        <span className="text-nexus-text">{task?.title ?? w.noTask}</span>
      </div>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-center text-xs">
        <div><dt className="text-nexus-dim">{w.completed}</dt><dd className="mt-0.5 font-semibold tabular-nums text-nexus-success">{formatNumber(agent.tasks_completed ?? 0)}</dd></div>
        <div><dt className="text-nexus-dim">{w.failedCount}</dt><dd className="mt-0.5 font-semibold tabular-nums text-nexus-error">{formatNumber(agent.tasks_failed ?? 0)}</dd></div>
        <div><dt className="text-nexus-dim">{w.successRate}</dt><dd className="mt-0.5 font-semibold tabular-nums text-nexus-text">{rate === null ? '—' : `${formatNumber(rate)}%`}</dd></div>
      </dl>
      <p className="mt-3 text-[11px] text-nexus-dim">{w.lastActivity}: {ago(agent.last_heartbeat)}</p>
      <div className="mt-3 flex flex-wrap gap-1.5 border-t border-nexus-border/60 pt-3">
        <Button size="sm" variant="secondary" onClick={() => onOpen('profile')} data-testid={`agent-view-${agent.id}`}><UserRound className="h-3.5 w-3.5" />{w.viewProfile}</Button>
        {!isManager && <Button size="sm" variant="secondary" onClick={onAssign} data-testid={`agent-assign-${agent.id}`}><SendHorizontal className="h-3.5 w-3.5 rtl:rotate-180" />{w.assignTask}</Button>}
        {!isManager && (
          <Button size="sm" variant="ghost" onClick={onTogglePause} disabled={pausing} aria-busy={pausing} data-testid={`agent-pause-${agent.id}`}>
            {agent.is_enabled === false ? <><Play className="h-3.5 w-3.5 rtl:rotate-180" />{w.resume}</> : <><Pause className="h-3.5 w-3.5" />{w.pause}</>}
          </Button>
        )}
      </div>
    </article>
  )
}

function AgentDrawer({ agent, tab, onTab, onClose, tasks, projects, department, employees, onChanged, agents }: {
  agent: any; tab: string; onTab: (t: string) => void; onClose: () => void; tasks: any[]; projects: any[]; department: string
  employees: any[]; onChanged: () => void; agents: any[]
}) {
  const { t, formatNumber } = useI18n()
  const w = t.nexus.workforce
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const [traces, setTraces] = useState<any[] | null>(null)
  const [name, setName] = useState(agent.name)
  const [model, setModel] = useState(agent.model_name ?? '')
  const [taskId, setTaskId] = useState('')

  useEffect(() => { setName(agent.name); setModel(agent.model_name ?? '') }, [agent])
  useEffect(() => {
    if (tab !== 'activity') return
    setTraces(null)
    apiFetch<any>(`/traces?agent_id=${encodeURIComponent(agent.id)}&limit=50`).then((d) => setTraces(d?.traces ?? [])).catch(() => setTraces([]))
  }, [tab, agent.id])

  const activeProjectIds = new Set(projects.filter((p) => (p.status ?? 'active') === 'active').map((p) => p.id))
  const assignable = tasks.filter((x) => x.status === 'queued' && (x.task_type ?? 'implementation') === 'implementation' && activeProjectIds.has(x.project_id))
  const current = tasks.find((x) => x.id === agent.current_task_id)
  const myTasks = tasks.filter((x) => x.assigned_agent_id === agent.id)
  const employee = employees.find((e) => e.id === agent.id)
  const manager = agents.find((a) => a.id === agent.reports_to)
  const rate = successRate(agent)

  const tabs = [
    { id: 'profile', label: w.profileTab },
    { id: 'assign', label: w.assignTask },
    { id: 'edit', label: w.edit },
    { id: 'activity', label: w.activity },
    { id: 'performance', label: w.performance },
  ].filter((x) => agent.role !== 'manager' || x.id !== 'assign')

  return (
    <Drawer open title={<span className="flex items-center gap-3"><AgentAvatar agent={agent} size="sm" />{agent.name}</span>} onClose={onClose} testId="agent-drawer" wide>
      <SubTabs tabs={tabs} active={tab} onChange={onTab} label={agent.name} />
      <div className="mt-4">
        {tab === 'profile' && (
          <dl data-testid="agent-profile">
            <Field label={t.nexus.common.status}><StatusDot tone={STATE_TONE[agentState(agent)]} label={t.nexus.states[agentState(agent)]} /></Field>
            <Field label={w.role}>{agent.role === 'manager' ? w.manager : w.worker}</Field>
            <Field label={w.department}>{department || t.nexus.common.none}</Field>
            <Field label={w.reportsTo}>{manager?.name ?? t.nexus.common.none}</Field>
            <Field label={w.currentTask}>{current?.title ?? w.noTask}</Field>
            <Field label={w.provider}>{employee?.provider ?? agent.provider_id ?? '—'}</Field>
            <Field label={w.model}><span dir="ltr">{agent.model_name ?? '—'}</span></Field>
            <Field label={w.browserProfile}><span dir="ltr">{agent.browser_profile_id ?? '—'}</span></Field>
            <Field label={w.branch}><span dir="ltr">{agent.git_branch ?? '—'}</span></Field>
            <Field label={w.workspace}><span dir="ltr" className="break-all">{agent.workspace_path ?? '—'}</span></Field>
            <Field label={w.skills}>{(agent.skills ?? []).join(', ') || '—'}</Field>
            <Field label={w.lastActivity}>{ago(agent.last_heartbeat)}</Field>
          </dl>
        )}

        {tab === 'assign' && (
          <form className="space-y-3" data-testid="agent-assign-form" onSubmit={(e) => {
            e.preventDefault()
            if (!taskId) return
            run('assign', () => apiFetch(`/arena/agents/${agent.id}/assign-task`, { method: 'POST', body: JSON.stringify({ task_id: taskId }) }),
              { success: fill(w.assignDone, { name: agent.name }), onDone: () => { setTaskId(''); onChanged() } })
          }}>
            <p className="text-sm text-nexus-muted">{fill(w.assignTitle, { name: agent.name })}</p>
            {assignable.length === 0 ? <p className="rounded-lg border border-nexus-border bg-nexus-bg/40 p-3 text-sm text-nexus-muted" data-testid="assign-none">{w.assignNone}</p> : (
              <>
                <Select value={taskId} onChange={(e) => setTaskId(e.target.value)} className="w-full" aria-label={w.assignPick} data-testid="assign-select">
                  <option value="">{w.assignPick}</option>
                  {assignable.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
                </Select>
                <Button type="submit" disabled={!taskId || isBusy('assign')} aria-busy={isBusy('assign')} data-testid="assign-submit">
                  <SendHorizontal className="h-4 w-4 rtl:rotate-180" />{isBusy('assign') ? t.nexus.common.working : w.assignSubmit}
                </Button>
              </>
            )}
          </form>
        )}

        {tab === 'edit' && (
          <form className="space-y-3" data-testid="agent-edit-form" onSubmit={(e) => {
            e.preventDefault()
            const body: Record<string, string> = {}
            if (name.trim() && name.trim() !== agent.name) body.name = name.trim()
            if (model.trim() !== (agent.model_name ?? '')) body.model_name = model.trim()
            if (!Object.keys(body).length) return
            run('edit', () => apiFetch(`/agents/${agent.id}`, { method: 'PATCH', body: JSON.stringify(body) }), { success: w.editSaved, onDone: onChanged })
          }}>
            <label className="block text-sm"><span className="mb-1 block text-nexus-muted">{w.editName}</span>
              <Input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} data-testid="edit-name" /></label>
            <label className="block text-sm"><span className="mb-1 block text-nexus-muted">{w.model}</span>
              <Input value={model} onChange={(e) => setModel(e.target.value)} dir="ltr" data-testid="edit-model" /></label>
            <Button type="submit" disabled={isBusy('edit')} aria-busy={isBusy('edit')} data-testid="edit-save"><Pencil className="h-4 w-4" />{isBusy('edit') ? t.nexus.common.saving : t.nexus.common.save}</Button>
          </form>
        )}

        {tab === 'activity' && (
          <div data-testid="agent-activity">
            {traces === null ? <p className="text-sm text-nexus-muted">{t.nexus.common.loading}</p>
              : traces.length === 0 ? <p className="text-sm text-nexus-muted">{w.noActivity}</p> : (
                <ol className="space-y-1.5">
                  {traces.map((tr) => (
                    <li key={tr.id} className="flex items-start justify-between gap-3 rounded-lg border border-nexus-border/60 bg-nexus-bg/40 px-3 py-2 text-xs">
                      <span className="min-w-0"><span className="font-medium text-nexus-text" dir="ltr">{tr.action}</span>
                        {tr.resource_type && <span className="text-nexus-dim"> · {tr.resource_type}</span>}</span>
                      <time className="shrink-0 text-nexus-dim" dateTime={tr.timestamp}>{ago(tr.timestamp)}</time>
                    </li>
                  ))}
                </ol>
              )}
          </div>
        )}

        {tab === 'performance' && (
          <div className="space-y-4" data-testid="agent-performance">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Ring value={rate} label={w.successRate} tone="success" />
              <Ring value={agent.performance_score == null ? null : Math.min(100, Number(agent.performance_score))} label={w.score} tone="violet" />
              <div className="col-span-2 space-y-2 text-sm sm:col-span-1">
                <Field label={w.completed}>{formatNumber(agent.tasks_completed ?? 0)}</Field>
                <Field label={w.failedCount}>{formatNumber(agent.tasks_failed ?? 0)}</Field>
                <Field label={t.nexus.analytics.cost}>{employee ? `$${Number(employee.cost_usd ?? 0).toFixed(2)}` : t.nexus.common.dataUnavailable}</Field>
              </div>
            </div>
            <div>
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-nexus-dim">{fill(w.tasksTotal, { n: myTasks.length }, formatNumber)}</h4>
              <ul className="space-y-1">
                {myTasks.slice(0, 12).map((x) => (
                  <li key={x.id} className="flex items-center justify-between gap-2 rounded-lg bg-nexus-bg/40 px-3 py-1.5 text-xs">
                    <span className="truncate text-nexus-text">{x.title}</span><span className="shrink-0 text-nexus-dim">{x.status}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </div>
    </Drawer>
  )
}

export function WorkforceView({ agents, tasks, projects, templates, onReload, openAgentId, onOpenAgent }: {
  agents: any[]; tasks: any[]; projects: any[]; templates: any[]; onReload: () => void
  openAgentId: string | null; onOpenAgent: (id: string | null) => void
}) {
  const { t, formatNumber } = useI18n()
  const w = t.nexus.workforce
  const { run, isBusy } = useAction()
  const [query, setQuery] = useState('')
  const [state, setState] = useState('all')
  const [tab, setTab] = useState('profile')
  const [employees, setEmployees] = useState<any[]>([])

  const loadEmployees = useCallback(() => {
    apiFetch<any>('/dashboard/employees').then((d) => setEmployees(d?.employees ?? [])).catch(() => setEmployees([]))
  }, [])
  useEffect(() => { loadEmployees() }, [loadEmployees, agents])

  const visible = useMemo(() => agents.filter((a) => {
    const q = query.trim().toLowerCase()
    const matches = !q || [a.name, a.id, a.specialty, departmentOf(a, templates)].some((v) => String(v ?? '').toLowerCase().includes(q))
    return matches && (state === 'all' || agentState(a) === state)
  }), [agents, query, state, templates])

  const opened = agents.find((a) => a.id === openAgentId) ?? null
  const open = (id: string, nextTab = 'profile') => { setTab(nextTab); onOpenAgent(id) }

  const togglePause = (agent: any) => {
    const pausing = agent.is_enabled !== false
    run(`pause:${agent.id}`, () => apiFetch(`/agents/${agent.id}`, { method: 'PATCH', body: JSON.stringify({ is_enabled: !pausing }) }), {
      confirm: pausing ? { message: fill(w.pauseConfirm, { name: agent.name }), confirmLabel: w.pause } : undefined,
      success: fill(pausing ? w.pauseDone : w.resumeDone, { name: agent.name }),
      onDone: onReload,
    })
  }

  return (
    <div className="space-y-5" data-testid="workforce-view">
      <PageHeader title={w.title} subtitle={w.subtitle} icon={<Bot className="h-6 w-6" />} />
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative min-w-[14rem] flex-1 sm:max-w-xs">
          <span className="sr-only">{w.search}</span>
          <Search className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-nexus-dim" aria-hidden />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={w.search} className="ps-9" data-testid="agent-search" />
        </label>
        <Select value={state} onChange={(e) => setState(e.target.value)} aria-label={t.nexus.common.filter} data-testid="agent-state-filter">
          <option value="all">{w.allStates}</option>
          {(['working', 'idle', 'waiting', 'blocked', 'failed', 'offline', 'paused'] as const).map((s) => <option key={s} value={s}>{t.nexus.states[s]}</option>)}
        </Select>
        <span className="text-xs text-nexus-dim">{formatNumber(visible.length)} / {formatNumber(agents.length)}</span>
      </div>
      {visible.length === 0 ? <p className="rounded-xl border border-nexus-border bg-nexus-card p-6 text-center text-sm text-nexus-muted">{w.noAgents}</p> : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {visible.map((agent) => (
            <AgentCard key={agent.id} agent={agent} task={tasks.find((x) => x.id === agent.current_task_id)} department={departmentOf(agent, templates)}
              onOpen={(nextTab) => open(agent.id, nextTab)} onAssign={() => open(agent.id, 'assign')}
              onTogglePause={() => togglePause(agent)} pausing={isBusy(`pause:${agent.id}`)} />
          ))}
        </div>
      )}
      {opened && (
        <AgentDrawer agent={opened} tab={tab} onTab={setTab} onClose={() => onOpenAgent(null)} tasks={tasks} projects={projects}
          department={departmentOf(opened, templates)} employees={employees} agents={agents} onChanged={() => { onReload(); loadEmployees() }} />
      )}
    </div>
  )
}
