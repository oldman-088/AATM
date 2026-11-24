import React, { useEffect, useState } from 'react'
import { Activity, AlertTriangle, ArrowLeftRight, GitPullRequest, OctagonX, Pause, Play, RefreshCw, UserRoundCog } from 'lucide-react'
import { Badge, Button, Card, Progress } from '../components/ui'
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

/** Live events from the authenticated /ws/events socket are re-broadcast by App as this DOM event. */
export const LIVE_EVENT = 'aatm:event'

export function useLiveEvents(handler: (event: any) => void) {
  useEffect(() => {
    const listener = (e: Event) => handler((e as CustomEvent).detail)
    window.addEventListener(LIVE_EVENT, listener)
    return () => window.removeEventListener(LIVE_EVENT, listener)
  }, [handler])
}

function useLoad<T>(path: string | null, reloadOn: string[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState('')
  const load = React.useCallback(async () => {
    if (!path) return
    try { setData(await apiFetch<T>(path)); setError('') } catch (err: any) { setError(err?.message || 'error') }
  }, [path])
  useEffect(() => { load() }, [load])
  const onEvent = React.useCallback((ev: any) => { if (reloadOn.includes(ev?.type)) load() }, [load, reloadOn.join(',')])
  useLiveEvents(onEvent)
  return { data, error, reload: load }
}

const TASK_EVENTS = ['task.updated', 'task.created', 'agent.heartbeat', 'arena.needs_human', 'arena.resumed', 'approval.decided']

// ------------------------------------------------------------------ home strip + counters

export function HomeOverview() {
  const { t, formatNumber } = useI18n()
  const { data, error, reload } = useLoad<any>('/dashboard/home', TASK_EVENTS)
  if (error) return <EmptyState variant="error" onRetry={reload} testId="home-overview-error" />
  if (!data?.counters || !Array.isArray(data.architecture)) return <EmptyState variant="loading" testId="home-overview-loading" />
  if (data.architecture.length === 0) return <EmptyState variant="empty" testId="home-overview-empty" />
  const c = data.counters
  const archLabel: Record<string, string> = {
    dashboard: t.home.archDashboard, orchestrator: t.home.archOrchestrator, profiles: t.home.archProfiles, results: t.home.archResults,
  }
  return <section data-testid="home-overview" className="space-y-3">
    <Card>
      <h3 className="mb-2 text-sm font-semibold text-white">{t.home.architecture}</h3>
      <ol className="flex flex-wrap items-center gap-2 text-sm">
        {data.architecture.map((a: any, i: number) => <li key={a.id} className="flex items-center gap-2">
          <span className="rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-3 py-1 text-indigo-100">
            {archLabel[a.id]}{a.count !== null && <span className="ms-1 text-xs text-slate-400">({formatNumber(a.count)})</span>}
          </span>
          {i < data.architecture.length - 1 && <span aria-hidden className="text-slate-500 rtl:rotate-180">→</span>}
        </li>)}
      </ol>
    </Card>
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Card><p className="text-xs text-slate-400">{t.home.activeEmployees}</p><p data-testid="counter-active" className="text-xl text-white">{formatNumber(c.active_employees)} / {formatNumber(c.total_employees)}</p></Card>
      <Card><p className="text-xs text-slate-400">{t.home.openPrs}</p><p data-testid="counter-prs" className="text-xl text-white">{formatNumber(c.open_prs)}</p></Card>
      <Card><p className="text-xs text-slate-400">{t.home.costToday}</p><p className="text-xl text-white" dir="ltr">${c.cost_today_usd.toFixed(2)} / ${c.daily_budget_usd.toFixed(2)}</p></Card>
      <Card><p className="text-xs text-slate-400">{t.home.needsHuman}</p><p className={`text-xl ${c.needs_human ? 'text-rose-300' : 'text-white'}`}>{formatNumber(c.needs_human)}</p></Card>
    </div>
    <Card>
      <p className="mb-1 text-xs text-slate-400">{t.home.tasksByStatus}</p>
      <div className="flex flex-wrap gap-2">
        {Object.entries(c.tasks_by_status || {}).map(([s, n]) => <Badge key={s} variant="neutral">{s}: {formatNumber(n as number)}</Badge>)}
      </div>
    </Card>
  </section>
}

// ------------------------------------------------------------------ P1: needs you now

const ATTENTION_EVENTS = [...TASK_EVENTS, 'approval.requested', 'approval.plan_release', 'manager.activity']

export function NeedsYouPanel({ onNavigate }: { onNavigate?: (tab: string) => void }) {
  const { t } = useI18n()
  const n = t.needsYou
  const { data, error, reload } = useLoad<any>('/dashboard/attention', ATTENTION_EVENTS)
  if (error) return <EmptyState variant="error" onRetry={reload} testId="needs-you-error" />
  if (!data || !Array.isArray(data.items)) return null
  const items: any[] = data.items
  return <Card className={items.length ? 'border-amber-500/50' : ''}>
    <section data-testid="needs-you" className="text-start">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
          <AlertTriangle className={`h-4 w-4 ${items.length ? 'text-amber-300' : 'text-slate-400'}`} />{n.title}
          {items.length > 0 && <Badge variant="warning">{data.count ?? items.length}</Badge>}
        </h3>
        <p className="text-xs text-slate-400">{n.subtitle}</p>
      </div>
      {items.length === 0 && <p data-testid="needs-you-empty" className="mt-2 text-xs text-emerald-300">{n.empty}</p>}
      {items.length > 0 && <ul className="mt-2 divide-y divide-slate-800">
        {items.map((item) => <li key={`${item.kind}:${item.id}`} data-testid={`needs-you-${item.kind}`} className="flex flex-wrap items-center gap-2 py-2 text-xs">
          <Badge variant={item.kind === 'approval' || item.kind === 'plan_draft' ? 'info' : 'warning'}>{(n.kinds as Record<string, string>)[item.kind] || item.kind}</Badge>
          <span dir="auto" className="min-w-0 flex-1 truncate text-slate-200">{item.title}</span>
          {item.detail && <span dir="auto" className="min-w-0 max-w-full truncate text-slate-400 sm:max-w-xs">{item.detail}</span>}
          {onNavigate && <Button size="sm" variant="secondary" onClick={() => onNavigate(item.tab)}>{n.open}</Button>}
        </li>)}
      </ul>}
    </section>
  </Card>
}

// ------------------------------------------------------------------ P3 O5: the last 24 hours

export function DailySummary() {
  const { t, formatNumber } = useI18n()
  const d = t.home
  const { data, error, reload } = useLoad<any>('/dashboard/daily', TASK_EVENTS)
  if (error) return <EmptyState variant="error" onRetry={reload} testId="daily-summary-error" />
  if (!data || typeof data.completed_tasks !== 'number') return null
  const merged: any[] = data.merged_prs || []
  const failed: any[] = data.failed_or_blocked || []
  return <Card className="text-start"><section data-testid="daily-summary">
    <h3 className="mb-2 text-sm font-semibold text-white">{d.dailyTitle}</h3>
    <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
      <div><p className="text-slate-400">{d.dailyCompleted}</p><p className="text-lg text-emerald-300">{formatNumber(data.completed_tasks)}</p></div>
      <div><p className="text-slate-400">{d.dailyMerged}</p><p className="text-lg text-white">{formatNumber(merged.length)}</p></div>
      <div><p className="text-slate-400">{d.dailyFailed}</p><p className={`text-lg ${failed.length ? 'text-rose-300' : 'text-white'}`}>{formatNumber(failed.length)}</p></div>
      <div><p className="text-slate-400">{d.dailyWaiting}</p><p className={`text-lg ${data.pending_approvals ? 'text-amber-300' : 'text-white'}`}>{formatNumber(data.pending_approvals)}</p></div>
    </div>
    {merged.length > 0 && <ul className="mt-2 space-y-0.5 text-xs text-slate-300">{merged.slice(0, 5).map((m) =>
      <li key={m.task_id} className="truncate"><span dir="ltr">#{m.pr_number}</span> {m.title}</li>)}</ul>}
    {failed.length > 0 && <ul className="mt-2 space-y-0.5 text-xs text-rose-200">{failed.slice(0, 5).map((f) =>
      <li key={f.task_id} className="truncate">{f.title} ({f.status})</li>)}</ul>}
  </section></Card>
}

// ------------------------------------------------------------------ 8-step timeline

export function ProjectTimeline({ projectId }: { projectId: string | null }) {
  const { t, formatNumber } = useI18n()
  const { data, error, reload } = useLoad<any>(projectId ? `/projects/${projectId}/timeline` : null, TASK_EVENTS)
  const labels: Record<string, string> = {
    request: t.home.stepRequest, analyze_split: t.home.stepAnalyze, distribute: t.home.stepDistribute, execute: t.home.stepExecute,
    collect: t.home.stepCollect, pull_request: t.home.stepPr, review_merge: t.home.stepReview, completed: t.home.stepCompleted,
  }
  const stateLabel: Record<string, string> = { done: t.home.stateDone, active: t.home.stateActive, pending: t.home.statePending }
  const tone: Record<string, string> = {
    done: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-100',
    active: 'border-amber-400/60 bg-amber-400/10 text-amber-100 animate-pulse',
    pending: 'border-slate-700 text-slate-400',
  }
  return <Card data-testid="project-timeline">
    <h3 className="mb-2 text-sm font-semibold text-white">{t.home.timeline}{data ? ` — ${data.project_name}` : ''}</h3>
    {data?.progress && data.progress.total > 0 && <div data-testid="project-progress" className="mb-3 text-start text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2 text-slate-300">
        <span>{t.home.progressDone}: <span dir="ltr">{formatNumber(data.progress.done)}/{formatNumber(data.progress.total)}</span> ({formatNumber(data.progress.percent)}%)</span>
        <span data-testid="project-eta">{data.progress.remaining === 0 ? t.home.etaDone
          : data.progress.eta_minutes == null ? t.home.etaUnknown
          : `${t.home.etaLeft}: ${data.progress.eta_minutes >= 90 ? `${formatNumber(Math.round(data.progress.eta_minutes / 6) / 10)} ${t.home.hoursShort}` : `${formatNumber(data.progress.eta_minutes)} ${t.home.minutesShort}`}`}</span>
      </div>
      <Progress value={data.progress.percent} className="mt-1" />
    </div>}
    {error ? (
      <EmptyState variant="error" onRetry={reload} testId="project-timeline-error" />
    ) : !projectId ? (
      <p className="text-xs text-slate-400">{t.home.noProject}</p>
    ) : !Array.isArray(data?.steps) || data.steps.length === 0 ? (
      <EmptyState variant="empty" testId="project-timeline-empty" />
    ) : (
      <ol className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {data.steps.map((s: any) => <li key={s.id} data-testid={`step-${s.id}`} data-state={s.state} className={`rounded-lg border p-2 text-start text-xs ${tone[s.state]}`}>
        <div className="font-medium">{labels[s.id]}</div>
        <div className="mt-1 flex items-center justify-between gap-1">
          <span>{stateLabel[s.state]}</span>
          {s.total > 0 && <span dir="ltr">{formatNumber(s.done)}/{formatNumber(s.total)}</span>}
        </div>
      </li>)}
    </ol>
    )}
  </Card>
}

// ------------------------------------------------------------------ employee cards

export function EmployeeCards() {
  const { t } = useI18n()
  const { data, error: loadError, reload } = useLoad<any>('/dashboard/employees', TASK_EVENTS)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id); setError('')
    try { await fn(); await reload() } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  return <section data-testid="employee-cards" className="space-y-2">
    <h3 className="text-sm font-semibold text-white">{t.home.employees}</h3>
    {loadError ? (
      <EmptyState variant="error" onRetry={reload} testId="employee-cards-error" />
    ) : (Array.isArray(data?.employees) && data.employees.length === 0) ? (
      <EmptyState variant="empty" testId="employee-cards-empty" />
    ) : (
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {(Array.isArray(data?.employees) ? data.employees : []).map((e: any) => <Card key={e.id} className="space-y-1" >
        <div data-testid={`employee-${e.id}`} className="space-y-1 border-s-4 ps-2" style={{ borderInlineStartColor: e.color }}>
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-medium text-white">{e.name}</span>
            <Badge variant={e.is_emergency_stopped ? 'danger' : e.status === 'working' ? 'success' : e.status === 'needs_human' ? 'danger' : 'neutral'}>{e.is_emergency_stopped ? t.home.emergencyStop : e.status}</Badge>
          </div>
          <p className="text-xs text-slate-400">{e.role} · {t.home.model}: <span dir="ltr">{e.model || t.home.none}</span>{e.provider ? ` (${e.provider})` : ''}</p>
          <p className="text-xs text-slate-400">{t.home.profile}: {e.profile_status || t.home.none}{e.connector_status ? ` · ${e.connector_status}` : ''}</p>
          <p className="text-xs text-slate-300">{t.home.currentTask}: {e.current_task ? `${e.current_task.title} (${e.current_task.progress}%)` : t.home.none}</p>
          <p className="line-clamp-2 text-xs text-slate-400">{t.home.lastAction}: {e.last_action?.summary || t.home.none}</p>
          <p className="text-xs text-slate-400">{t.home.cost}: <span dir="ltr">${Number(e.cost_usd || 0).toFixed(4)}</span></p>
          <div className="flex flex-wrap gap-1 pt-1">
            <Button size="sm" variant="secondary" disabled={busy === e.id} onClick={() => act(e.id, () => apiFetch(`/agents/${e.id}`, { method: 'PATCH', body: JSON.stringify({ is_enabled: !e.is_enabled }) }))}>
              {e.is_enabled ? <><Pause className="me-1 h-3 w-3" />{t.home.pause}</> : <><Play className="me-1 h-3 w-3" />{t.home.unpause}</>}
            </Button>
            <Button size="sm" variant="danger" disabled={busy === e.id || e.is_emergency_stopped} onClick={() => act(e.id, () => apiFetch('/emergency-stop', { method: 'POST', body: JSON.stringify({ scope: 'employee', target_id: e.id, reason: 'Stopped from the dashboard' }) }))}>
              <OctagonX className="me-1 h-3 w-3" />{t.home.emergencyStop}
            </Button>
            {e.role !== 'manager' && <Button size="sm" variant="ghost" disabled={busy === e.id} onClick={() => act(e.id, () => apiFetch(`/employees/${e.id}/replace`, { method: 'POST' }))}>
              <UserRoundCog className="me-1 h-3 w-3" />{t.home.replace}
            </Button>}
          </div>
        </div>
      </Card>)}
    </div>
    )}
    {error && <p className="text-xs text-rose-300">{error}</p>}
  </section>
}

// ------------------------------------------------------------------ live agent log

export function LiveAgentLog({ max = 200 }: { max?: number }) {
  const { t } = useI18n()
  const [entries, setEntries] = useState<any[]>([])
  const [error, setError] = useState(false)
  const load = React.useCallback(() => {
    setError(false)
    apiFetch<any>('/dashboard/agent-log?limit=100').then((d) => setEntries(Array.isArray(d?.entries) ? d.entries : [])).catch(() => setError(true))
  }, [])
  useEffect(() => { load() }, [load])
  const onEvent = React.useCallback((ev: any) => {
    if (ev?.type !== 'agent.iteration') return
    const p = ev.payload || {}
    setEntries((prev) => [...prev, {
      agent_id: p.agent_id, task_id: p.task_id, failed: !!p.failed, at: ev.timestamp,
      summary: `Iteration ${p.iteration}: ${p.action} — ${p.result || ''}`,
    }].slice(-max))
  }, [max])
  useLiveEvents(onEvent)
  return <Card data-testid="agent-log">
    <div className="mb-2 flex items-center justify-between">
      <h3 className="flex items-center gap-1 text-sm font-semibold text-white"><Activity className="h-4 w-4" />{t.home.agentLog}</h3>
      <Badge variant="info">{t.home.logLive}</Badge>
    </div>
    {error ? (
      <EmptyState variant="error" onRetry={load} testId="agent-log-error" />
    ) : entries.length === 0 ? (
      <EmptyState variant="empty" testId="agent-log-empty" />
    ) : null}
    <ul role="log" aria-live="polite" className="max-h-72 space-y-1 overflow-y-auto font-mono text-xs" dir="ltr">
      {entries.map((e, i) => <li key={i} className={e.failed ? 'text-rose-300' : 'text-slate-300'}>
        <span className="text-indigo-300">[{e.agent_id}]</span> {e.summary}
      </li>)}
    </ul>
  </Card>
}

// ------------------------------------------------------------------ PR view

// P1 PR card: CI on the head commit and merge conflicts, read from GitHub for each open PR.
export function PullRequestStatus({ taskId }: { taskId: string }) {
  const { t } = useI18n()
  const h = t.home
  const { data, error } = useLoad<any>(`/reviews/tasks/${encodeURIComponent(taskId)}/pr-state`, ['task.updated'])
  if (error || (data && data.available === false)) return <p data-testid={`pr-state-${taskId}`} className="mt-1 text-xs text-slate-500">{h.prStateUnavailable}</p>
  if (!data) return null
  const ci: Record<string, [string, 'success' | 'danger' | 'warning' | 'neutral']> = {
    success: [h.ciPassed, 'success'], failure: [h.ciFailed, 'danger'], pending: [h.ciPending, 'warning'], none: [h.ciNone, 'neutral'],
  }
  const [ciText, ciTone] = ci[data.ci] || ci.none
  const failing = (data.checks || []).filter((c: any) => c.result === 'failure')
  return <div data-testid={`pr-state-${taskId}`} className="mt-1 flex flex-wrap items-center gap-1 text-xs">
    <Badge variant={ciTone}>{ciText}</Badge>
    {data.state === 'OPEN' && <Badge variant={data.conflicting ? 'danger' : 'neutral'}>{data.conflicting ? h.conflict : h.noConflict}</Badge>}
    {failing.length > 0 && <span className="text-rose-300">{h.failingChecks}: <span dir="ltr">{failing.map((c: any) => c.name).join(', ')}</span></span>}
  </div>
}

export function PullRequestsView({ projectId }: { projectId?: string | null }) {
  const { t } = useI18n()
  const { data: raw, error: loadError, reload } = useLoad<any[]>(projectId ? `/reviews/pull-requests?project_id=${projectId}` : '/reviews/pull-requests', TASK_EVENTS)
  const data = Array.isArray(raw) ? raw : []
  const [diff, setDiff] = useState<{ task: string; text: string | null } | null>(null)
  const [error, setError] = useState('')
  const showDiff = async (taskId: string) => {
    try {
      const d = await apiFetch<any>(`/reviews/tasks/${taskId}/diff`)
      setDiff({ task: taskId, text: d.available ? (d.diff || d.stdout || '') : null })
    } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) }
  }
  const run = async (fn: () => Promise<unknown>) => {
    setError('')
    try { await fn(); await reload() } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) }
  }
  return <Card data-testid="pr-view" className="space-y-2">
    <h3 className="flex items-center gap-1 text-sm font-semibold text-white"><GitPullRequest className="h-4 w-4" />{t.home.pullRequests}</h3>
    {loadError ? (
      <EmptyState variant="error" onRetry={reload} testId="pr-view-error" />
    ) : data.length === 0 ? (
      <EmptyState variant="empty" testId="pr-view-empty" />
    ) : null}
    {data.map((pr: any) => {
      const approved = (pr.merge_approvals || []).find((a: any) => a.status === 'approved')
      const pending = (pr.merge_approvals || []).find((a: any) => a.status === 'pending')
      const merged = ['merged', 'done'].includes(pr.status)
      return <div key={pr.task_id} data-testid={`pr-${pr.pr_number}`} className="rounded-lg border border-slate-700 p-2 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-white">#{pr.pr_number} {pr.title}</span>
          <Badge variant={merged ? 'success' : pr.status === 'changes_requested' ? 'warning' : 'info'}>{pr.status}</Badge>
        </div>
        <p className="text-xs text-slate-400" dir="ltr">{pr.branch_name}</p>
        {!merged && <PullRequestStatus taskId={pr.task_id} />}
        {pr.reviews?.length > 0 && <details className="mt-1 text-xs text-slate-300"><summary>{t.home.reviews} ({pr.reviews.length})</summary>
          <ul className="ms-4 list-disc">{pr.reviews.map((r: any) => <li key={r.task_id}>{r.task_type} #{r.round}: {r.status}{r.summary ? ` — ${r.summary}` : ''}</li>)}</ul>
        </details>}
        <div className="mt-2 flex flex-wrap gap-1">
          <Button size="sm" variant="secondary" onClick={() => showDiff(pr.task_id)}><ArrowLeftRight className="me-1 h-3 w-3" />{t.home.viewDiff}</Button>
          {!merged && !approved && !pending && <Button size="sm" onClick={() => run(() => apiFetch(`/reviews/tasks/${pr.task_id}/request-merge`, { method: 'POST' }))}>{t.home.requestMerge}</Button>}
          {!merged && approved && <Button size="sm" onClick={() => run(() => apiFetch(`/reviews/tasks/${pr.task_id}/merge`, { method: 'POST', body: JSON.stringify({ approval_id: approved.id }) }))}>{t.home.merge}</Button>}
          {pr.pr_url && <a className="text-xs text-indigo-300 underline" href={pr.pr_url} target="_blank" rel="noreferrer">{t.home.openOnGithub}</a>}
        </div>
        {!merged && pending && <p className="mt-1 text-xs text-amber-200">{t.home.mergeApprovalPending}</p>}
        {diff && diff.task === pr.task_id && (diff.text === null
          ? <p className="mt-1 text-xs text-slate-400">{t.home.diffUnavailable}</p>
          : <pre data-testid="pr-diff" dir="ltr" className="mt-2 max-h-80 overflow-auto rounded bg-slate-950 p-2 text-start text-xs">{diff.text.split('\n').map((line, i) =>
            <div key={i} className={line.startsWith('+') ? 'text-emerald-300' : line.startsWith('-') ? 'text-rose-300' : 'text-slate-300'}>{line}</div>)}</pre>)}
      </div>
    })}
    {error && <p className="text-xs text-rose-300">{error}</p>}
  </Card>
}

// ------------------------------------------------------------------ errors & recovery

export function RecoveryPanel() {
  const { t } = useI18n()
  const { data, error, reload } = useLoad<any>('/dashboard/recovery', TASK_EVENTS)
  const resume = async (profileId: string) => {
    await apiFetch(`/arena/profiles/${profileId}/resume`, { method: 'POST' }).catch(() => undefined)
    await reload()
  }
  if (error) return <EmptyState variant="error" onRetry={reload} testId="recovery-panel-error" />
  if (!data || !['failed_tasks', 'retried_tasks', 'blocked_tasks', 'replacements', 'needs_human'].every((k) => Array.isArray(data[k]))) return <EmptyState variant="loading" testId="recovery-panel-loading" />
  const empty = !data.failed_tasks.length && !data.retried_tasks.length && !data.blocked_tasks.length && !data.replacements.length && !data.needs_human.length
  const rows = (title: string, items: any[]) => items.length > 0 && <div>
    <p className="text-xs font-medium text-slate-300">{title} ({items.length})</p>
    <ul className="ms-4 list-disc text-xs text-slate-400">{items.map((x) => <li key={x.id}>{x.title} — {x.status}{x.retry_count ? ` · ${x.retry_count}/${x.max_retries} ${t.home.retries}` : ''}{x.error ? `: ${x.error}` : ''}</li>)}</ul>
  </div>
  return <Card data-testid="recovery-panel" className="space-y-2">
    <div className="flex items-center justify-between">
      <h3 className="flex items-center gap-1 text-sm font-semibold text-white"><AlertTriangle className="h-4 w-4" />{t.home.recovery}</h3>
      <Button size="sm" variant="ghost" onClick={reload}><RefreshCw className="h-3 w-3" /></Button>
    </div>
    {empty && <p className="text-xs text-slate-400">{t.home.nothingToRecover}</p>}
    {data.needs_human.map((n: any) => <div key={n.profile_id} className="flex flex-wrap items-center justify-between gap-2 rounded border border-rose-500/40 p-2 text-xs text-rose-200">
      <span><Badge variant="danger">{t.home.needsHuman}</Badge> {n.agent_id}: {n.reason}</span>
      <Button size="sm" onClick={() => resume(n.profile_id)}>{t.arena.resume}</Button>
    </div>)}
    {rows(t.home.failedTasks, data.failed_tasks)}
    {rows(t.home.blockedTasks, data.blocked_tasks)}
    {rows(t.home.retriedTasks, data.retried_tasks)}
    {data.replacements.length > 0 && <div>
      <p className="text-xs font-medium text-slate-300">{t.home.replacements} ({data.replacements.length})</p>
      <ul className="ms-4 list-disc text-xs text-slate-400">{data.replacements.map((r: any) => <li key={r.id}>{r.resource_id} — {r.actor}</li>)}</ul>
    </div>}
  </Card>
}

export function MissionControl({ projectId, onNavigate }: { projectId: string | null; onNavigate?: (tab: string) => void }) {
  return <div className="space-y-4" data-testid="mission-control">
    <NeedsYouPanel onNavigate={onNavigate} />
    <HomeOverview />
    <ProjectTimeline projectId={projectId} />
    <DailySummary />
    <div className="grid gap-4 xl:grid-cols-2">
      <LiveAgentLog />
      <RecoveryPanel />
    </div>
    <EmployeeCards />
  </div>
}
