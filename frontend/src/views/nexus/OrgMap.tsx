/**
 * NEXUS Organization Map (owner spec §8): Executive Director → departments → department managers → agents → tasks.
 * Built only from backend data: agent `reports_to`, `role_template` (department title from /employees/templates),
 * status, and the tasks assigned to each agent. Nodes expand and collapse; agents open their profile.
 */
import React, { useMemo, useState } from 'react'
import { ChevronDown, Crown, Network, Users } from 'lucide-react'
import { useI18n } from '../../i18n/I18nContext'
import { Button, cn } from '../../components/ui'
import { agentState, fill, PageHeader, STATE_TONE, StatusDot } from '../../components/nexus'
import { AgentAvatar, departmentOf } from './Workforce'

const OPEN_TASK = (s: string) => !['completed', 'done', 'merged', 'cancelled'].includes(s)

function Toggle({ open, onClick, label }: { open: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick} aria-expanded={open} aria-label={label}
      className="rounded-md p-1 text-nexus-muted hover:bg-nexus-raised hover:text-nexus-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
      <ChevronDown className={cn('h-4 w-4 transition-transform', !open && '-rotate-90 rtl:rotate-90')} />
    </button>
  )
}

function AgentNode({ agent, tasks, reports, depth, expanded, toggle, onOpenAgent, onOpenTask }: {
  agent: any; tasks: any[]; reports: any[]; depth: number; expanded: Set<string>; toggle: (id: string) => void
  onOpenAgent: (id: string) => void; onOpenTask: (id: string) => void
}) {
  const { t, formatNumber } = useI18n()
  const o = t.nexus.org
  const open = expanded.has(agent.id)
  const mine = tasks.filter((x) => x.assigned_agent_id === agent.id && OPEN_TASK(x.status))
  const state = agentState(agent)
  return (
    <li className="relative" data-testid={`org-agent-${agent.id}`}>
      <div className="flex items-center gap-2 rounded-lg border border-nexus-border/70 bg-nexus-bg/50 p-2">
        {(mine.length > 0 || reports.length > 0) ? <Toggle open={open} onClick={() => toggle(agent.id)} label={agent.name} /> : <span className="w-6" />}
        <AgentAvatar agent={agent} size="sm" />
        <button type="button" onClick={() => onOpenAgent(agent.id)} className="min-w-0 flex-1 text-start focus-visible:outline-none focus-visible:underline" title={o.openProfile}>
          <span className="block truncate text-sm font-medium text-nexus-text">{agent.name}</span>
          <span className="flex items-center gap-2 text-[11px] text-nexus-dim">
            <StatusDot tone={STATE_TONE[state]} pulse={state === 'working'} label={t.nexus.states[state]} />
          </span>
        </button>
        <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-[11px] tabular-nums', mine.length ? 'bg-nexus-cyan/10 text-nexus-cyan' : 'bg-nexus-raised text-nexus-dim')}
          data-testid={`org-workload-${agent.id}`}>
          {mine.length ? fill(o.workload, { n: mine.length }, formatNumber) : o.noTasks}
        </span>
      </div>
      {open && (mine.length > 0 || reports.length > 0) && (
        <ul className="ms-6 mt-2 space-y-2 border-s border-nexus-border/70 ps-4">
          {mine.map((task) => (
            <li key={task.id}>
              <button type="button" onClick={() => onOpenTask(task.id)}
                className="flex w-full items-center justify-between gap-2 rounded-md bg-nexus-raised/50 px-3 py-1.5 text-start text-xs hover:bg-nexus-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
                <span className="truncate text-nexus-text">{task.title}</span><span className="shrink-0 text-nexus-dim" dir="ltr">{task.status}</span>
              </button>
            </li>
          ))}
          {reports.map((r) => (
            <AgentNode key={r.agent.id} agent={r.agent} tasks={tasks} reports={r.reports} depth={depth + 1} expanded={expanded} toggle={toggle}
              onOpenAgent={onOpenAgent} onOpenTask={onOpenTask} />
          ))}
        </ul>
      )}
    </li>
  )
}

interface Tree { agent: any; reports: Tree[] }

export function OrgMap({ agents, tasks, templates, onOpenAgent, onOpenTask }: {
  agents: any[]; tasks: any[]; templates: any[]; onOpenAgent: (id: string) => void; onOpenTask: (id: string) => void
}) {
  const { t, formatNumber } = useI18n()
  const o = t.nexus.org
  const director = agents.find((a) => a.role === 'manager') ?? null
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [openDepts, setOpenDepts] = useState<Set<string> | 'all'>('all')

  // Agents reporting to someone other than the director become department managers' reports.
  const { departments, allIds } = useMemo(() => {
    const byId = Object.fromEntries(agents.map((a) => [a.id, a]))
    const children: Record<string, any[]> = {}
    for (const a of agents) {
      if (a === director) continue
      const parent = a.reports_to && byId[a.reports_to] && a.reports_to !== director?.id ? a.reports_to : '__top__'
      ;(children[parent] ??= []).push(a)
    }
    const build = (a: any): Tree => ({ agent: a, reports: (children[a.id] ?? []).map(build) })
    const groups: Record<string, Tree[]> = {}
    for (const a of children.__top__ ?? []) {
      const dept = departmentOf(a, templates) || o.noDepartment
      ;(groups[dept] ??= []).push(build(a))
    }
    return { departments: Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)), allIds: agents.map((a) => a.id) }
  }, [agents, director, templates, o.noDepartment])

  const toggle = (id: string) => setExpanded((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const deptOpen = (name: string) => openDepts === 'all' || openDepts.has(name)
  const toggleDept = (name: string) => setOpenDepts((s) => {
    const n = new Set(s === 'all' ? departments.map(([d]) => d) : s)
    n.has(name) ? n.delete(name) : n.add(name)
    return n
  })
  const countTree = (trees: Tree[]): number => trees.reduce((sum, tr) => sum + 1 + countTree(tr.reports), 0)

  if (!agents.length) return <p className="rounded-xl border border-nexus-border bg-nexus-card p-6 text-center text-sm text-nexus-muted">{o.empty}</p>

  return (
    <div className="space-y-5" data-testid="org-map">
      <PageHeader title={o.title} subtitle={o.subtitle} icon={<Network className="h-6 w-6" />} actions={<>
        <Button size="sm" variant="secondary" onClick={() => { setOpenDepts('all'); setExpanded(new Set(allIds)) }} data-testid="org-expand-all">{o.expandAll}</Button>
        <Button size="sm" variant="ghost" onClick={() => { setOpenDepts(new Set()); setExpanded(new Set()) }} data-testid="org-collapse-all">{o.collapseAll}</Button>
      </>} />

      {director && (
        <div className="flex justify-center">
          <button type="button" onClick={() => onOpenAgent(director.id)} data-testid="org-director"
            className="flex items-center gap-3 rounded-2xl border border-nexus-cyan/40 bg-nexus-card px-5 py-3 shadow-glow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
            <AgentAvatar agent={director} />
            <span className="text-start">
              <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-nexus-cyan"><Crown className="h-3.5 w-3.5" />{o.director}</span>
              <span className="block text-sm font-semibold text-nexus-text">{director.name}</span>
              <StatusDot tone={STATE_TONE[agentState(director)]} label={t.nexus.states[agentState(director)]} />
            </span>
          </button>
        </div>
      )}
      <div className="mx-auto h-6 w-px bg-nexus-border" aria-hidden />

      <section aria-label={o.departments} className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {departments.map(([name, trees]) => {
          const open = deptOpen(name)
          return (
            <div key={name} className="rounded-xl border border-nexus-border/80 bg-nexus-card shadow-card" data-testid="org-department">
              <header className="flex items-center gap-2 border-b border-nexus-border/60 px-3 py-2.5">
                <Toggle open={open} onClick={() => toggleDept(name)} label={name} />
                <Users className="h-4 w-4 text-nexus-violet" aria-hidden />
                <h3 className="min-w-0 flex-1 truncate text-sm font-semibold text-nexus-text">{name}</h3>
                <span className="text-xs text-nexus-dim">{fill(o.agentsCount, { n: countTree(trees) }, formatNumber)}</span>
              </header>
              {open && (
                <ul className="space-y-2 p-3">
                  {trees.map((tr) => (
                    <AgentNode key={tr.agent.id} agent={tr.agent} tasks={tasks} reports={tr.reports} depth={0} expanded={expanded} toggle={toggle}
                      onOpenAgent={onOpenAgent} onOpenTask={onOpenTask} />
                  ))}
                </ul>
              )}
            </div>
          )
        })}
      </section>
    </div>
  )
}
