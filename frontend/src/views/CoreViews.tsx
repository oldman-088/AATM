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
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'
import { messageText } from '../lib/chat'

export function DashboardView({
  overview,
  error,
  onRefresh,
  onRunCycle,
  onSelfHeal,
  onOpenProvidersWizard,
}: {
  overview: any
  error?: string | null
  onRefresh: () => void
  onRunCycle: () => void
  onSelfHeal: () => void
  onOpenProvidersWizard?: () => void
}) {
  const { t, formatNumber, formatDate } = useI18n()
  if (error) return <EmptyState variant="error" onRetry={onRefresh} testId="dashboard-error" />
  if (!overview) return <EmptyState variant="loading" testId="dashboard-loading" />
  const { kpis, divisions, resources, recommendations, recent_events, sandbox, model_status } = overview

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

      {model_status && model_status.configured === false && (
        <Card className="border-rose-500/60 bg-rose-950/40">
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <div className="flex items-center gap-2 text-sm font-semibold text-rose-200">
                <ShieldAlert className="h-4 w-4 text-rose-400" />
                <Badge variant="danger">{t.dashboard.noModelBannerTitle}</Badge>
                {model_status.reason && <span className="text-xs font-normal text-rose-200/90">{model_status.reason}</span>}
              </div>
              <p className="text-xs text-rose-100/90">{t.dashboard.noModelBannerText}</p>
            </div>
            {onOpenProvidersWizard && (
              <Button size="sm" onClick={onOpenProvidersWizard}>
                <Cpu className="h-4 w-4" />
                {t.dashboard.openSetupWizard}
              </Button>
            )}
          </div>
        </Card>
      )}

      {sandbox && (
        <Card
          className={
            sandbox.status === 'SANDBOX_UNAVAILABLE'
              ? 'border-rose-500/40 bg-rose-950/30'
              : 'border-emerald-500/30 bg-emerald-950/20'
          }
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
              {sandbox.status === 'SANDBOX_UNAVAILABLE' ? (
                <ShieldAlert className="h-4 w-4 text-rose-400" />
              ) : (
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              )}
              <span>{t.dashboard.sandboxStatus}:</span>
              <Badge variant={sandbox.status === 'SANDBOX_UNAVAILABLE' ? 'danger' : 'success'}>
                {sandbox.status === 'SANDBOX_UNAVAILABLE' ? t.dashboard.sandboxUnavailable : t.dashboard.sandboxReady}
              </Badge>
              <span className="text-xs text-slate-400">
                ({t.dashboard.sandboxModeLabel}: {sandbox.mode}
                {sandbox.image ? ` · ${sandbox.image}` : ''})
              </span>
            </div>
            {sandbox.status === 'SANDBOX_UNAVAILABLE' && (
              <div className="text-xs text-rose-200">
                {sandbox.reason || t.dashboard.sandboxBuildHint}
              </div>
            )}
          </div>
          {sandbox.status === 'SANDBOX_UNAVAILABLE' && (
            <div className="mt-1 font-mono text-[11px] text-rose-300/90">{t.dashboard.sandboxBuildHint}</div>
          )}
        </Card>
      )}

      {/* KPI Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.activeWorkers}</div>
          <div className="mt-1 text-2xl font-bold text-sky-400">
            {formatNumber(kpis.active_workers)} / {formatNumber(kpis.total_agents)}
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
          <div className="mt-1 text-xs text-slate-500">{t.dashboard.providersSublabel}</div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.runningBrowsers}</div>
          <div className="mt-1 text-2xl font-bold text-cyan-400">
            {formatNumber(kpis.running_browsers)} / {formatNumber(kpis.total_browsers)}
          </div>
          <div className="mt-1 text-xs text-slate-500">{t.dashboard.profilesSublabel}</div>
        </Card>
        <Card>
          <div className="text-xs text-slate-400">{t.dashboard.pendingApprovals}</div>
          <div className="mt-1 text-2xl font-bold text-amber-400">{formatNumber(kpis.pending_approvals)}</div>
          <div className="mt-1 text-xs text-slate-500">{t.dashboard.approvalsSublabel}</div>
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
  projects,
  onSelectProject,
  error,
  onReload,
  onChatCleared,
}: {
  messages: any[]
  projectId: string | null
  projects?: any[]
  onSelectProject?: (id: string) => void
  error?: string | null
  onReload: () => void
  onChatCleared?: () => void
}) {
  const { t, formatDate } = useI18n()
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)
  // Only an active project takes new requests; completed and disabled ones stay on the Projects screen.
  const chatProjects = (projects || []).filter((p: any) => (p.status ?? 'active') === 'active' || p.id === projectId)
  const selected = (projects || []).find((p: any) => p.id === projectId)
  const inactive = selected && (selected.status ?? 'active') !== 'active' ? selected : null

  const clearChat = async () => {
    if (!window.confirm(t.bridge.clearChatConfirm)) return
    setSendError(null)
    try {
      await apiFetch('/chat', { method: 'DELETE' })
      onChatCleared?.()
      onReload()
    } catch (error: any) {
      setSendError(error?.message || t.chat.projectRequired)
    }
  }

  const startNewConversation = async () => {
    setSendError(null)
    try {
      await apiFetch('/arena/manager/new-session', { method: 'POST', body: JSON.stringify({}) })
      onReload()
    } catch (error: any) {
      setSendError(error?.message || t.chat.projectRequired)
    }
  }

  const sendDirective = async (msgText: string) => {
    if (!msgText.trim()) return
    if (inactive) return  // a completed or disabled project refuses new requests; the banner says so
    if (!projectId && (projects?.length ?? 0) > 1) {
      setSendError(t.chat.projectRequired)
      return
    }
    setSending(true)
    setSendError(null)
    try {
      await apiFetch('/chat', {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId, content: msgText }),
      })
      setText('')
      onReload()
    } catch (error: any) {
      setSendError(error?.message || t.chat.projectRequired)
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
        <div className="flex flex-wrap items-center gap-2">
          {chatProjects.length > 1 && onSelectProject && (
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <span>{t.chat.projectLabel}</span>
              <select
                aria-label={t.chat.projectLabel}
                className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-white"
                value={projectId ?? ''}
                onChange={(event) => onSelectProject(event.target.value)}
              >
                {chatProjects.map((project: any) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Button size="sm" variant="secondary" onClick={() => sendDirective('orchestrate all ready tasks')}>
            {t.chat.quickOrchestrate}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => sendDirective('create task verify automated QA coverage')}>
            {t.chat.quickPlanTask}
          </Button>
          <Button size="sm" variant="secondary" onClick={() => sendDirective('merge main branch release')}>
            {t.chat.quickProtectedMerge}
          </Button>
          <Button size="sm" variant="secondary" disabled={sending} data-testid="manager-new-conversation" onClick={startNewConversation}>
            {t.bridge.newConversation}
          </Button>
          <Button size="sm" variant="danger" disabled={sending} data-testid="manager-clear-chat" onClick={clearChat}>
            {t.bridge.clearChat}
          </Button>
        </div>
      </div>

      <Card className="flex h-[520px] flex-col justify-between">
        <div className="flex-1 space-y-3 overflow-y-auto pe-2">
          {error ? (
            <EmptyState variant="error" onRetry={onReload} testId="chat-error" />
          ) : messages.length === 0 ? (
            <EmptyState variant="empty" testId="chat-empty" />
          ) : messages.map((m) => (
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
              <div className="whitespace-pre-wrap">{messageText(m, t)}</div>
            </div>
          ))}
        </div>

        {inactive && <p data-testid="chat-inactive-project" role="alert"
          className="mt-4 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-start text-sm text-amber-100">
          {t.chat.inactiveProject.replace('{name}', inactive.name)}</p>}
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
          <Button type="submit" disabled={sending || !text.trim() || !!inactive}>
            {t.chat.send}
          </Button>
        </form>
        {sendError && (
          <p role="alert" className="mt-2 text-sm text-rose-300">
            {sendError}
          </p>
        )}
      </Card>
    </div>
  )
}

export function AgentsView({
  agents,
  providers = [],
  error,
  onReload,
}: {
  agents: any[]
  providers?: any[]
  error?: string | null
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const [modelEdits, setModelEdits] = useState<Record<string, { provider_id: string; model_name: string }>>({})
  const [savedAgentId, setSavedAgentId] = useState<string | null>(null)

  if (error) return <EmptyState variant="error" onRetry={onReload} testId="agents-error" />
  if (!agents.length) return <EmptyState variant="empty" testId="agents-empty" />

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

  const saveModel = async (ag: any) => {
    const draft = modelEdits[ag.id] || { provider_id: ag.provider_id, model_name: ag.model_name }
    await apiFetch(`/agents/${ag.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ provider_id: draft.provider_id, model_name: draft.model_name }),
    })
    setSavedAgentId(ag.id)
    onReload()
  }

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold text-white">{t.agents.title}</h2>
      <div className="grid gap-4 md:grid-cols-2">
        {agents.map((ag) => {
          const draft = modelEdits[ag.id] || {
            provider_id: ag.provider_id || '',
            model_name: ag.model_name || '',
          }
          const selectedProvider = providers.find((p) => p.id === draft.provider_id)
          return (
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
                <div className="text-slate-500">{t.agents.modelLabel}</div>
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

            {providers.length > 0 && (
              <div className="grid gap-2 rounded-lg border border-slate-800/80 bg-slate-950/40 p-2 sm:grid-cols-3">
                <Select
                  aria-label={`${ag.name} ${t.agents.providerLabel}`}
                  value={draft.provider_id}
                  onChange={(e) => {
                    const nextProv = providers.find((p) => p.id === e.target.value)
                    setModelEdits({
                      ...modelEdits,
                      [ag.id]: {
                        provider_id: e.target.value,
                        model_name: nextProv?.default_model || draft.model_name,
                      },
                    })
                  }}
                >
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </Select>
                <Input
                  aria-label={`${ag.name} ${t.agents.modelLabel}`}
                  list={`models-${ag.id}`}
                  value={draft.model_name}
                  onChange={(e) =>
                    setModelEdits({
                      ...modelEdits,
                      [ag.id]: { ...draft, model_name: e.target.value },
                    })
                  }
                />
                {selectedProvider?.available_models && (
                  <datalist id={`models-${ag.id}`}>
                    {(selectedProvider.available_models as string[]).map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                )}
                <Button size="sm" variant="secondary" onClick={() => saveModel(ag)}>
                  {t.agents.saveModelAssignment}
                </Button>
                {savedAgentId === ag.id && (
                  <span className="text-[11px] text-emerald-400 sm:col-span-3">{t.agents.modelSaved}</span>
                )}
              </div>
            )}

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
          )
        })}
      </div>
    </div>
  )
}

function taskTone(status: string): 'success' | 'danger' | 'warning' | 'info' | 'neutral' {
  if (['completed', 'done', 'merged', 'approved'].includes(status)) return 'success'
  if (['failed', 'changes_requested'].includes(status)) return 'danger'
  if (['in_progress', 'in_review'].includes(status)) return 'warning'
  if (status === 'pr_open') return 'info'
  return 'neutral'
}

export function TasksView({
  tasks,
  projectId,
  error,
  onReload,
}: {
  tasks: any[]
  projectId: string | null
  error?: string | null
  onReload: () => void
}) {
  const { t } = useI18n()
  const [title, setTitle] = useState('')
  const [division, setDivision] = useState('D08')
  const [priority, setPriority] = useState('high')
  const [executionMode, setExecutionMode] = useState('automatic')
  const [statusFilter, setStatusFilter] = useState('all')

  const doneStatuses = ['completed', 'done', 'merged', 'approved']
  const activeStatuses = ['in_progress', 'in_review', 'pr_open']
  const failedStatuses = ['failed', 'changes_requested']
  const summary = {
    total: tasks.length,
    active: tasks.filter((task) => activeStatuses.includes(task.status)).length,
    done: tasks.filter((task) => doneStatuses.includes(task.status)).length,
    failed: tasks.filter((task) => failedStatuses.includes(task.status)).length,
  }
  const statusOptions = Array.from(new Set(tasks.map((task) => String(task.status)))).sort()
  const visibleTasks = statusFilter === 'all' ? tasks : tasks.filter((task) => task.status === statusFilter)

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

      {error ? (
        <EmptyState variant="error" onRetry={onReload} testId="tasks-error" />
      ) : tasks.length === 0 ? (
        <EmptyState variant="empty" testId="tasks-empty" />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="tasks-summary">
            <Card className="p-4">
              <div className="text-xs text-slate-400">{t.tasks.summaryTotal}</div>
              <div className="text-2xl font-semibold text-white">{summary.total}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-slate-400">{t.tasks.summaryActive}</div>
              <div className="text-2xl font-semibold text-amber-300">{summary.active}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-slate-400">{t.tasks.summaryDone}</div>
              <div className="text-2xl font-semibold text-emerald-300">{summary.done}</div>
            </Card>
            <Card className="p-4">
              <div className="text-xs text-slate-400">{t.tasks.summaryFailed}</div>
              <div className="text-2xl font-semibold text-rose-300">{summary.failed}</div>
            </Card>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-slate-400">{t.tasks.filterLabel}</span>
            <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} data-testid="tasks-filter">
              <option value="all">{t.tasks.filterAll}</option>
              {statusOptions.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </Select>
          </div>

          <div className="space-y-2.5">
        {visibleTasks.map((task) => (
          <Card key={task.id} className="flex flex-wrap items-center justify-between gap-3">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="info">{task.division_code}</Badge>
                <Badge variant={taskTone(task.status)}>{task.status}</Badge>
                {task.branch_name && (
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-sky-300">
                    {task.branch_name}
                  </span>
                )}
                {task.pr_number && (
                  task.pr_url && String(task.pr_url).startsWith('https://') ? (
                    <a
                      href={task.pr_url}
                      target="_blank"
                      rel="noreferrer"
                      className="rounded bg-emerald-950/60 px-1.5 py-0.5 font-mono text-[11px] text-emerald-300 underline"
                    >
                      PR #{task.pr_number}
                    </a>
                  ) : (
                    <Badge variant="info">PR #{task.pr_number}</Badge>
                  )
                )}
                <span className="font-medium text-white">{task.title}</span>
              </div>
              <div className="text-xs text-slate-400">
                Agent: <span className="font-mono text-sky-400">{task.assigned_agent_id || 'unassigned'}</span> • Priority:{' '}
                {task.priority} • Retries: {task.retry_count}/{task.max_retries}
              </div>
            </div>
            <div className="flex gap-2">
              {!['completed', 'done', 'merged'].includes(task.status) && (
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
        </>
      )}
    </div>
  )
}
