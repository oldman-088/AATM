import React, { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { NexusProvider, SubTabs } from '../../components/nexus'
import { TaskBoard } from '../nexus/TaskBoard'
import { WorkforceView } from '../nexus/Workforce'
import { OrgMap } from '../nexus/OrgMap'
import { AnalyticsView } from '../nexus/Analytics'
import { AdminSettings, SystemLogs, UsersPermissions } from '../nexus/Admin'
import { ExecutivePlans } from '../nexus/ExecutivePlans'
import { legacyRoute, parseHash, routeHash } from '../../shell/sections'

type Call = { path: string; method: string; body: any }

/** Answers fetch by path; records every call. */
function mockApi(routes: Record<string, any> = {}) {
  const calls: Call[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: any, init: any = {}) => {
    const url = new URL(String(input), 'http://test')
    const path = url.pathname.replace(/^\/api\/v1/, '')
    const method = (init.method ?? 'GET').toUpperCase()
    calls.push({ path, method, body: init.body ? JSON.parse(init.body) : undefined })
    const hit = routes[`${method} ${path}`] ?? routes[path]
    const data = typeof hit === 'function' ? hit() : hit ?? {}
    return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  return calls
}

function wrap(ui: React.ReactElement, language = 'en') {
  localStorage.setItem('aatm_language', language)
  return render(<I18nProvider><NexusProvider>{ui}</NexusProvider></I18nProvider>)
}

const project = { id: 'p1', name: 'Calc', status: 'active' }
const agents = [
  { id: 'manager-0', name: 'Director', role: 'manager', status: 'idle' },
  { id: 'worker-1', name: 'Builder', role: 'worker', status: 'working', reports_to: 'manager-0', specialty: 'Backend', tasks_completed: 3, tasks_failed: 1 },
  { id: 'worker-2', name: 'Tester', role: 'worker', status: 'idle', reports_to: 'manager-0', specialty: 'QA', tasks_completed: 1 },
]
const tasks = [
  { id: 't1', project_id: 'p1', title: 'Write parser', status: 'queued', task_type: 'implementation', priority: 'high', checkpoints: [{ manager_job_id: 'job-1' }] },
  { id: 't2', project_id: 'p1', title: 'Add tests', status: 'in_progress', task_type: 'implementation', assigned_agent_id: 'worker-1', dependencies: ['t1'],
    checkpoints: [{ manager_job_id: 'job-1' }], started_at: '2026-11-25T10:00:00Z' },
  { id: 't3', project_id: 'p1', title: 'Ship it', status: 'completed', task_type: 'implementation', assigned_agent_id: 'worker-2',
    started_at: '2026-11-25T10:00:00Z', completed_at: '2026-11-25T10:30:00Z' },
  { id: 't4', project_id: 'p1', title: 'Broken build', status: 'failed', task_type: 'implementation', error_message: 'pytest red' },
]

beforeEach(() => localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('NEXUS routing', () => {
  it('round-trips hash routes and maps every legacy tab to a section', () => {
    expect(parseHash(routeHash({ section: 'projects', sub: 'pullRequests' }))).toEqual({ section: 'projects', sub: 'pullRequests' })
    expect(parseHash('#/nonsense/x').section).toBe('command')
    expect(legacyRoute('chat')).toEqual({ section: 'executive', sub: 'chat' })
    expect(legacyRoute('git').section).toBe('projects')
  })
})

describe('SubTabs keyboard', () => {
  function Harness() {
    const [active, setActive] = useState('a')
    return <SubTabs label="tabs" active={active} onChange={setActive} tabs={[{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }]} />
  }
  it('moves forward with ArrowRight in LTR and with ArrowLeft in RTL', () => {
    const { unmount } = wrap(<Harness />)
    fireEvent.keyDown(screen.getByTestId('subtab-a'), { key: 'ArrowRight' })
    expect(screen.getByTestId('subtab-b').getAttribute('aria-selected')).toBe('true')
    unmount()
    wrap(<Harness />, 'ar')
    fireEvent.keyDown(screen.getByTestId('subtab-a'), { key: 'ArrowLeft' })
    expect(screen.getByTestId('subtab-b').getAttribute('aria-selected')).toBe('true')
  })
})

describe('Task board', () => {
  function Board() {
    const [open, setOpen] = useState<string | null>(null)
    return <TaskBoard tasks={tasks} agents={agents} projects={[project]} projectId="p1" onReload={() => {}} openTaskId={open} onOpenTask={setOpen} />
  }

  it('puts each task in the column of its status', () => {
    mockApi()
    wrap(<Board />)
    expect(within(screen.getByTestId('kanban-queued')).getByText('Write parser')).toBeInTheDocument()
    expect(within(screen.getByTestId('kanban-active')).getByText('Add tests')).toBeInTheDocument()
    expect(within(screen.getByTestId('kanban-done')).getByText('Ship it')).toBeInTheDocument()
    expect(within(screen.getByTestId('kanban-problem')).getByText('Broken build')).toBeInTheDocument()
  })

  it('asks before cancelling a task, then PATCHes its status', async () => {
    const calls = mockApi()
    wrap(<Board />)
    fireEvent.click(screen.getByTestId('task-card-t1'))
    fireEvent.change(await screen.findByTestId('task-status-select'), { target: { value: 'cancelled' } })
    fireEvent.click(screen.getByTestId('task-status-apply'))
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
    fireEvent.click(await screen.findByTestId('confirm-ok'))
    await waitFor(() => expect(calls).toContainEqual({ path: '/tasks/t1', method: 'PATCH', body: { status: 'cancelled' } }))
  })

  it('filters by search text', () => {
    mockApi()
    wrap(<Board />)
    fireEvent.change(screen.getByTestId('task-search'), { target: { value: 'parser' } })
    expect(screen.queryByText('Ship it')).toBeNull()
    expect(screen.getByText('Write parser')).toBeInTheDocument()
  })
})

describe('AI Workforce', () => {
  function Workforce() {
    const [open, setOpen] = useState<string | null>(null)
    return <WorkforceView agents={agents} tasks={tasks} projects={[project]} templates={[]} onReload={() => {}} openAgentId={open} onOpenAgent={setOpen} />
  }

  it('assigns a queued task to a worker through the Arena assign endpoint', async () => {
    const calls = mockApi({ '/traces': [], '/dashboard/employees': { employees: [] } })
    wrap(<Workforce />)
    fireEvent.click(screen.getByTestId('agent-assign-worker-2'))
    fireEvent.change(await screen.findByTestId('assign-select'), { target: { value: 't1' } })
    fireEvent.click(screen.getByTestId('assign-submit'))
    await waitFor(() => expect(calls).toContainEqual({ path: '/arena/agents/worker-2/assign-task', method: 'POST', body: { task_id: 't1' } }))
  })

  it('confirms before pausing an agent and disables it', async () => {
    const calls = mockApi()
    wrap(<Workforce />)
    fireEvent.click(screen.getByTestId('agent-pause-worker-1'))
    fireEvent.click(await screen.findByTestId('confirm-ok'))
    await waitFor(() => expect(calls).toContainEqual({ path: '/agents/worker-1', method: 'PATCH', body: { is_enabled: false } }))
  })
})

describe('Organization map', () => {
  it('shows the director, a department per specialty and each workload', () => {
    wrap(<OrgMap agents={agents} tasks={tasks} templates={[]} onOpenAgent={() => {}} onOpenTask={() => {}} />)
    expect(screen.getByTestId('org-director').textContent).toContain('Director')
    expect(screen.getAllByTestId('org-department')).toHaveLength(2)
    expect(screen.getByTestId('org-workload-worker-1').textContent).toContain('1')
  })
})

describe('Analytics', () => {
  it('computes rates and execution time from real tasks and marks missing data unavailable', () => {
    mockApi()
    const { container } = wrap(<AnalyticsView agents={agents} tasks={tasks} projects={[project]} usage={null} devices={[]} />)
    expect(screen.getByTestId('metric-avg-execution').textContent).toContain('30')
    expect(within(screen.getByTestId('metric-tokens')).getByText(/unavailable/i)).toBeInTheDocument()
    expect(container.querySelectorAll('[data-testid="data-unavailable"]').length).toBeGreaterThan(0)
  })
})

describe('System administration', () => {
  it('rejects invalid JSON and saves a valid setting through PUT /settings/{key}', async () => {
    const calls = mockApi({ '/settings': { concurrency: { value: { max_workers: 4 }, description: 'Worker slots' } }, '/scaling/status': {} })
    wrap(<AdminSettings user={{ role: 'admin' }} health={null} />)
    fireEvent.click(await screen.findByTestId('setting-edit-concurrency'))
    const editor = screen.getByTestId('setting-editor-concurrency')
    fireEvent.change(editor, { target: { value: '{oops' } })
    fireEvent.click(screen.getByTestId('setting-save-concurrency'))
    expect(screen.getByRole('alert')).toBeInTheDocument()
    fireEvent.change(editor, { target: { value: '{"max_workers": 6}' } })
    fireEvent.click(screen.getByTestId('setting-save-concurrency'))
    await waitFor(() => expect(calls).toContainEqual({ path: '/settings/concurrency', method: 'PUT', body: { value: { max_workers: 6 } } }))
  })

  it('hides editing from a viewer', async () => {
    mockApi({ '/settings': { concurrency: { value: 1 } } })
    wrap(<AdminSettings user={{ role: 'viewer' }} health={null} />)
    await screen.findByTestId('setting-concurrency')
    expect(screen.queryByTestId('setting-edit-concurrency')).toBeNull()
  })

  it('filters system logs by severity', () => {
    wrap(<SystemLogs auditLogs={[{ id: '1', action: 'task.create', severity: 'info' }, { id: '2', action: 'setting.update', severity: 'warning' }]} />)
    fireEvent.change(screen.getByTestId('logs-severity'), { target: { value: 'warning' } })
    expect(screen.queryByText('task.create')).toBeNull()
    expect(screen.getByText('setting.update')).toBeInTheDocument()
  })

  it('says plainly that user management needs a backend API', () => {
    wrap(<UsersPermissions user={{ email: 'owner@example.test', role: 'admin' }} />)
    expect(screen.getByTestId('users-api-missing')).toBeInTheDocument()
  })
})

describe('Executive plans', () => {
  it('draws a plan job as numbered steps with dependencies and approval need', async () => {
    mockApi({ '/arena/manager/jobs': { jobs: [{ id: 'job-1', kind: 'plan', status: 'draft', project_id: 'p1', created_at: '2026-11-25T10:00:00Z' }] } })
    const navigate = vi.fn()
    wrap(<ExecutivePlans tasks={tasks} agents={agents} projects={[project]} onNavigate={navigate} onOpenTask={() => {}} />)
    const plan = await screen.findByTestId('plan-job-1')
    expect(within(plan).getByText('Write parser')).toBeInTheDocument()
    expect(within(plan).getByText('Add tests')).toBeInTheDocument()
    expect(within(plan).queryByText('Ship it')).toBeNull()
    expect(plan.textContent).toMatch(/Step 1/)
    fireEvent.click(within(plan).getByTestId('plan-approve-job-1'))
    expect(navigate).toHaveBeenCalledWith({ section: 'executive', sub: 'chat' })
  })
})
