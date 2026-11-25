/**
 * NEXUS Projects & Tasks workspace (owner spec §7): Kanban, list and timeline views of the real task list,
 * plus a task details drawer (dependencies, priority, assignee, due date, PR, execution log) with
 * status change (PATCH /tasks/{id} status) and reassignment (PATCH /tasks/{id} assigned_agent_id).
 */
import React, { useMemo, useState } from 'react'
import { CalendarClock, Columns3, GitPullRequest, List, ListTree, Search } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Badge, Button, cn, Input, Select } from '../../components/ui'
import { Drawer, Field, fill, useAction, useRelativeTime } from '../../components/nexus'

type Column = 'queued' | 'active' | 'review' | 'done' | 'problem'
export const COLUMN_OF: Record<string, Column> = {
  pending: 'queued', queued: 'queued', assigned: 'active', in_progress: 'active',
  pr_open: 'review', in_review: 'review', changes_requested: 'review', approved: 'review',
  merged: 'done', done: 'done', completed: 'done', cancelled: 'done',
  failed: 'problem', blocked: 'problem',
}
const COLUMNS: Column[] = ['queued', 'active', 'review', 'done', 'problem']
const COLUMN_ACCENT: Record<Column, string> = {
  queued: 'bg-nexus-dim', active: 'bg-nexus-cyan', review: 'bg-nexus-violet', done: 'bg-nexus-success', problem: 'bg-nexus-error',
}
const PRIORITY_VARIANT: Record<string, 'danger' | 'warning' | 'info' | 'neutral'> = { critical: 'danger', high: 'warning', medium: 'info', low: 'neutral' }
const STATUS_CHOICES = ['queued', 'blocked', 'completed', 'cancelled'] as const

function toDate(value?: string | null) {
  if (!value) return null
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

function PriorityBadge({ priority }: { priority: string }) {
  const { t } = useI18n()
  const label = ({ critical: t.tasks.priorityCritical, high: t.tasks.priorityHigh, medium: t.tasks.priorityMedium, low: t.tasks.priorityLow } as any)[priority] ?? priority
  return <Badge variant={PRIORITY_VARIANT[priority] ?? 'neutral'} className="px-2 py-0 text-[10px]">{label}</Badge>
}

function TaskCard({ task, agent, project, onOpen }: { task: any; agent: any; project: any; onOpen: () => void }) {
  const { t } = useI18n()
  return (
    <button type="button" onClick={onOpen} data-testid={`task-card-${task.id}`}
      className="w-full rounded-lg border border-nexus-border/70 bg-nexus-bg/50 p-3 text-start transition-colors hover:border-nexus-cyan/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
      <div className="flex items-start justify-between gap-2">
        <p className="line-clamp-2 text-sm font-medium text-nexus-text">{task.title}</p>
        <PriorityBadge priority={task.priority} />
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-nexus-dim">
        <span className="truncate">{agent?.name ?? t.nexus.board.unassigned}</span>
        {project && <span className="truncate">{project.name}</span>}
        {(task.dependencies?.length ?? 0) > 0 && <span className="inline-flex items-center gap-1"><ListTree className="h-3 w-3" />{task.dependencies.length}</span>}
        {task.pr_number && <span className="inline-flex items-center gap-1" dir="ltr"><GitPullRequest className="h-3 w-3" />#{task.pr_number}</span>}
      </div>
      {task.progress_percent > 0 && task.progress_percent < 100 && (
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-nexus-raised" aria-hidden><div className="h-full bg-nexus-cyan" style={{ width: `${task.progress_percent}%` }} /></div>
      )}
    </button>
  )
}

function Kanban({ tasks, agentsById, projectsById, onOpen }: { tasks: any[]; agentsById: Record<string, any>; projectsById: Record<string, any>; onOpen: (id: string) => void }) {
  const { t, formatNumber } = useI18n()
  const grouped = useMemo(() => {
    const out: Record<Column, any[]> = { queued: [], active: [], review: [], done: [], problem: [] }
    for (const task of tasks) out[COLUMN_OF[task.status] ?? 'queued'].push(task)
    return out
  }, [tasks])
  return (
    <div className="-mx-1 flex snap-x gap-3 overflow-x-auto px-1 pb-2" data-testid="task-kanban">
      {COLUMNS.map((col) => (
        <section key={col} className="flex w-72 shrink-0 snap-start flex-col rounded-xl border border-nexus-border/70 bg-nexus-card/80 md:w-auto md:min-w-[15rem] md:flex-1" data-testid={`kanban-${col}`}>
          <header className="flex items-center justify-between gap-2 border-b border-nexus-border/60 px-3 py-2.5">
            <h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-nexus-muted">
              <span className={cn('h-2 w-2 rounded-full', COLUMN_ACCENT[col])} aria-hidden />{t.nexus.board.columns[col]}
            </h3>
            <span className="rounded-full bg-nexus-raised px-2 text-[11px] tabular-nums text-nexus-muted">{formatNumber(grouped[col].length)}</span>
          </header>
          <div className="max-h-[60vh] min-h-[6rem] space-y-2 overflow-y-auto p-2">
            {grouped[col].length === 0 && <p className="px-1 py-4 text-center text-xs text-nexus-dim">{t.nexus.board.empty}</p>}
            {grouped[col].map((task) => (
              <TaskCard key={task.id} task={task} agent={agentsById[task.assigned_agent_id]} project={projectsById[task.project_id]} onOpen={() => onOpen(task.id)} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function TaskList({ tasks, agentsById, projectsById, onOpen }: { tasks: any[]; agentsById: Record<string, any>; projectsById: Record<string, any>; onOpen: (id: string) => void }) {
  const { t, formatDate } = useI18n()
  const b = t.nexus.board
  if (!tasks.length) return <p className="rounded-xl border border-nexus-border bg-nexus-card p-6 text-center text-sm text-nexus-muted">{b.empty}</p>
  return (
    <div className="overflow-x-auto rounded-xl border border-nexus-border/80 bg-nexus-card" data-testid="task-list">
      <table className="w-full min-w-[44rem] text-sm">
        <thead className="border-b border-nexus-border text-xs text-nexus-dim">
          <tr>
            <th className="px-3 py-2.5 text-start font-medium">{t.tasks.taskTitle}</th>
            <th className="px-3 py-2.5 text-start font-medium">{t.nexus.common.status}</th>
            <th className="px-3 py-2.5 text-start font-medium">{b.priority}</th>
            <th className="px-3 py-2.5 text-start font-medium">{b.assignee}</th>
            <th className="px-3 py-2.5 text-start font-medium">{b.project}</th>
            <th className="px-3 py-2.5 text-start font-medium">{b.created}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-nexus-border/50">
          {tasks.map((task) => (
            <tr key={task.id} className="cursor-pointer hover:bg-nexus-raised/50" onClick={() => onOpen(task.id)}>
              <td className="px-3 py-2"><button type="button" className="text-start font-medium text-nexus-text hover:text-nexus-cyan focus-visible:outline-none focus-visible:underline" onClick={(e) => { e.stopPropagation(); onOpen(task.id) }}>{task.title}</button></td>
              <td className="px-3 py-2 text-xs text-nexus-muted">{b.columns[COLUMN_OF[task.status] ?? 'queued']} <span className="text-nexus-dim" dir="ltr">({task.status})</span></td>
              <td className="px-3 py-2"><PriorityBadge priority={task.priority} /></td>
              <td className="px-3 py-2 text-xs text-nexus-muted">{agentsById[task.assigned_agent_id]?.name ?? b.unassigned}</td>
              <td className="px-3 py-2 text-xs text-nexus-muted">{projectsById[task.project_id]?.name ?? '—'}</td>
              <td className="px-3 py-2 text-xs text-nexus-dim">{task.created_at ? formatDate(task.created_at) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Horizontal bars from start (or creation) to finish (or now), on one shared time axis. */
function Timeline({ tasks, onOpen }: { tasks: any[]; onOpen: (id: string) => void }) {
  const { t, formatDate } = useI18n()
  const rows = tasks
    .map((task) => {
      const start = toDate(task.started_at) ?? (COLUMN_OF[task.status] !== 'queued' ? toDate(task.created_at) : null)
      const end = toDate(task.completed_at) ?? (COLUMN_OF[task.status] === 'done' ? toDate(task.updated_at) : new Date())
      return start ? { task, start: start.getTime(), end: Math.max(end?.getTime() ?? Date.now(), start.getTime() + 60_000) } : null
    })
    .filter(Boolean) as Array<{ task: any; start: number; end: number }>
  if (!rows.length) return <p className="rounded-xl border border-nexus-border bg-nexus-card p-6 text-center text-sm text-nexus-muted" data-testid="task-timeline">{t.nexus.board.timelineEmpty}</p>
  rows.sort((a, b) => a.start - b.start)
  const min = rows[0].start
  const max = Math.max(...rows.map((r) => r.end))
  const span = Math.max(1, max - min)
  return (
    <div className="rounded-xl border border-nexus-border/80 bg-nexus-card p-4" data-testid="task-timeline">
      <div className="mb-3 flex justify-between text-[11px] text-nexus-dim"><span>{formatDate(new Date(min).toISOString())}</span><span>{formatDate(new Date(max).toISOString())}</span></div>
      <ol className="space-y-2">
        {rows.map(({ task, start, end }) => {
          const col = COLUMN_OF[task.status] ?? 'queued'
          return (
            <li key={task.id} className="grid grid-cols-[minmax(8rem,30%)_1fr] items-center gap-3">
              <button type="button" onClick={() => onOpen(task.id)} className="truncate text-start text-xs text-nexus-text hover:text-nexus-cyan">{task.title}</button>
              <div className="relative h-5 rounded bg-nexus-raised/60">
                <div className={cn('absolute inset-y-0.5 rounded', COLUMN_ACCENT[col])}
                  style={{ insetInlineStart: `${((start - min) / span) * 100}%`, width: `${Math.max(1.5, ((end - start) / span) * 100)}%` }}
                  title={`${formatDate(new Date(start).toISOString())} → ${formatDate(new Date(end).toISOString())}`} />
              </div>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function TaskDrawer({ task, tasks, agents, projects, onClose, onChanged }: {
  task: any; tasks: any[]; agents: any[]; projects: any[]; onClose: () => void; onChanged: () => void
}) {
  const { t, formatDate, formatNumber } = useI18n()
  const b = t.nexus.board
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const [status, setStatus] = useState('')
  const [assignee, setAssignee] = useState(task.assigned_agent_id ?? '')
  const agent = agents.find((a) => a.id === task.assigned_agent_id)
  const project = projects.find((p) => p.id === task.project_id)
  const deps = (task.dependencies ?? []).map((id: string) => tasks.find((x) => x.id === id) ?? { id, title: id, status: '?' })
  const log: any[] = Array.isArray(task.checkpoints) ? [...task.checkpoints].reverse() : []
  const workers = agents.filter((a) => a.role !== 'manager')
  const statusLabel = (s: string) => ({ queued: b.columns.queued, blocked: b.columns.problem, completed: b.columns.done, cancelled: b.cancelled } as Record<string, string>)[s] ?? s

  const applyStatus = () => {
    if (!status) return
    const destructive = status === 'cancelled' || status === 'completed'
    run('status', () => apiFetch(`/tasks/${task.id}`, { method: 'PATCH', body: JSON.stringify({ status }) }), {
      confirm: destructive ? { message: `${b.changeStatus}: ${task.title} → ${status}`, danger: status === 'cancelled' } : undefined,
      success: fill(b.statusSaved, { status }), onDone: () => { setStatus(''); onChanged() },
    })
  }
  const applyAssignee = () => {
    if (!assignee || assignee === task.assigned_agent_id) return
    const name = agents.find((a) => a.id === assignee)?.name ?? assignee
    run('assignee', () => apiFetch(`/tasks/${task.id}`, { method: 'PATCH', body: JSON.stringify({ assigned_agent_id: assignee }) }), {
      success: fill(b.reassigned, { name }), onDone: onChanged,
    })
  }

  return (
    <Drawer open title={task.title} onClose={onClose} testId="task-drawer" wide>
      <dl>
        <Field label={t.nexus.common.status}>{b.columns[COLUMN_OF[task.status] ?? 'queued']} <span className="text-nexus-dim" dir="ltr">({task.status})</span></Field>
        <Field label={b.priority}><PriorityBadge priority={task.priority} /></Field>
        <Field label={b.assignee}>{agent?.name ?? b.unassigned}</Field>
        <Field label={b.project}>{project?.name ?? '—'}</Field>
        <Field label={b.created}>{task.created_at ? formatDate(task.created_at) : '—'}</Field>
        <Field label={b.started}>{task.started_at ? formatDate(task.started_at) : '—'}</Field>
        <Field label={b.finished}>{task.completed_at ? formatDate(task.completed_at) : '—'}</Field>
        <Field label={t.nexus.board.estimate}>{task.estimated_minutes ? fill(t.nexus.common.minutes, { n: task.estimated_minutes }, formatNumber) : '—'}</Field>
        {task.deadline && <Field label={b.due}>{formatDate(task.deadline)}</Field>}
        <Field label={b.pr}>{task.pr_url ? <a href={task.pr_url} target="_blank" rel="noreferrer" className="text-nexus-cyan hover:underline" dir="ltr">#{task.pr_number ?? ''} {task.pr_status ?? ''}</a> : '—'}</Field>
        {task.error_message && <Field label={b.error}><span className="text-nexus-error">{task.error_message}</span></Field>}
      </dl>

      {task.description && (
        <section className="mt-4">
          <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-nexus-dim">{b.description}</h3>
          <p className="whitespace-pre-wrap rounded-lg border border-nexus-border/60 bg-nexus-bg/40 p-3 text-sm text-nexus-muted">{task.description}</p>
        </section>
      )}

      <section className="mt-4">
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-nexus-dim">{b.dependencies}</h3>
        {deps.length === 0 ? <p className="text-sm text-nexus-muted">{b.noDependencies}</p> : (
          <ul className="space-y-1">{deps.map((d: any) => (
            <li key={d.id} className="flex items-center justify-between gap-2 rounded-lg bg-nexus-bg/40 px-3 py-1.5 text-xs"><span className="truncate text-nexus-text">{d.title}</span><span className="text-nexus-dim">{d.status}</span></li>
          ))}</ul>
        )}
      </section>

      <section className="mt-4 grid gap-3 rounded-xl border border-nexus-border/70 bg-nexus-card p-3 sm:grid-cols-2">
        <div className="space-y-2">
          <label className="block text-xs text-nexus-muted" htmlFor="task-status">{b.changeStatus}</label>
          <div className="flex gap-2">
            <Select id="task-status" value={status} onChange={(e) => setStatus(e.target.value)} className="min-w-0 flex-1" data-testid="task-status-select">
              <option value="">—</option>
              {STATUS_CHOICES.filter((s) => s !== task.status).map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
            </Select>
            <Button size="sm" onClick={applyStatus} disabled={!status || isBusy('status')} aria-busy={isBusy('status')} data-testid="task-status-apply">{t.nexus.common.save}</Button>
          </div>
        </div>
        <div className="space-y-2">
          <label className="block text-xs text-nexus-muted" htmlFor="task-assignee">{b.reassign}</label>
          <div className="flex gap-2">
            <Select id="task-assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)} className="min-w-0 flex-1" data-testid="task-assignee-select">
              <option value="">{b.unassigned}</option>
              {workers.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </Select>
            <Button size="sm" onClick={applyAssignee} disabled={!assignee || assignee === task.assigned_agent_id || isBusy('assignee')} aria-busy={isBusy('assignee')} data-testid="task-assignee-apply">{t.nexus.common.save}</Button>
          </div>
        </div>
      </section>

      <section className="mt-4">
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-nexus-dim">{b.executionLog}</h3>
        {log.length === 0 ? <p className="text-sm text-nexus-muted">{b.noLog}</p> : (
          <ol className="space-y-1.5" data-testid="task-log">
            {log.slice(0, 40).map((entry, i) => (
              <li key={i} className="rounded-lg border border-nexus-border/50 bg-nexus-bg/40 px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-nexus-text" dir="ltr">{entry.event ?? entry.kind ?? entry.status ?? '—'}</span>
                  <time className="text-nexus-dim">{ago(entry.ts ?? entry.timestamp)}</time>
                </div>
                {(entry.message || entry.detail || entry.reason) && <p className="mt-1 text-nexus-muted">{entry.message ?? entry.detail ?? entry.reason}</p>}
              </li>
            ))}
          </ol>
        )}
      </section>
    </Drawer>
  )
}

export function TaskBoard({ tasks, agents, projects, projectId, onReload, openTaskId, onOpenTask }: {
  tasks: any[]; agents: any[]; projects: any[]; projectId: string | null; onReload: () => void
  openTaskId: string | null; onOpenTask: (id: string | null) => void
}) {
  const { t } = useI18n()
  const b = t.nexus.board
  const [view, setView] = useState('kanban')
  const [query, setQuery] = useState('')
  const [project, setProject] = useState<string>('all')
  const [priority, setPriority] = useState('all')
  const agentsById = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a])), [agents])
  const projectsById = useMemo(() => Object.fromEntries(projects.map((p) => [p.id, p])), [projects])
  const effectiveProject = project === 'current' ? projectId : project
  const visible = useMemo(() => tasks.filter((task) => {
    const q = query.trim().toLowerCase()
    return (!q || [task.title, task.description, task.id].some((v) => String(v ?? '').toLowerCase().includes(q)))
      && (effectiveProject === 'all' || !effectiveProject || task.project_id === effectiveProject)
      && (priority === 'all' || task.priority === priority)
  }), [tasks, query, effectiveProject, priority])
  const opened = tasks.find((x) => x.id === openTaskId) ?? null
  const icons: Record<string, React.ReactNode> = { kanban: <Columns3 className="h-4 w-4" />, list: <List className="h-4 w-4" />, timeline: <CalendarClock className="h-4 w-4" /> }

  return (
    <div className="space-y-4" data-testid="task-board">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex rounded-lg border border-nexus-border bg-nexus-raised/50 p-0.5" role="group" aria-label={b.kanban}>
          {(['kanban', 'list', 'timeline'] as const).map((id) => (
            <button key={id} type="button" aria-pressed={view === id} onClick={() => setView(id)} data-testid={`board-view-${id}`}
              className={cn('flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan',
                view === id ? 'bg-nexus-cyan text-nexus-on-accent' : 'text-nexus-muted hover:text-nexus-text')}>
              {icons[id]}{b[id]}
            </button>
          ))}
        </div>
        <label className="relative min-w-[12rem] flex-1 sm:max-w-xs">
          <span className="sr-only">{b.filterTasks}</span>
          <Search className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-nexus-dim" aria-hidden />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={b.filterTasks} className="ps-9" data-testid="task-search" />
        </label>
        <Select value={project} onChange={(e) => setProject(e.target.value)} aria-label={b.project} data-testid="task-project-filter">
          <option value="all">{b.allProjects}</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        <Select value={priority} onChange={(e) => setPriority(e.target.value)} aria-label={b.priority}>
          <option value="all">{b.allPriorities}</option>
          {['critical', 'high', 'medium', 'low'].map((p) => <option key={p} value={p}>{p}</option>)}
        </Select>
      </div>
      {view === 'kanban' && <Kanban tasks={visible} agentsById={agentsById} projectsById={projectsById} onOpen={onOpenTask} />}
      {view === 'list' && <TaskList tasks={visible} agentsById={agentsById} projectsById={projectsById} onOpen={onOpenTask} />}
      {view === 'timeline' && <Timeline tasks={visible} onOpen={onOpenTask} />}
      {opened && <TaskDrawer task={opened} tasks={tasks} agents={agents} projects={projects} onClose={() => onOpenTask(null)} onChanged={onReload} />}
    </div>
  )
}
