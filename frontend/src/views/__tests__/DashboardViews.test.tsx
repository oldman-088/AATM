import React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider, resolveInitialLanguage } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { EmployeeCards, HomeOverview, LIVE_EVENT, LiveAgentLog, ProjectTimeline, PullRequestsView, RecoveryPanel } from '../DashboardViews'

const wrap = (ui: React.ReactElement, lang: 'en' | 'ar' | 'fa' = 'en') => {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider>{ui}</I18nProvider>)
}
const STEP_IDS = ['request', 'analyze_split', 'distribute', 'execute', 'collect', 'pull_request', 'review_merge', 'completed']
const timeline = (states: string[]) => ({
  project_id: 'p1', project_name: 'Shop', status: 'active', current_step: 'pull_request',
  steps: STEP_IDS.map((id, i) => ({ id, state: states[i], done: states[i] === 'done' ? 4 : 1, total: 4 })),
})

describe('Phase 7 dashboard (infographic)', () => {
  beforeEach(() => localStorage.clear())

  it('Arabic RTL is the default language for first-time visitors', () => {
    expect(resolveInitialLanguage(null, undefined)).toBe('ar')
    expect(resolveInitialLanguage(null, 'xx')).toBe('ar')
    expect(resolveInitialLanguage('fa', 'en')).toBe('fa')
    expect(resolveInitialLanguage(null, 'en')).toBe('en')
  })

  it('renders the 8 infographic steps lit from the API state (Arabic, RTL)', async () => {
    installFetchMock({ '/api/v1/projects/p1/timeline': timeline(['done', 'done', 'done', 'done', 'done', 'active', 'pending', 'pending']) })
    wrap(<ProjectTimeline projectId="p1" />, 'ar')
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(8))
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByTestId('step-pull_request').getAttribute('data-state')).toBe('active')
    expect(screen.getByTestId('step-completed').getAttribute('data-state')).toBe('pending')
    expect(screen.getByText('6. إنشاء طلب دمج على GitHub')).toBeTruthy()
  })

  it('shows the architecture strip and live counters', async () => {
    installFetchMock({ '/api/v1/dashboard/home': {
      architecture: [{ id: 'dashboard', count: null }, { id: 'orchestrator', count: 1 }, { id: 'profiles', count: 5 }, { id: 'results', count: 3 }],
      counters: { active_employees: 3, total_employees: 4, tasks_by_status: { pr_open: 2 }, open_prs: 2, cost_today_usd: 1.5, daily_budget_usd: 20, needs_human: 1 },
    } })
    wrap(<HomeOverview />)
    expect((await screen.findByTestId('counter-active')).textContent).toBe('3 / 4')
    expect(screen.getByTestId('counter-prs').textContent).toBe('2')
    for (const label of ['Dashboard', 'Orchestrator', 'Profiles', 'Results']) expect(screen.getByText(label, { exact: false })).toBeTruthy()
    expect(screen.getByText('$1.50 / $20.00')).toBeTruthy()
  })

  it('employee cards are colored and their pause / emergency stop / replace buttons call the API', async () => {
    const { calls } = installFetchMock({ '/api/v1/dashboard/employees': { employees: [{
      id: 'worker-1', name: 'Backend Engineer', role: 'worker', status: 'working', is_enabled: true, is_emergency_stopped: false,
      color: '#6366f1', model: 'claude-x', provider: 'Claude', profile_status: 'running', connector_status: 'ready',
      current_task: { id: 't1', title: 'Add API', status: 'in_progress', progress: 40 }, last_action: { summary: 'Iteration 2: write — ok' }, cost_usd: 0.12,
    }] } })
    wrap(<EmployeeCards />)
    const card = await screen.findByTestId('employee-worker-1')
    expect(card.getAttribute('style')).toContain('border-inline-start-color')
    expect(card.textContent).toContain('Add API (40%)')
    expect(card.textContent).toContain('Iteration 2: write')
    fireEvent.click(screen.getByRole('button', { name: /Pause/ }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/agents/worker-1' && c.method === 'PATCH')?.body).toEqual({ is_enabled: false }))
    fireEvent.click(screen.getByRole('button', { name: /Emergency stop/ }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/emergency-stop')?.body).toMatchObject({ scope: 'employee', target_id: 'worker-1' }))
    fireEvent.click(screen.getByRole('button', { name: /Replace/ }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/employees/worker-1/replace' && c.method === 'POST')).toBeDefined())
  })

  it('live agent log loads the backlog and appends streamed agent.iteration events', async () => {
    installFetchMock({ '/api/v1/dashboard/agent-log': { entries: [{ agent_id: 'worker-2', summary: 'Iteration 1: read — README' }] } })
    wrap(<LiveAgentLog />)
    expect(await screen.findByText(/Iteration 1: read/)).toBeTruthy()
    act(() => {
      window.dispatchEvent(new CustomEvent(LIVE_EVENT, { detail: { type: 'agent.iteration', timestamp: 'now', payload: { agent_id: 'worker-3', iteration: 2, action: 'run', result: 'pytest passed' } } }))
    })
    expect(await screen.findByText(/Iteration 2: run — pytest passed/)).toBeTruthy()
    expect(screen.getByRole('log').children).toHaveLength(2)
  })

  it('PR view shows the diff, requests merge approval, and merges only once approved', async () => {
    let approvals: any[] = []
    const { calls } = installFetchMock({
      '/api/v1/reviews/pull-requests': () => [{ task_id: 't1', pr_number: 7, title: 'Add API', status: 'pr_open', branch_name: 'aatm/t1', pr_url: 'https://github.com/acme/demo/pull/7',
        reviews: [{ task_id: 'r1', task_type: 'review', round: 1, status: 'completed', summary: 'COMMENT: looks fine' }], merge_approvals: approvals }],
      '/api/v1/reviews/tasks/t1/diff': { available: true, diff: '+added line\n-removed line' },
      '/api/v1/reviews/tasks/t1/request-merge': () => { approvals = [{ id: 'ap1', status: 'pending' }]; return { approval_id: 'ap1', status: 'pending' } },
      '/api/v1/reviews/tasks/t1/merge': { ok: true },
    })
    wrap(<PullRequestsView />)
    fireEvent.click(await screen.findByRole('button', { name: /View diff/ }))
    expect((await screen.findByTestId('pr-diff')).textContent).toContain('+added line')
    expect(screen.queryByRole('button', { name: /Merge \(approved\)/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Request merge approval/ }))
    expect(await screen.findByText(/Waiting for a human to approve/)).toBeTruthy()
    approvals = [{ id: 'ap1', status: 'approved' }]
    act(() => { window.dispatchEvent(new CustomEvent(LIVE_EVENT, { detail: { type: 'approval.decided' } })) })
    fireEvent.click(await screen.findByRole('button', { name: /Merge \(approved\)/ }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/reviews/tasks/t1/merge')?.body).toEqual({ approval_id: 'ap1' }))
  })

  it('errors & recovery panel lists failures and resumes needs_human sessions', async () => {
    const { calls } = installFetchMock({ '/api/v1/dashboard/recovery': {
      failed_tasks: [{ id: 'f1', title: 'Broken build', status: 'failed', retry_count: 3, max_retries: 3, error: 'tests failed' }],
      retried_tasks: [], blocked_tasks: [], replacements: [{ id: 'a1', resource_id: 'worker-2', actor: 'system' }],
      needs_human: [{ profile_id: 'profile-qa-e2e', agent_id: 'worker-4', reason: 'human check element on page' }],
    } })
    wrap(<RecoveryPanel />)
    expect(await screen.findByText(/Broken build/)).toBeTruthy()
    expect(screen.getByText(/worker-2 — system/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/arena/profiles/profile-qa-e2e/resume')).toBeDefined())
  })
})
