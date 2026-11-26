/**
 * NEXUS AI Executive Director: plans and execution (owner spec §6).
 * Each manager plan job (GET /arena/manager/jobs) is drawn as numbered steps: the tasks it created
 * (linked through the task checkpoint `manager_job_id`), their assignee, dependencies, status and progress,
 * any error, and whether the plan waits for the owner's approval. An active job can be cancelled
 * (POST /arena/manager/jobs/{id}/cancel). Nothing here is simulated.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleDashed, Crown, GitBranch, Loader2, XCircle } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Badge, Button, Progress, cn } from '../../components/ui'
import { fill, SectionCard, useAction, useRelativeTime } from '../../components/nexus'
import { COLUMN_OF } from './TaskBoard'
import type { Route } from '../../shell/sections'

const ACTIVE_JOB = ['queued', 'running', 'waiting_connection', 'needs_human', 'draft', 'sent', 'awaiting_reply']

function planIdOf(task: any): string | null {
  const cps: any[] = Array.isArray(task?.checkpoints) ? task.checkpoints : []
  return cps.find((c) => c?.manager_job_id)?.manager_job_id ?? null
}

function StepIcon({ status }: { status: string }) {
  const col = COLUMN_OF[status] ?? 'queued'
  if (col === 'done') return <CheckCircle2 className="h-4 w-4 text-nexus-success" />
  if (col === 'problem') return <XCircle className="h-4 w-4 text-nexus-error" />
  if (col === 'active' || col === 'review') return <Loader2 className="h-4 w-4 animate-spin text-nexus-cyan" />
  return <CircleDashed className="h-4 w-4 text-nexus-dim" />
}

export function ExecutivePlans({ tasks, agents, projects, onNavigate, onOpenTask }: {
  tasks: any[]; agents: any[]; projects: any[]; onNavigate: (r: Route) => void; onOpenTask: (id: string) => void
}) {
  const { t, formatNumber } = useI18n()
  const e = t.nexus.executive
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const [jobs, setJobs] = useState<any[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    apiFetch<any>('/arena/manager/jobs?limit=50').then((d) => { setJobs(d?.jobs ?? []); setError(null) }).catch((err) => { setError(err?.message ?? 'error'); setJobs([]) })
  }, [])
  useEffect(() => { load() }, [load, tasks.length])

  const agentsById = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a])), [agents])
  const projectsById = useMemo(() => Object.fromEntries(projects.map((p) => [p.id, p])), [projects])
  const byPlan = useMemo(() => {
    const out: Record<string, any[]> = {}
    for (const task of tasks) {
      const id = planIdOf(task)
      if (id) (out[id] ??= []).push(task)
    }
    return out
  }, [tasks])
  const plans = (jobs ?? []).filter((j) => j.kind === 'plan')

  return (
    <SectionCard testId="executive-plans" title={<span className="flex items-center gap-2"><Crown className="h-4 w-4 text-nexus-cyan" />{e.plansTitle}</span>}
      actions={<Button size="sm" variant="ghost" onClick={load}>{t.nexus.common.refresh}</Button>}>
      {error && <p className="mb-3 text-sm text-nexus-error" role="alert">{fill(t.nexus.common.failed, { reason: error })}</p>}
      {jobs === null ? <p className="text-sm text-nexus-muted">{t.nexus.common.loading}</p>
        : plans.length === 0 ? <p className="text-sm text-nexus-muted" data-testid="plans-empty">{e.plansEmpty}</p> : (
          <ol className="space-y-4">
            {plans.map((job) => {
              const steps = byPlan[job.id] ?? []
              const done = steps.filter((s) => COLUMN_OF[s.status] === 'done').length
              const indexOf = Object.fromEntries(steps.map((s, i) => [s.id, i + 1]))
              const needsYou = job.status === 'draft' || job.status === 'needs_human'
              const active = ACTIVE_JOB.includes(job.status)
              return (
                <li key={job.id} className="rounded-xl border border-nexus-border/70 bg-nexus-bg/40 p-4" data-testid={`plan-${job.id}`}>
                  <header className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-nexus-text">
                        <Badge variant={job.status === 'completed' || job.status === 'done' ? 'success' : job.status === 'failed' ? 'danger' : needsYou ? 'warning' : 'info'}>{job.status}</Badge>
                        <span className="truncate">{projectsById[job.project_id]?.name ?? '—'}</span>
                      </p>
                      <p className="mt-1 text-[11px] text-nexus-dim">{ago(job.created_at)} · <span dir="ltr">{job.id.slice(0, 8)}</span></p>
                    </div>
                    <div className="flex items-center gap-2">
                      {needsYou && (
                        <Button size="sm" onClick={() => onNavigate({ section: 'executive', sub: 'chat' })} data-testid={`plan-approve-${job.id}`}>
                          <AlertTriangle className="h-3.5 w-3.5" />{e.needsApproval}
                        </Button>
                      )}
                      {active && (
                        <Button size="sm" variant="ghost" disabled={isBusy(`cancel:${job.id}`)} aria-busy={isBusy(`cancel:${job.id}`)} data-testid={`plan-cancel-${job.id}`}
                          onClick={() => run(`cancel:${job.id}`, () => apiFetch(`/arena/manager/jobs/${job.id}/cancel`, { method: 'POST' }), {
                            confirm: { message: t.arenaAgent.manager.cancelJobConfirm, confirmLabel: t.arenaAgent.manager.cancelJob, danger: true }, onDone: load,
                          })}>
                          {t.nexus.common.cancel}
                        </Button>
                      )}
                    </div>
                  </header>
                  {steps.length > 0 && (
                    <div className="mt-3">
                      <div className="mb-1 flex justify-between text-[11px] text-nexus-muted"><span>{e.progress}</span><span className="tabular-nums">{formatNumber(done)} / {formatNumber(steps.length)}</span></div>
                      <Progress value={(done / steps.length) * 100} />
                    </div>
                  )}
                  {job.error && <p className="mt-3 rounded-md border border-nexus-error/30 bg-nexus-error/5 p-2 text-xs text-nexus-error"><span className="font-medium">{e.errors}: </span>{job.error}</p>}
                  <div className="mt-3">
                    <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{e.proposedTasks}</h4>
                    {steps.length === 0 ? <p className="text-xs text-nexus-muted">{e.noTasks}</p> : (
                      <ol className="relative space-y-2 border-s border-nexus-border/70 ps-5">
                        {steps.map((s, i) => (
                          <li key={s.id} className="relative">
                            <span className="absolute -start-[1.95rem] top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-nexus-card ring-1 ring-nexus-border"><StepIcon status={s.status} /></span>
                            <button type="button" onClick={() => onOpenTask(s.id)}
                              className="w-full rounded-lg border border-nexus-border/60 bg-nexus-card px-3 py-2 text-start transition-colors hover:border-nexus-cyan/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-sm text-nexus-text"><span className="text-nexus-dim">{fill(e.step, { n: i + 1 }, formatNumber)} · </span>{s.title}</p>
                                <span className={cn('shrink-0 text-[11px]', COLUMN_OF[s.status] === 'problem' ? 'text-nexus-error' : 'text-nexus-muted')} dir="ltr">{s.status}</span>
                              </div>
                              <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-nexus-dim">
                                <span>{e.assigned}: {agentsById[s.assigned_agent_id]?.name ?? t.nexus.board.unassigned}</span>
                                {(s.dependencies?.length ?? 0) > 0 && (
                                  <span className="inline-flex items-center gap-1"><GitBranch className="h-3 w-3" />{e.dependsOn}: {s.dependencies.map((d: string) => indexOf[d] ? fill(e.step, { n: indexOf[d] }, formatNumber) : d.slice(0, 6)).join(', ')}</span>
                                )}
                              </div>
                              {s.error_message && <p className="mt-1 text-[11px] text-nexus-error">{s.error_message}</p>}
                            </button>
                          </li>
                        ))}
                      </ol>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        )}
    </SectionCard>
  )
}
