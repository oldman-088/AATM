/**
 * NEXUS Analytics & Monitoring (owner spec §10). Every figure is computed from collected backend data:
 * tasks (/tasks), agents' lifetime counters (/agents), provider usage logs and budgets (/providers/usage),
 * scaling status and device capacity. A metric with no data says "Data unavailable".
 */
import React, { useEffect, useMemo, useState } from 'react'
import { BarChart3, Coins, Cpu, FolderKanban, Gauge, Hash, Timer, Users } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { BarList, fill, PageHeader, Ring, SectionCard, StatCard, Unavailable, agentState } from '../../components/nexus'
import { Progress } from '../../components/ui'
import { DailySummary } from '../DashboardViews'

const DONE = ['completed', 'done', 'merged', 'approved']
const FAILED = ['failed', 'changes_requested']

function minutesBetween(a?: string | null, b?: string | null) {
  if (!a || !b) return null
  const norm = (v: string) => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : `${v}Z`).getTime()
  const d = (norm(b) - norm(a)) / 60000
  return Number.isFinite(d) && d >= 0 ? d : null
}

export function AnalyticsView({ agents, tasks, projects, usage, devices, resources }: {
  agents: any[]; tasks: any[]; projects: any[]; usage: any; devices: any[]; resources?: any
}) {
  const { t, formatNumber } = useI18n()
  const a = t.nexus.analytics
  const [scaling, setScaling] = useState<any>(null)
  useEffect(() => { apiFetch<any>('/scaling/status').then(setScaling).catch(() => setScaling(null)) }, [])

  const stats = useMemo(() => {
    const done = tasks.filter((x) => DONE.includes(x.status)).length
    const failed = tasks.filter((x) => FAILED.includes(x.status)).length
    const lifetimeDone = agents.reduce((s, x) => s + Number(x.tasks_completed ?? 0), 0)
    const lifetimeFailed = agents.reduce((s, x) => s + Number(x.tasks_failed ?? 0), 0)
    const completion = tasks.length ? (done / tasks.length) * 100 : null
    const failureBase = done + failed > 0 ? { f: failed, n: done + failed } : lifetimeDone + lifetimeFailed > 0 ? { f: lifetimeFailed, n: lifetimeDone + lifetimeFailed } : null
    const durations = tasks.map((x) => minutesBetween(x.started_at, x.completed_at)).filter((v): v is number => v !== null)
    const avg = durations.length ? durations.reduce((s, v) => s + v, 0) / durations.length : null
    const workers = agents.filter((x) => x.role !== 'manager')
    const busy = workers.filter((x) => agentState(x) === 'working').length
    return { completion, failure: failureBase ? (failureBase.f / failureBase.n) * 100 : null, avg, durations: durations.length, workers: workers.length, busy }
  }, [agents, tasks])

  const productivity = agents
    .map((x) => ({ id: x.id, label: x.name, value: Number(x.tasks_completed ?? 0), secondary: Number(x.tasks_failed ?? 0),
      display: `${formatNumber(Number(x.tasks_completed ?? 0))} / ${formatNumber(Number(x.tasks_failed ?? 0))}` }))
    .filter((r) => r.value + r.secondary > 0)
    .sort((x, y) => y.value - x.value)

  const logs: any[] = usage?.logs ?? []
  const perProvider = Object.entries(logs.reduce<Record<string, number>>((acc, l) => { acc[l.provider_id] = (acc[l.provider_id] ?? 0) + 1; return acc }, {}))
    .map(([id, n]) => ({ id, label: id, value: n, display: fill(a.requests, { n }, formatNumber) }))
    .sort((x, y) => y.value - x.value)
  const summary = usage?.summary
  const budget = usage?.budget_summary

  const projectRows = projects.map((p) => {
    const mine = tasks.filter((x) => x.project_id === p.id)
    const done = mine.filter((x) => DONE.includes(x.status)).length
    return { p, total: mine.length, done }
  })
  const local = devices.find((d) => d.device_type === 'local') ?? devices[0]

  return (
    <div className="space-y-6" data-testid="analytics-view">
      <PageHeader title={a.title} subtitle={a.subtitle} icon={<BarChart3 className="h-6 w-6" />} />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard testId="metric-avg-execution" label={a.avgExecution} icon={<Timer className="h-4 w-4" />} tone="violet"
          value={stats.avg === null ? <Unavailable /> : fill(t.nexus.common.minutes, { n: Math.round(stats.avg) }, formatNumber)}
          hint={stats.avg === null ? undefined : fill(a.avgExecutionHint, { n: stats.durations }, formatNumber)} />
        <StatCard testId="metric-utilization" label={a.utilization} icon={<Users className="h-4 w-4" />}
          value={stats.workers ? `${formatNumber(Math.round((stats.busy / stats.workers) * 100))}%` : <Unavailable />}
          hint={fill(a.utilizationHint, { a: stats.busy, b: stats.workers }, formatNumber)} />
        <StatCard testId="metric-tokens" label={a.tokens} icon={<Hash className="h-4 w-4" />} tone="cyan"
          value={summary ? formatNumber((summary.total_input_tokens ?? 0) + (summary.total_output_tokens ?? 0)) : <Unavailable />}
          hint={summary ? `${a.inputTokens} ${formatNumber(summary.total_input_tokens ?? 0)} · ${a.outputTokens} ${formatNumber(summary.total_output_tokens ?? 0)}` : undefined} />
        <StatCard testId="metric-cost" label={a.cost} icon={<Coins className="h-4 w-4" />} tone="warning"
          value={budget ? `$${Number(budget.global_daily_spend_usd ?? 0).toFixed(2)}` : <Unavailable />}
          hint={budget ? `${a.costToday} · ${fill(a.costLimit, { n: `$${Number(budget.daily_limit_usd ?? 0).toFixed(0)}` })}` : undefined} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <SectionCard title={a.completionRate} testId="metric-rates" className="lg:col-span-1">
          <div className="flex flex-wrap items-center justify-around gap-4">
            <Ring value={stats.completion} label={a.completionRate} tone="success" testId="ring-completion" />
            <Ring value={stats.failure} label={a.failureRate} tone="error" testId="ring-failure" />
          </div>
        </SectionCard>
        <SectionCard title={<span title={a.productivityHint}>{a.productivity}</span>} className="lg:col-span-2" testId="metric-productivity">
          <p className="mb-3 flex gap-4 text-[11px] text-nexus-dim">
            <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-nexus-cyan" />{a.completed}</span>
            <span className="inline-flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-nexus-error" />{a.failed}</span>
          </p>
          <BarList rows={productivity} testId="productivity-bars" />
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard title={<span title={a.providerUsageHint}>{a.providerUsage}</span>} testId="metric-provider-usage">
          <BarList rows={perProvider} testId="provider-bars" />
        </SectionCard>
        <SectionCard title={a.cost} testId="metric-cost-detail">
          {budget ? (
            <div className="space-y-4 text-sm">
              {[{ label: a.costToday, spend: budget.global_daily_spend_usd, limit: budget.daily_limit_usd },
                { label: a.costMonth, spend: budget.global_monthly_spend_usd, limit: budget.monthly_limit_usd }].map((row) => (
                <div key={row.label}>
                  <div className="mb-1 flex justify-between text-xs"><span className="text-nexus-muted">{row.label}</span>
                    <span className="tabular-nums text-nexus-text">${Number(row.spend ?? 0).toFixed(2)} / ${Number(row.limit ?? 0).toFixed(0)}</span></div>
                  <Progress value={row.limit ? (Number(row.spend ?? 0) / Number(row.limit)) * 100 : 0} />
                </div>
              ))}
              {summary?.has_unknown_pricing && <p className="text-xs text-nexus-warning">{a.unknownPricing}</p>}
            </div>
          ) : <Unavailable />}
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard title={<span className="flex items-center gap-2"><Cpu className="h-4 w-4 text-nexus-cyan" />{a.resources}</span>} testId="metric-resources">
          <dl className="space-y-2 text-sm">
            {resources ? ([
              { label: t.nexus.cc.healthCpu, value: resources.cpu_percent, text: fill(t.nexus.cc.cpuValue, { p: resources.cpu_percent, n: resources.cpu_count }, formatNumber) },
              { label: t.nexus.cc.healthMemory, value: resources.ram_percent, text: fill(t.nexus.cc.memoryValue, { a: resources.ram_used_gb, b: resources.ram_total_gb, p: resources.ram_percent }, formatNumber) },
              { label: a.disk, value: resources.disk_percent, text: fill(a.diskValue, { a: resources.disk_free_gb, b: resources.disk_total_gb, p: resources.disk_percent }, formatNumber) },
            ].map((row) => (
              <div key={row.label} data-testid="resource-row">
                <div className="mb-1 flex justify-between gap-2 text-xs"><dt className="text-nexus-muted">{row.label}</dt><dd className="tabular-nums text-nexus-text">{row.text}</dd></div>
                <Progress value={row.value} />
              </div>
            ))) : (<>
              <div className="flex justify-between gap-2"><dt className="text-nexus-muted">{t.nexus.cc.healthCpu}</dt><dd><Unavailable text={local?.cpu_cores ? `${t.nexus.common.dataUnavailable} · ${formatNumber(local.cpu_cores)} cores` : undefined} /></dd></div>
              <div className="flex justify-between gap-2"><dt className="text-nexus-muted">{t.nexus.cc.healthMemory}</dt><dd><Unavailable text={local?.ram_gb ? `${t.nexus.common.dataUnavailable} · ${formatNumber(local.ram_gb)} GB` : undefined} /></dd></div>
            </>)}
            <div className="flex justify-between gap-2"><dt className="text-nexus-muted"><Gauge className="me-1 inline h-3.5 w-3.5" />{t.nexus.admin.workerConfig}</dt>
              <dd className="text-nexus-text">{scaling ? fill(t.nexus.admin.workersRuntime, { a: scaling.default_workers, b: scaling.extra_workers, c: scaling.parallel_slots }, formatNumber) : <Unavailable />}</dd></div>
            <p className="pt-1 text-xs text-nexus-dim">{resources ? a.resourcesHint : t.nexus.cc.healthCpuHint}</p>
          </dl>
        </SectionCard>
        <SectionCard title={<span className="flex items-center gap-2"><FolderKanban className="h-4 w-4 text-nexus-violet" />{a.projectPerformance}</span>} testId="metric-projects">
          {projectRows.length === 0 ? <p className="text-sm text-nexus-muted">{a.noProjects}</p> : (
            <ul className="space-y-3">
              {projectRows.map(({ p, total, done }) => (
                <li key={p.id} className="text-sm">
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <span className="truncate text-nexus-text">{p.name} <span className="text-xs text-nexus-dim">({p.status})</span></span>
                    <span className="shrink-0 text-xs text-nexus-muted">{fill(a.projectDone, { a: done, b: total }, formatNumber)}</span>
                  </div>
                  <Progress value={total ? (done / total) * 100 : 0} />
                </li>
              ))}
            </ul>
          )}
        </SectionCard>
      </div>

      <DailySummary />
    </div>
  )
}
