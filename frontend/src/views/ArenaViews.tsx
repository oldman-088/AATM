import React, { useEffect, useState } from 'react'
import { AlertTriangle, BookOpen, ExternalLink, FileDiff, GitBranch, KeyRound, ListChecks, Play, PlayCircle, Plus, RefreshCw, Send, Workflow } from 'lucide-react'
import { Badge, Button, Card } from '../components/ui'
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'
import { LIVE_EVENT } from './DashboardViews'

const DOT: Record<string, string> = { green: 'bg-emerald-400', yellow: 'bg-amber-400', red: 'bg-red-500' }

export function connectorLabel(t: any, status: string | null | undefined): string {
  switch (status) {
    case 'ready': return t.arena.statusReady
    case 'busy': return t.arena.statusBusy
    case 'needs_human': return t.arena.statusNeedsHuman
    default: return t.arena.statusOffline
  }
}

export function ArenaConnectorPanel() {
  const { t } = useI18n()
  const [wall, setWall] = useState<any>(null)
  const [script, setScript] = useState<{ profile: string; text: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const load = async () => {
    try { setWall(await apiFetch<any>('/arena/profiles')) } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) }
  }
  useEffect(() => { load() }, [])
  const gate = wall?.gate
  const generate = async (profileId: string) => {
    setBusy(profileId); setError(''); setCopied(false)
    try {
      const data = await apiFetch<any>(`/arena/profiles/${profileId}/userscript`, { method: 'POST' })
      setScript({ profile: profileId, text: data.script }); await load()
    } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  const resume = async (profileId: string) => {
    setBusy(profileId); setError('')
    try { await apiFetch<any>(`/arena/profiles/${profileId}/resume`, { method: 'POST' }); await load() }
    catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  const copy = async () => {
    if (!script) return
    try { await navigator.clipboard.writeText(script.text); setCopied(true) } catch { setCopied(false) }
  }
  return <section data-testid="arena-connector-panel"><Card className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold text-white">{t.arena.title}</h3>
      <Button variant="secondary" onClick={load}><RefreshCw className="me-1 h-4 w-4" />{t.common.refresh}</Button>
    </div>
    {!wall && error && <EmptyState variant="error" onRetry={load} testId="arena-connector-error" />}
    {!wall && !error && <EmptyState variant="loading" testId="arena-connector-loading" />}
    {gate && <div role="status" data-testid="arena-gate" className={`rounded-lg border p-2 text-start text-xs ${gate.real_enabled ? 'border-emerald-500/40 text-emerald-200' : gate.mode === 'mock' ? 'border-sky-500/40 text-sky-200' : 'border-amber-500/40 bg-amber-500/10 text-amber-200'}`}>
      {gate.real_enabled ? t.arena.approved : gate.mode === 'mock' ? t.arena.mockMode : t.arena.waiting}
    </div>}
    <p className="text-xs text-slate-500">{t.arena.runUserscriptDisabled}</p>
    <div className="grid gap-2 md:grid-cols-2">
      {(wall?.profiles || []).length === 0 && <EmptyState variant="empty" testId="arena-connector-empty" />}
      {(wall?.profiles || []).map((p: any) => <div key={p.profile_id} data-testid={`arena-profile-${p.profile_id}`} className="rounded-lg border border-slate-700 p-2" style={{ borderInlineStartColor: p.color, borderInlineStartWidth: 4 }}>
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm text-white">{p.agent_name}</span>
          <span className="flex items-center gap-1 text-xs text-slate-300">
            <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${DOT[p.health] || DOT.yellow}`} />
            {connectorLabel(t, p.connector_status)}
          </span>
        </div>
        {p.connector_status === 'needs_human' && <p className="mt-1 text-xs text-red-300"><Badge variant="danger">{t.arena.needsHuman}</Badge> {p.needs_human_reason}</p>}
        <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="secondary" disabled={!gate?.enabled || busy === p.profile_id} onClick={() => generate(p.profile_id)}>
            <KeyRound className="me-1 h-4 w-4" />{p.token_issued ? t.arena.rotate : t.arena.generate}
          </Button>
          {p.connector_status === 'needs_human' && <Button disabled={busy === p.profile_id} onClick={() => resume(p.profile_id)}><PlayCircle className="me-1 h-4 w-4" />{t.arena.resume}</Button>}
        </div>
      </div>)}
    </div>
    {script && <div className="space-y-2" data-testid="arena-script">
      <p className="text-xs text-amber-200">{t.arena.review} {t.arena.tokenOnce}</p>
      <textarea readOnly dir="ltr" aria-label={t.arena.review} className="h-48 w-full rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-200" value={script.text} />
      <Button variant="secondary" onClick={copy}>{copied ? t.arena.copied : t.arena.copy}</Button>
    </div>}
    {error && <p className="text-xs text-red-300">{error}</p>}
  </Card></section>
}

// ----------------------------------------------------------------------------- R5 Arena Agent dashboard

const PAGE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'neutral'> = {
  ready: 'success', idle: 'success', generating: 'info', keep_working_visible: 'warning', tool_call_limit: 'warning', pr_ready: 'success', needs_human: 'danger', offline: 'neutral',
}
const MAX_LIVE_FRAMES = 40

function fmtTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value.endsWith('Z') || value.includes('+') ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

function Stat({ label, value, tone, testId }: { label: string; value: React.ReactNode; tone?: string; testId: string }) {
  return <div data-testid={testId} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-start">
    <div className="text-xs text-slate-400">{label}</div>
    <div className={`mt-1 text-xl font-semibold ${tone || 'text-white'}`}>{value}</div>
  </div>
}

/** Live diff of one session: GitHub compare summary plus the agent.code frames streamed for that branch. */
export function ArenaLiveDiff({ sessionId, branch, frames }: { sessionId: string; branch?: string | null; frames: any[] }) {
  const { t } = useI18n()
  const a = t.arenaAgent
  const [diff, setDiff] = useState<any>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let stop = false
    apiFetch<any>(`/arena/sessions/${sessionId}/live-diff?refresh=true`)
      .then((data) => { if (!stop) setDiff(data) })
      .catch((err: any) => { if (!stop) setError(err?.message || t.enterprise.operationFailed) })
    return () => { stop = true }
  }, [sessionId])
  const mine = frames.filter((f) => f.branch && f.branch === (diff?.branch || branch)).slice(-8)
  return <div data-testid={`arena-live-diff-${sessionId}`} className="mt-2 space-y-2 rounded-lg border border-slate-700 bg-slate-950/60 p-2 text-start">
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-300">
      <FileDiff className="h-4 w-4" /><span className="font-semibold text-white">{a.liveDiff}</span>
      {diff?.ok && <>
        <Badge variant="success">+{diff.additions} {a.additions}</Badge>
        <Badge variant="danger">−{diff.deletions} {a.deletions}</Badge>
        {diff.compare_url && <a className="inline-flex items-center gap-1 text-sky-300 underline" href={diff.compare_url} target="_blank" rel="noreferrer"><ExternalLink className="h-3 w-3" />{a.compare}</a>}
      </>}
    </div>
    {error && <p className="text-xs text-red-300">{error}</p>}
    {diff && !diff.ok && <p className="text-xs text-slate-400">{a.noDiff}</p>}
    {diff?.ok && <div className="grid gap-2 md:grid-cols-2">
      <div>
        <div className="text-xs text-slate-400">{a.files}</div>
        <ul className="mt-1 space-y-0.5 font-mono text-xs" dir="ltr">
          {(diff.files || []).map((f: any) => <li key={f.filename} className="flex justify-between gap-2"><span className="truncate">{f.filename}</span><span><span className="text-emerald-300">+{f.additions}</span> <span className="text-rose-300">−{f.deletions}</span></span></li>)}
        </ul>
      </div>
      <div>
        <div className="text-xs text-slate-400">{a.commits}</div>
        <ul className="mt-1 space-y-0.5 font-mono text-xs" dir="ltr">
          {(diff.commits || []).slice(-8).reverse().map((c: any) => <li key={c.sha} className="truncate"><span className="text-amber-300">{c.sha.slice(0, 7)}</span> {c.message}</li>)}
        </ul>
      </div>
    </div>}
    {mine.length > 0 && <div>
      <div className="text-xs text-slate-400">{a.latestChanges} · {a.liveFrames}</div>
      {mine.map((f, i) => <pre key={`${f.path}-${i}`} dir="ltr" className="mt-1 max-h-40 overflow-auto rounded bg-black/40 p-2 text-start font-mono text-[11px]">
        <div className="text-slate-400">{f.path} {f.commit ? `@${String(f.commit).slice(0, 7)}` : ''}</div>
        {(f.diff || []).slice(0, 60).map((line: any, j: number) => <div key={j} className={line.type === 'add' ? 'text-emerald-300' : line.type === 'del' ? 'text-rose-300' : 'text-slate-400'}>{line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' '}{line.text}</div>)}
      </pre>)}
    </div>}
  </div>
}

export function ArenaAgentDashboard() {
  const { t } = useI18n()
  const a = t.arenaAgent
  const [data, setData] = useState<any>(null)
  const [queued, setQueued] = useState<any[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [output, setOutput] = useState<{ agent: string; kind: 'reply' | 'selector' | 'script'; value: any } | null>(null)
  const [diffFor, setDiffFor] = useState<string | null>(null)
  const [choice, setChoice] = useState<Record<string, string>>({})
  const [frames, setFrames] = useState<any[]>([])

  const load = async () => {
    try {
      setData(await apiFetch<any>('/arena/agent/overview'))
      const tasks = await apiFetch<any>('/tasks?status=queued')
      setQueued(Array.isArray(tasks) ? tasks.filter((task: any) => (task.task_type || 'implementation') === 'implementation') : [])
    } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) }
  }
  useEffect(() => {
    load()
    const timer = window.setInterval(load, 5000)
    const onEvent = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (!detail) return
      if (detail.type === 'agent.code') {
        const incoming = (detail.payload?.frames || []).filter((f: any) => f?.source === 'arena_github')
        if (incoming.length) setFrames((prev) => [...prev, ...incoming].slice(-MAX_LIVE_FRAMES))
      } else if (typeof detail.type === 'string' && detail.type.startsWith('arena.')) {
        void load()
      }
    }
    window.addEventListener(LIVE_EVENT, onEvent)
    return () => { window.clearInterval(timer); window.removeEventListener(LIVE_EVENT, onEvent) }
  }, [])

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key); setError('')
    try { await fn(); await load() } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  const command = (row: any, cmd: string) => run(`${row.agent_id}:${cmd}`, async () => {
    const res = await apiFetch<any>(`/arena/profiles/${row.profile_id}/command`, { method: 'POST', body: JSON.stringify({ command: cmd }) })
    if (cmd === 'read_last_reply') setOutput({ agent: row.agent_id, kind: 'reply', value: res.result?.text || '' })
    if (cmd === 'selector_test') setOutput({ agent: row.agent_id, kind: 'selector', value: res.result || {} })
  })
  const setMode = (row: any, mode: string) => run(`${row.agent_id}:mode`, async () => {
    await apiFetch<any>(`/arena/agents/${row.agent_id}/mode`, { method: 'PUT', body: JSON.stringify({ mode }) })
  })
  const allAgent = () => run('all:mode', async () => {
    await apiFetch<any>('/arena/agents/mode', { method: 'PUT', body: JSON.stringify({ mode: 'agent' }) })
  })
  const script = (row: any) => run(`${row.agent_id}:script`, async () => {
    const res = await apiFetch<any>(`/arena/profiles/${row.profile_id}/userscript`, { method: 'POST' })
    setOutput({ agent: row.agent_id, kind: 'script', value: res.script })
  })
  const resume = (row: any) => run(`${row.agent_id}:resume`, async () => {
    await apiFetch<any>(`/arena/profiles/${row.profile_id}/resume`, { method: 'POST' })
  })
  // R7: the manager's New session goes through the manager runtime (new long-lived session + context prompt).
  const managerNewSession = (row: any) => run(`${row.agent_id}:new_session`, async () => {
    await apiFetch<any>('/arena/manager/new-session', { method: 'POST', body: JSON.stringify({}) })
  })
  const assign = (row: any) => run(`${row.agent_id}:assign`, async () => {
    const taskId = choice[row.agent_id]
    if (!taskId) return
    await apiFetch<any>(`/arena/agents/${row.agent_id}/assign-task`, { method: 'POST', body: JSON.stringify({ task_id: taskId }) })
    setChoice((prev) => ({ ...prev, [row.agent_id]: '' }))
  })

  const gate = data?.gate
  const counts = data?.counts || {}
  const rows: any[] = data?.employees || data?.profiles || []
  const humans = rows.filter((r) => r.page_state === 'needs_human' || r.connector_status === 'needs_human' || r.agent_status === 'needs_human')
  const connectorText = !gate ? '—' : gate.real_enabled ? a.connectorOn : gate.mode === 'mock' ? a.connectorMock : a.connectorOff
  const label = (map: Record<string, string>, key?: string | null) => (key && map[key]) || key || a.none

  if (!data && error) return <section data-testid="arena-agent-dashboard"><EmptyState variant="error" onRetry={load} testId="arena-agent-error" /></section>
  if (!data) return <section data-testid="arena-agent-dashboard"><EmptyState variant="loading" testId="arena-agent-loading" /></section>

  return <section data-testid="arena-agent-dashboard" className="space-y-4"><Card className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="text-start">
        <h3 className="flex items-center gap-2 text-base font-semibold text-white"><Workflow className="h-5 w-5" />{a.title}</h3>
        <p className="mt-1 text-xs text-slate-400">{a.subtitle}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" disabled={!gate?.enabled || busy === 'all:mode'} onClick={allAgent}><Workflow className="me-1 h-4 w-4" />{a.allAgentMode}</Button>
        <Button variant="secondary" onClick={load}><RefreshCw className="me-1 h-4 w-4" />{t.common.refresh}</Button>
      </div>
    </div>

    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7">
      <Stat testId="arena-stat-connector" label={a.connector} value={connectorText} tone={gate?.enabled ? 'text-emerald-300' : 'text-amber-300'} />
      <Stat testId="arena-stat-profiles" label={a.activeProfiles} value={`${counts.connected_profiles ?? 0}/${counts.agent_mode_profiles ?? 0}`} />
      <Stat testId="arena-stat-sessions" label={a.openSessions} value={counts.open_sessions ?? 0} />
      <Stat testId="arena-stat-running" label={a.runningTasks} value={counts.running_tasks ?? 0} tone="text-sky-300" />
      <Stat testId="arena-stat-completed" label={a.completedTasks} value={counts.completed_tasks ?? 0} tone="text-emerald-300" />
      <Stat testId="arena-stat-failed" label={a.failedTasks} value={counts.failed_tasks ?? 0} tone="text-rose-300" />
      <Stat testId="arena-stat-human" label={a.needsHumanCount} value={counts.needs_human ?? humans.length} tone={humans.length ? 'text-red-300' : 'text-white'} />
    </div>

    {data?.task_distribution && Object.keys(data.task_distribution).length > 0 && <div data-testid="arena-distribution" className="flex flex-wrap items-center gap-2 text-xs text-slate-300">
      <ListChecks className="h-4 w-4" />{a.distribution}:
      {Object.entries(data.task_distribution).map(([status, n]) => <Badge key={status} variant="neutral">{status}: {String(n)}</Badge>)}
    </div>}

    {humans.length > 0 && <div role="alert" data-testid="arena-human-alert" className="rounded-lg border border-red-500/50 bg-red-500/10 p-3 text-start text-sm text-red-200">
      <div className="flex items-center gap-2 font-semibold"><AlertTriangle className="h-4 w-4" />{a.humanAlert}</div>
      <p className="mt-1 text-xs">{a.humanAlertHint}</p>
      <ul className="mt-2 space-y-1 text-xs">
        {humans.map((r) => <li key={r.agent_id} className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{r.agent_name}</span><span>{r.needs_human_reason || ''}</span>
          <Button size="sm" disabled={busy === `${r.agent_id}:resume`} onClick={() => resume(r)}><PlayCircle className="me-1 h-4 w-4" />{a.resumeSession}</Button>
        </li>)}
      </ul>
    </div>}

    {data?.manager && (() => {
      const m = data.manager
      const mk = (cmd: string) => `${m.agent_id}:${cmd}`
      const canCommand = !!gate?.enabled && m.arena_mode === 'agent' && m.connected
      const act = m.activity
      const activityText = act ? `${label(a.manager.activities, act.kind)} · ${label(a.manager.activities, act.status)}` : a.manager.activities.idle
      const ms = m.session
      return <div data-testid="arena-manager-card" className="rounded-lg border border-indigo-500/50 bg-indigo-500/5 p-3 text-start">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-white">{a.manager.title}</div>
            <div className="truncate text-xs text-slate-400">{a.profile}: <span dir="ltr">{m.profile_name || m.profile_id}</span> · {a.manager.subtitle}</div>
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <span data-testid="arena-manager-connection"><Badge variant={m.connected ? 'success' : 'warning'}>{m.connected ? a.manager.connected : a.manager.notConnected}</Badge></span>
            <Badge variant={PAGE_TONE[m.page_state] || 'neutral'}>{label(a.states, m.page_state)}</Badge>
          </div>
        </div>
        {!m.connected && <p data-testid="arena-manager-hint" className="mt-2 text-xs text-amber-200">{a.manager.notConnectedHint}</p>}
        <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <dt className="text-slate-400">{a.manager.currentSession}</dt><dd className="text-slate-200" data-testid="arena-manager-session">{ms ? `${label(a.phases, ms.phase)} · ${a.manager.exchanges}: ${ms.exchange_count ?? 0}` : a.none}</dd>
          <dt className="text-slate-400">{a.manager.currentActivity}</dt><dd className="text-slate-200" data-testid="arena-manager-activity">{activityText}</dd>
          <dt className="text-slate-400">{a.lastActivity}</dt><dd className="text-slate-200">{fmtTime(act?.updated_at || ms?.last_activity_at || ms?.last_state_change_at)}</dd>
          <dt className="text-slate-400">{a.selectorTest}</dt><dd className={m.selector_test?.ok ? 'text-emerald-300' : 'text-amber-300'}>{m.selector_test?.ran ? (m.selector_test.ok ? a.manager.selectorPassed : a.selectorFailed) : a.manager.selectorNotRun}</dd>
          {(ms?.error_message || m.last_error || m.needs_human_reason) && <><dt className="text-slate-400">{a.error}</dt><dd className="truncate text-rose-300">{m.needs_human_reason || ms?.error_message || m.last_error}</dd></>}
        </dl>
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Button size="sm" variant="secondary" disabled={!gate?.enabled || busy === mk('script')} onClick={() => script(m)}><KeyRound className="me-1 h-4 w-4" />{m.token_issued ? a.rotateScript : a.generateScript}</Button>
          <Button size="sm" variant="secondary" disabled={!canCommand || busy === mk('selector_test')} onClick={() => command(m, 'selector_test')}><ListChecks className="me-1 h-4 w-4" />{a.selectorTest}</Button>
          <Button size="sm" variant="secondary" disabled={!canCommand || busy === mk('new_session')} onClick={() => managerNewSession(m)}><Plus className="me-1 h-4 w-4" />{a.newSession}</Button>
          <Button size="sm" variant="secondary" disabled={!canCommand || busy === mk('read_last_reply')} onClick={() => command(m, 'read_last_reply')}><BookOpen className="me-1 h-4 w-4" />{a.readLastReply}</Button>
          <Button size="sm" variant="secondary" disabled={!canCommand || busy === mk('keep_working')} onClick={() => command(m, 'keep_working')}><Play className="me-1 h-4 w-4" />{a.continueWorking}</Button>
          {(m.page_state === 'needs_human' || m.connector_status === 'needs_human') && <Button size="sm" disabled={busy === mk('resume')} onClick={() => resume(m)}><PlayCircle className="me-1 h-4 w-4" />{a.resumeSession}</Button>}
        </div>
        {output && output.agent === m.agent_id && <div className="mt-2 space-y-1" data-testid={`arena-output-${m.agent_id}`}>
          {output.kind === 'reply' && <><div className="text-xs text-slate-400">{a.lastReply}</div>
            <pre dir="auto" className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-black/40 p-2 text-start text-xs text-slate-200">{output.value || a.none}</pre></>}
          {output.kind === 'selector' && <><div className="text-xs text-slate-400">{a.selectorReport}</div>
            <p className={`text-xs ${output.value.ok ? 'text-emerald-300' : 'text-amber-300'}`}>{output.value.ok ? a.selectorOk : a.selectorFailed}</p>
            <pre dir="ltr" className="max-h-48 overflow-auto rounded bg-black/40 p-2 text-start text-[11px] text-slate-300">{JSON.stringify(output.value, null, 2)}</pre></>}
          {output.kind === 'script' && <><p className="text-xs text-amber-200">{t.arena.review} {t.arena.tokenOnce}</p>
            <textarea readOnly dir="ltr" aria-label={t.arena.review} className="h-40 w-full rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-200" value={output.value} /></>}
        </div>}
      </div>
    })()}

    {rows.length === 0 && <EmptyState variant="empty" testId="arena-agent-empty" />}
    <div className="grid gap-3 lg:grid-cols-2">
      {rows.map((r) => {
        const s = r.session
        const isAgent = r.arena_mode === 'agent'
        const key = (cmd: string) => `${r.agent_id}:${cmd}`
        const canCommand = !!gate?.enabled && isAgent && r.connected
        return <div key={r.agent_id} data-testid={`arena-employee-${r.agent_id}`} className="rounded-lg border border-slate-700 p-3 text-start">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-white">{r.agent_name}</div>
              <div className="truncate text-xs text-slate-400">{a.profile}: <span dir="ltr">{r.profile_name || r.profile_id}</span></div>
            </div>
            <div className="flex flex-wrap items-center gap-1">
              <Badge variant={isAgent ? 'info' : 'neutral'}>{isAgent ? a.modeAgent : a.modeChat}</Badge>
              <Badge variant={PAGE_TONE[r.page_state] || 'neutral'}>{label(a.states, r.page_state)}</Badge>
            </div>
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            <dt className="text-slate-400">{a.sessionStatus}</dt><dd className="text-slate-200">{s ? label(a.phases, s.phase) : a.none}</dd>
            <dt className="text-slate-400">{a.currentTask}</dt><dd className="truncate text-slate-200">{r.task ? `${r.task.title} (${r.task.status})` : a.none}</dd>
            <dt className="text-slate-400">{a.branch}</dt><dd className="truncate font-mono text-slate-200" dir="ltr">{s?.branch_name ? <><GitBranch className="me-1 inline h-3 w-3" />{s.branch_name}</> : a.none}</dd>
            <dt className="text-slate-400">{a.pullRequest}</dt><dd className="truncate">{s?.pr_url ? <a className="text-sky-300 underline" href={s.pr_url} target="_blank" rel="noreferrer" dir="ltr">#{s.pr_number}</a> : a.none}</dd>
            <dt className="text-slate-400">{a.lastActivity}</dt><dd className="text-slate-200">{fmtTime(s?.last_activity_at || s?.last_state_change_at)}</dd>
            <dt className="text-slate-400">{a.keepWorking}</dt><dd className="text-slate-200" data-testid={`arena-kw-${r.agent_id}`}>{s?.keep_working_count ?? r.keep_working_clicks ?? 0}</dd>
            {(s?.error_message || r.last_error) && <><dt className="text-slate-400">{a.error}</dt><dd className="truncate text-rose-300">{s?.error_message || r.last_error}</dd></>}
          </dl>
          {(r.human_events || []).length > 0 && <div className="mt-2 text-xs text-red-200">
            <div className="text-slate-400">{a.humanEvents}</div>
            <ul>{r.human_events.map((e: any, i: number) => <li key={i}>{fmtTime(e.ts)} · {e.detail || e.kind}</li>)}</ul>
          </div>}
          <div className="mt-3 flex flex-wrap gap-1.5">
            <Button size="sm" variant="secondary" disabled={!gate?.enabled || busy === key('mode')} onClick={() => setMode(r, isAgent ? 'chat' : 'agent')}>{isAgent ? a.switchToChat : a.switchToAgent}</Button>
            <Button size="sm" variant="secondary" disabled={!canCommand || busy === key('new_session')} onClick={() => command(r, 'new_session')}><Plus className="me-1 h-4 w-4" />{a.newSession}</Button>
            <Button size="sm" variant="secondary" disabled={!canCommand || busy === key('keep_working')} onClick={() => command(r, 'keep_working')}><Play className="me-1 h-4 w-4" />{a.continueWorking}</Button>
            <Button size="sm" variant="secondary" disabled={!canCommand || busy === key('read_last_reply')} onClick={() => command(r, 'read_last_reply')}><BookOpen className="me-1 h-4 w-4" />{a.readLastReply}</Button>
            <Button size="sm" variant="secondary" disabled={!canCommand || busy === key('selector_test')} onClick={() => command(r, 'selector_test')}><ListChecks className="me-1 h-4 w-4" />{a.selectorTest}</Button>
            <Button size="sm" variant="secondary" disabled={!gate?.enabled || busy === key('script')} onClick={() => script(r)}><KeyRound className="me-1 h-4 w-4" />{r.token_issued ? a.rotateScript : a.generateScript}</Button>
            {(r.page_state === 'needs_human' || r.connector_status === 'needs_human' || r.agent_status === 'needs_human') && <Button size="sm" disabled={busy === key('resume')} onClick={() => resume(r)}><PlayCircle className="me-1 h-4 w-4" />{a.resumeSession}</Button>}
            {s && <Button size="sm" variant="secondary" onClick={() => setDiffFor(diffFor === s.id ? null : s.id)}><FileDiff className="me-1 h-4 w-4" />{a.viewLiveDiff}</Button>}
            {s?.pr_url && <a className="inline-flex items-center rounded-lg border border-slate-700 px-2 py-1 text-xs text-sky-300" href={s.pr_url} target="_blank" rel="noreferrer"><ExternalLink className="me-1 h-4 w-4" />{a.viewPr}</a>}
          </div>
          {isAgent && <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <select aria-label={a.chooseTask} className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200"
              value={choice[r.agent_id] || ''} onChange={(e) => setChoice((prev) => ({ ...prev, [r.agent_id]: e.target.value }))}>
              <option value="">{a.chooseTask}</option>
              {queued.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
            </select>
            <Button size="sm" disabled={!choice[r.agent_id] || busy === key('assign')} onClick={() => assign(r)}><Send className="me-1 h-4 w-4" />{a.submitTask}</Button>
          </div>}
          {s && diffFor === s.id && <ArenaLiveDiff sessionId={s.id} branch={s.branch_name} frames={frames} />}
          {output && output.agent === r.agent_id && <div className="mt-2 space-y-1" data-testid={`arena-output-${r.agent_id}`}>
            {output.kind === 'reply' && <><div className="text-xs text-slate-400">{a.lastReply}</div>
              <pre dir="auto" className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-black/40 p-2 text-start text-xs text-slate-200">{output.value || a.none}</pre></>}
            {output.kind === 'selector' && <><div className="text-xs text-slate-400">{a.selectorReport}</div>
              <p className={`text-xs ${output.value.ok ? 'text-emerald-300' : 'text-amber-300'}`}>{output.value.ok ? a.selectorOk : a.selectorFailed}</p>
              <pre dir="ltr" className="max-h-48 overflow-auto rounded bg-black/40 p-2 text-start text-[11px] text-slate-300">{JSON.stringify(output.value, null, 2)}</pre></>}
            {output.kind === 'script' && <><p className="text-xs text-amber-200">{t.arena.review} {t.arena.tokenOnce}</p>
              <textarea readOnly dir="ltr" aria-label={t.arena.review} className="h-40 w-full rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-200" value={output.value} /></>}
          </div>}
        </div>
      })}
    </div>
    {error && <p className="text-xs text-red-300">{error}</p>}
  </Card></section>
}
