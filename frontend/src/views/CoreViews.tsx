import React, { useState } from 'react'
import {
  Activity,
  Bot,
  CheckCircle2,
  Cpu,
  MessageSquare,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Sparkles,
} from 'lucide-react'
import { Badge, Button, Card, Input, Progress, Select } from '../components/ui'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

export function DashboardView({
  overview,
  onRefresh,
  onRunCycle,
  onSelfHeal,
}: {
  overview: any
  onRefresh: () => void
  onRunCycle: () => void
  onSelfHeal: () => void
}) {
  const { t, formatNumber, formatDate } = useI18n()
  if (!overview) return <div className="text-slate-400">{t.common.loading}</div>
  const { kpis, divisions, resources, recommendations, recent_events } = overview

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold text-white">{t.dashboard.title}</h2>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={onRefresh}>
            <RefreshCw className="h-4 w-4" />
            {t.common.refresh}
          </Button>
          <Button onClick={onRunCycle}>
            <Play className="h-4 w-4" />
            {t.common.runCycle}
          </Button>
          <Button variant="secondary" onClick={onSelfHeal}>
            <Sparkles className="h-4 w-4" />
            {t.common.selfHeal}
          </Button>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.activeWorkers}</div>
          <div className="mt-1 text-2xl font-bold text-sky-400">
            {formatNumber(kpis.active_workers)} / {formatNumber(4)}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            {formatNumber(kpis.idle_workers)} {t.common.idle}
          </div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.completionRate}</div>
          <div className="mt-1 text-2xl font-bold text-emerald-400">{formatNumber(kpis.completion_rate)}%</div>
          <div className="mt-2">
            <Progress value={kpis.completion_rate} />
          </div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.healthyProviders}</div>
          <div className="mt-1 text-2xl font-bold text-indigo-400">
            {formatNumber(kpis.healthy_providers)} / {formatNumber(kpis.total_providers)}
          </div>
          <div className="mt-1 text-xs text-slate-500">Arena + Anthropic</div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.runningBrowsers}</div>
          <div className="mt-1 text-2xl font-bold text-cyan-400">
            {formatNumber(kpis.running_browsers)} / {formatNumber(kpis.total_browsers)}
          </div>
          <div className="mt-1 text-xs text-slate-500">Persistent Profiles</div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.pendingApprovals}</div>
          <div className="mt-1 text-2xl font-bold text-amber-400">{formatNumber(kpis.pending_approvals)}</div>
          <div className="mt-1 text-xs text-slate-500">Approval Center</div>
        </Card>
      </div>

      {/* 12 Divisions Matrix */}
      <Card>
        <h3 className="mb-3 text-sm font-semibold text-slate-200">{t.dashboard.divisionsMatrix}</h3>
        <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
          {divisions.map((d: any) => (
            <div key={d.code} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs font-bold text-sky-400">{d.code}</span>
                <span className="text-xs text-slate-400">{formatNumber(d.progress_percent)}%</span>
              </div>
              <Progress value={d.progress_percent} className="mt-2" />
              <div className="mt-1.5 text-[11px] text-slate-500">
                {formatNumber(d.completed_tasks)} / {formatNumber(d.total_tasks)} {t.common.tasksLabel}
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* Resources + Intelligence + Live Events */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-200">
            <Cpu className="h-4 w-4 text-sky-400" />
            {t.dashboard.systemResources}
          </h3>
          <div className="space-y-3 text-xs">
            <div>
              <div className="mb-1 flex justify-between text-slate-300">
                <span>CPU ({resources.cpu_count} {t.common.vcpu})</span>
                <span>{formatNumber(resources.cpu_percent)}%</span>
              </div>
              <Progress value={resources.cpu_percent} />
            </div>
            <div>
              <div className="mb-1 flex justify-between text-slate-300">
                <span>
                  RAM ({resources.ram_used_gb} / {resources.ram_total_gb} GB)
                </span>
                <span>{formatNumber(resources.ram_percent)}%</span>
              </div>
              <Progress value={resources.ram_percent} />
            </div>
            <div>
              <div className="mb-1 flex justify-between text-slate-300">
                <span>Disk ({resources.disk_free_gb} {t.common.gbFree})</span>
                <span>{formatNumber(resources.disk_percent)}%</span>
              </div>
              <Progress value={resources.disk_percent} />
            </div>
          </div>
        </Card>

        <Card>
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-200">
            <ShieldAlert className="h-4 w-4 text-amber-400" />
            {t.dashboard.intelligence}
          </h3>
          <ul className="space-y-2 text-xs text-slate-300">
            {recommendations.map((rec: string, i: number) => (
              <li key={i} className="rounded-lg border border-slate-800 bg-slate-950/60 p-2.5">
                {rec}
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-200">
            <Activity className="h-4 w-4 text-emerald-400" />
            {t.dashboard.liveFeed}
          </h3>
          <div className="max-h-48 space-y-2 overflow-y-auto text-xs">
            {recent_events.length === 0 ? (
              <div className="text-slate-500">{t.common.noData}</div>
            ) : (
              recent_events
                .slice()
                .reverse()
                .map((ev: any) => (
                  <div key={ev.event_id} className="rounded border border-slate-800/80 bg-slate-950/50 p-2">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-sky-400">{ev.type}</span>
                      <span className="text-[10px] text-slate-500">{formatDate(ev.timestamp)}</span>
                    </div>
                  </div>
                ))
            )}
          </div>
        </Card>
      </div>
    </div>
  )
}

export function ManagerChatView({
  messages,
  projectId,
  onReload,
}: {
  messages: any[]
  projectId: string | null
  onReload: () => void
}) {
  const { t, formatDate } = useI18n()
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)

  const sendDirective = async (msgText: string) => {
    if (!msgText.trim()) return
    setSending(true)
    try {
      await apiFetch('/chat', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, content: msgText }),
      })
      setText('')
      onReload()
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
          <MessageSquare className="h-5 w-5 text-sky-400" />
          {t.chat.title}
        </h2>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" onClick={() => sendDirective('orchestrate all ready tasks')}>
            {t.chat.quickOrchestrate}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => sendDirective('create task verify automated QA coverage')}>
            {t.chat.quickPlanTask}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => sendDirective('merge main branch release')}>
            {t.chat.quickProtectedMerge}
          </Button>
        </div>
      </div>

      <Card className="flex h-[520px] flex-col justify-between">
        <div className="flex-1 space-y-3 overflow-y-auto pe-2">
          {messages.map((m) => (
            <div
              key={m.id}
              className={`rounded-xl border p-3 text-sm ${
                m.sender_type === 'user'
                  ? 'ms-8 border-sky-500/30 bg-sky-950/30 text-sky-100'
                  : 'me-8 border-slate-800 bg-slate-950/80 text-slate-200'
              }`}
            >
              <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
                <span className="font-semibold text-sky-400">{m.sender_id}</span>
                <span>{formatDate(m.created_at)}</span>
              </div>
              <div className="whitespace-pre-wrap">{m.content}</div>
            </div>
          ))}
        </div>

        <form
          className="mt-4 flex gap-2 border-t border-slate-800 pt-3"
          onSubmit={(e) => {
            e.preventDefault()
            sendDirective(text)
          }}
        >
          <Input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t.chat.placeholder}
            disabled={sending}
          />
          <Button type="submit" disabled={sending || !text.trim()}>
            {t.chat.send}
          </Button>
        </form>
      </Card>
    </div>
  )
}

export function AgentsView({ agents, onReload }: { agents: any[]; onReload: () => void }) {
  const { t, formatNumber } = useI18n()

  const toggleStatus = async (ag: any) => {
    const nextStatus = ag.status === 'paused' ? 'idle' : 'paused'
    await apiFetch(`/agents/${ag.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: nextStatus }),
    })
    onReload()
  }

  const restart = async (id: string) => {
    await apiFetch(`/agents/${id}/restart`, { method: 'POST' })
    onReload()
  }

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold text-white">{t.agents.title}</h2>
      <div className="grid gap-4 md:grid-cols-2">
        {agents.map((ag) => (
          <Card key={ag.id} className="space-y-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2.5">
                <div className="rounded-lg bg-sky-500/15 p-2 text-sky-400">
                  <Bot className="h-5 w-5" />
                </div>
                <div>
                  <div className="font-semibold text-white">{ag.name}</div>
                  <div className="text-xs text-slate-400">{ag.specialty}</div>
                </div>
              </div>
              <Badge variant={ag.status === 'working' ? 'success' : ag.status === 'paused' ? 'warning' : 'info'}>
                {ag.status}
              </Badge>
            </div>

            <div className="flex flex-wrap gap-1.5">
              {(ag.divisions || []).map((d: string) => (
                <Badge key={d} variant="neutral">
                  {d}
                </Badge>
              ))}
            </div>

            <div className="grid grid-cols-3 gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 text-xs">
              <div>
                <div className="text-slate-500">Model</div>
                <div className="truncate font-mono text-slate-200">{ag.model_name}</div>
              </div>
              <div>
                <div className="text-slate-500">Branch</div>
                <div className="truncate font-mono text-sky-400">{ag.git_branch || 'main'}</div>
              </div>
              <div>
                <div className="text-slate-500">{t.agents.completedCount}</div>
                <div className="font-semibold text-emerald-400">{formatNumber(ag.tasks_completed)}</div>
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" onClick={() => toggleStatus(ag)}>
                {ag.status === 'paused' ? t.agents.resume : t.agents.pause}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => restart(ag.id)}>
                <RotateCcw className="h-3.5 w-3.5" />
                {t.agents.restart}
              </Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}

export function TasksView({
  tasks,
  projectId,
  onReload,
}: {
  tasks: any[]
  projectId: string | null
  onReload: () => void
}) {
  const { t } = useI18n()
  const [title, setTitle] = useState('')
  const [division, setDivision] = useState('D08')
  const [priority, setPriority] = useState('high')
  const [executionMode, setExecutionMode] = useState('automatic')

  const createTask = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projectId || !title.trim()) return
    await apiFetch('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        project_id: projectId,
        division_code: division,
        title,
        priority,
        execution_mode: executionMode,
      }),
    })
    setTitle('')
    onReload()
  }

  const updateTaskStatus = async (taskId: string, status: string) => {
    await apiFetch(`/tasks/${taskId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    })
    onReload()
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-semibold text-white">{t.tasks.title}</h2>
        <Button onClick={() => apiFetch('/orchestration/cycle', { method: 'POST' }).then(onReload)}>
          <Play className="h-4 w-4" />
          {t.common.runCycle}
        </Button>
      </div>

      <Card>
        <form onSubmit={createTask} className="grid gap-3 md:grid-cols-5">
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t.tasks.taskTitle}
            className="md:col-span-2"
          />
          <Select value={division} onChange={(e) => setDivision(e.target.value)}>
            {Array.from({ length: 12 }, (_, i) => `D${String(i + 1).padStart(2, '0')}`).map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </Select>
          <Select value={executionMode} onChange={(e) => setExecutionMode(e.target.value)}>
            <option value="automatic">{t.tasks.modeAuto}</option>
            <option value="manual">{t.tasks.modeManual}</option>
            <option value="collaborative">{t.tasks.modeCollaborative}</option>
          </Select>
          <div className="flex gap-2">
            <Select value={priority} onChange={(e) => setPriority(e.target.value)} className="flex-1">
              <option value="critical">{t.tasks.priorityCritical}</option>
              <option value="high">{t.tasks.priorityHigh}</option>
              <option value="medium">{t.tasks.priorityMedium}</option>
              <option value="low">{t.tasks.priorityLow}</option>
            </Select>
            <Button type="submit">{t.common.create}</Button>
          </div>
        </form>
      </Card>

      <div className="space-y-2.5">
        {tasks.map((task) => (
          <Card key={task.id} className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="info">{task.division_code}</Badge>
                <Badge
                  variant={
                    task.status === 'completed'
                      ? 'success'
                      : task.status === 'failed'
                        ? 'danger'
                        : task.status === 'in_progress'
                          ? 'warning'
                          : 'neutral'
                  }
                >
                  {task.status}
                </Badge>
                <span className="font-medium text-white">{task.title}</span>
              </div>
              <div className="text-xs text-slate-400">
                Agent: <span className="font-mono text-sky-400">{task.assigned_agent_id || 'unassigned'}</span> • Priority:{' '}
                {task.priority} • Retries: {task.retry_count}/{task.max_retries}
              </div>
            </div>
            <div className="flex gap-2">
              {task.status !== 'completed' && (
                <Button size="sm" variant="secondary" onClick={() => updateTaskStatus(task.id, 'completed')}>
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                  {t.tasks.complete}
                </Button>
              )}
              {task.status === 'in_progress' && (
                <Button size="sm" variant="danger" onClick={() => updateTaskStatus(task.id, 'failed')}>
                  {t.tasks.failRetry}
                </Button>
              )}
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
