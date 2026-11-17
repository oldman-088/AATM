import React from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { ArenaAgentDashboard, ArenaConnectorPanel } from '../ArenaViews'
import { LIVE_EVENT } from '../DashboardViews'

function renderWith(lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider><ArenaConnectorPanel /></I18nProvider>)
}

const card = (overrides: Record<string, any> = {}) => ({
  agent_id: 'worker-1', agent_name: 'Backend Engineer', agent_role: 'worker', color: '#6366f1',
  profile_id: 'profile-backend-docs', connector_status: 'offline', health: 'yellow', token_issued: false,
  needs_human_reason: null, ...overrides,
})

describe('ArenaConnectorPanel (Phase 6)', () => {
  beforeEach(() => localStorage.clear())

  it('shows that the connector is off and disables script generation while it is disabled', async () => {
    installFetchMock({ '/api/v1/arena/profiles': { gate: { mode: 'disabled', enabled: false, real_enabled: false }, profiles: [card()] } })
    renderWith()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('ARENA_CONNECTOR_ENABLED=true')
    expect((screen.getByRole('button', { name: /Generate userscript/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('generates a reviewable userscript in mock mode and resumes a needs_human profile', async () => {
    const { calls } = installFetchMock({
      '/api/v1/arena/profiles/profile-backend-docs/userscript': { script: '// ==UserScript==\n// @grant        none' },
      '/api/v1/arena/profiles/profile-backend-docs/resume': { requeued_tasks: ['t1'] },
      '/api/v1/arena/profiles': { gate: { mode: 'mock', enabled: true, real_enabled: false },
        profiles: [card({ connector_status: 'needs_human', health: 'red', needs_human_reason: 'human check element on page' })] },
    })
    renderWith()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('mock Arena page')
    fireEvent.click(screen.getByRole('button', { name: /Generate userscript/ }))
    await waitFor(() => expect(screen.getByTestId('arena-script').textContent).toContain('Tampermonkey'))
    expect((screen.getByLabelText(/Review this script/) as HTMLTextAreaElement).value).toContain('@grant        none')
    fireEvent.click(screen.getByRole('button', { name: /Resume/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/resume') && c.method === 'POST')).toBeDefined())
    expect(screen.getByText('human check element on page', { exact: false })).toBeTruthy()
  })

  it('renders Arabic RTL labels', async () => {
    installFetchMock({ '/api/v1/arena/profiles': { gate: { mode: 'disabled', enabled: false, real_enabled: false }, profiles: [] } })
    renderWith('ar')
    expect(await screen.findByText('موصّل Arena عبر المتصفح')).toBeTruthy()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('موصل Arena متوقف')
  })
})

// ----------------------------------------------------------------------------- R5 Arena Agent dashboard

const employee = (overrides: Record<string, any> = {}) => ({
  agent_id: 'worker-1', agent_name: 'Backend Engineer', agent_status: 'working', profile_id: 'profile-backend-docs',
  profile_name: 'backend-docs', arena_mode: 'agent', token_issued: true, connected: true, connector_status: 'ready',
  page_state: 'generating', needs_human_reason: null, keep_working_clicks: 3, last_error: null, human_events: [],
  task: { id: 'task-1', title: 'Add answer()', status: 'in_progress' },
  session: { id: 'sess-1', phase: 'working', page_state: 'generating', keep_working_count: 3, branch_name: 'arena/a1b2c3',
    pr_number: 101, pr_url: 'https://github.com/acme/demo/pull/101', last_activity_at: '2026-11-16T10:00:00', error_message: null },
  ...overrides,
})

const overview = (employees: any[], extra: Record<string, any> = {}) => ({
  gate: { mode: 'mock', enabled: true, real_enabled: false },
  task_distribution: { in_progress: 1, done: 2 },
  counts: { profiles: employees.length, agent_mode_profiles: employees.length, connected_profiles: employees.length, open_sessions: 1,
    running_tasks: 1, completed_tasks: 2, failed_tasks: 0, needs_human: 0 },
  employees,
  ...extra,
})

function renderDashboard(lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider><ArenaAgentDashboard /></I18nProvider>)
}

describe('ArenaAgentDashboard (R5)', () => {
  beforeEach(() => localStorage.clear())

  it('shows connector status, counters, distribution and every employee row without a profile-count limit', async () => {
    const many = Array.from({ length: 7 }, (_, i) => employee({ agent_id: `worker-${i + 1}`, agent_name: `Employee ${i + 1}`, profile_id: `p-${i + 1}` }))
    installFetchMock({ '/api/v1/arena/agent/overview': overview(many), '/api/v1/tasks': [] })
    renderDashboard()
    expect((await screen.findByTestId('arena-stat-connector')).textContent).toContain('Mock page')
    expect(screen.getByTestId('arena-stat-profiles').textContent).toContain('7/7')
    expect(screen.getByTestId('arena-stat-completed').textContent).toContain('2')
    expect(screen.getByTestId('arena-distribution').textContent).toContain('done: 2')
    for (let i = 1; i <= 7; i += 1) expect(screen.getByTestId(`arena-employee-worker-${i}`)).toBeTruthy()
    const row = screen.getByTestId('arena-employee-worker-1')
    expect(row.textContent).toContain('arena/a1b2c3')
    expect(row.textContent).toContain('Add answer() (in_progress)')
    expect(row.textContent).toContain('Working')
    expect(screen.getByTestId('arena-kw-worker-1').textContent).toBe('3')
    expect((row.querySelector('a[href="https://github.com/acme/demo/pull/101"]'))).toBeTruthy()
  })

  it('runs every control through the API: commands, mode, userscript, submit task and live diff', async () => {
    const { calls } = installFetchMock({
      '/api/v1/arena/agent/overview': overview([employee()]),
      '/api/v1/tasks': [{ id: 'task-9', title: 'Add greet()', status: 'queued', task_type: 'implementation' }],
      '/api/v1/arena/profiles/profile-backend-docs/command': (_path: string, init: any) => {
        const body = JSON.parse(init.body)
        if (body.command === 'read_last_reply') return { result: { text: '## AATM REPORT\nAll done' } }
        if (body.command === 'selector_test') return { result: { ok: false, composer: { found: false } } }
        return { result: { ok: true } }
      },
      '/api/v1/arena/profiles/profile-backend-docs/userscript': { script: '// ==UserScript==\n// @grant        none' },
      '/api/v1/arena/agents/worker-1/assign-task': { task_id: 'task-9' },
      '/api/v1/arena/agents/worker-1/mode': { mode: 'chat' },
      '/api/v1/arena/sessions/sess-1/live-diff': { ok: true, branch: 'arena/a1b2c3', additions: 5, deletions: 1,
        compare_url: 'https://github.com/acme/demo/compare/main...arena/a1b2c3',
        files: [{ filename: 'answer.py', additions: 5, deletions: 1, status: 'modified' }],
        commits: [{ sha: 'abcdef123456', message: 'Add answer()' }] },
    })
    renderDashboard()
    await screen.findByTestId('arena-employee-worker-1')
    const sent = (cmd: string) => calls.find((c) => c.path.endsWith('/command') && c.body?.command === cmd)
    for (const [name, cmd] of [[/Start new session/, 'new_session'], [/Continue working/, 'keep_working']] as const) {
      fireEvent.click(screen.getByRole('button', { name }))
      await waitFor(() => expect(sent(cmd)).toBeDefined())
    }
    fireEvent.click(screen.getByRole('button', { name: /Read last reply/ }))
    await waitFor(() => expect(screen.getByTestId('arena-output-worker-1').textContent).toContain('All done'))
    fireEvent.click(screen.getByRole('button', { name: /Selector test/ }))
    await waitFor(() => expect(screen.getByTestId('arena-output-worker-1').textContent).toContain('ask the owner to inspect'))
    fireEvent.click(screen.getByRole('button', { name: /Rotate userscript/ }))
    await waitFor(() => expect((screen.getByLabelText(/Review this script/) as HTMLTextAreaElement).value).toContain('@grant        none'))
    fireEvent.change(await screen.findByLabelText(/Choose a queued task/), { target: { value: 'task-9' } })
    fireEvent.click(screen.getByRole('button', { name: /Submit task/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/assign-task'))?.body).toEqual({ task_id: 'task-9' }))
    fireEvent.click(screen.getByRole('button', { name: /Switch to chat/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/agents/worker-1/mode'))?.body).toEqual({ mode: 'chat' }))
    fireEvent.click(screen.getByRole('button', { name: /View live diff/ }))
    const panel = await screen.findByTestId('arena-live-diff-sess-1')
    await waitFor(() => expect(panel.textContent).toContain('answer.py'))
    expect(panel.textContent).toContain('abcdef1')
    expect(panel.querySelector('a[href="https://github.com/acme/demo/compare/main...arena/a1b2c3"]')).toBeTruthy()
    // agent.code frames from the GitHub branch stream into the open live diff
    act(() => {
      window.dispatchEvent(new CustomEvent(LIVE_EVENT, { detail: { type: 'agent.code', payload: { frames: [
        { source: 'arena_github', branch: 'arena/a1b2c3', path: 'answer.py', commit: 'abcdef1234', diff: [{ type: 'add', text: 'return 42' }] },
        { source: 'workspace', branch: 'arena/a1b2c3', path: 'ignored.py', diff: [{ type: 'add', text: 'not from github' }] },
      ] } } }))
    })
    await waitFor(() => expect(panel.textContent).toContain('return 42'))
    expect(panel.textContent).not.toContain('not from github')
  })

  it('alerts when a session needs a human and resumes it', async () => {
    const { calls } = installFetchMock({
      '/api/v1/arena/agent/overview': overview([employee({ page_state: 'needs_human', needs_human_reason: 'stalled: no Arena activity for 20 minutes',
        human_events: [{ ts: '2026-11-16T10:00:00', kind: 'needs_human', detail: 'stalled' }] })]),
      '/api/v1/tasks': [],
      '/api/v1/arena/profiles/profile-backend-docs/resume': { requeued_tasks: ['task-1'] },
    })
    renderDashboard()
    const alert = await screen.findByTestId('arena-human-alert')
    expect(alert.textContent).toContain('stalled: no Arena activity')
    fireEvent.click(screen.getAllByRole('button', { name: /Resume session/ })[0])
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/resume') && c.method === 'POST')).toBeDefined())
  })

  it('is Arabic-first with RTL-safe labels (and Persian)', async () => {
    installFetchMock({ '/api/v1/arena/agent/overview': overview([employee()]), '/api/v1/tasks': [] })
    renderDashboard('ar')
    expect(await screen.findByText('وكيل Arena — التشغيل الكامل')).toBeTruthy()
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByRole('button', { name: /بدء جلسة جديدة/ })).toBeTruthy()
    expect(screen.getByTestId('arena-stat-connector').textContent).toContain('صفحة تجريبية')
  })

  it('renders Persian labels', async () => {
    installFetchMock({ '/api/v1/arena/agent/overview': overview([employee()]), '/api/v1/tasks': [] })
    renderDashboard('fa')
    expect(await screen.findByText('عامل Arena — عملیات کامل')).toBeTruthy()
  })
})

// ----------------------------------------------------------------------------- R7 manager card

const manager = (overrides: Record<string, any> = {}) => employee({
  agent_id: 'manager-0', agent_name: 'Chief AI Engineering Manager', agent_role: 'manager', is_manager: true,
  profile_id: 'profile-manager', profile_name: 'manager', page_state: 'ready', task: null,
  session: { id: 'msess-1', phase: 'manager_ready', page_state: 'ready', exchange_count: 3, last_activity_at: '2026-11-17T09:00:00' },
  selector_test: { ran: true, ok: true },
  activity: { kind: 'plan', status: 'running', updated_at: '2026-11-17T09:00:05' },
  ...overrides,
})

describe('ArenaAgentDashboard manager card (R7)', () => {
  beforeEach(() => localStorage.clear())

  it('shows the manager card above the worker cards with status, session, activity and selector result', async () => {
    installFetchMock({ '/api/v1/arena/agent/overview': overview([employee()], { manager: manager() }), '/api/v1/tasks': [] })
    renderDashboard()
    const card = await screen.findByTestId('arena-manager-card')
    const worker = screen.getByTestId('arena-employee-worker-1')
    // the manager card comes first in the document
    expect(card.compareDocumentPosition(worker) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(card).getByTestId('arena-manager-connection').textContent).toBe('Connected')
    expect(within(card).getByTestId('arena-manager-session').textContent).toContain('Manager session ready')
    expect(within(card).getByTestId('arena-manager-session').textContent).toContain('3')
    expect(within(card).getByTestId('arena-manager-activity').textContent).toContain('Planning')
    expect(card.textContent).toContain('Selector test passed')
    expect(card.textContent).toContain('manager')
  })

  it('runs the same controls as a worker card on the manager profile', async () => {
    const { calls } = installFetchMock({
      '/api/v1/arena/agent/overview': overview([employee()], { manager: manager() }),
      '/api/v1/tasks': [],
      '/api/v1/arena/profiles/profile-manager/command': (_path: string, init: any) => {
        const body = JSON.parse(init.body)
        if (body.command === 'read_last_reply') return { result: { text: 'تم إنشاء الخطة' } }
        if (body.command === 'selector_test') return { result: { ok: true, composer: { found: true } } }
        return { result: { ok: true } }
      },
      '/api/v1/arena/profiles/profile-manager/userscript': { script: '// ==UserScript==\n// @name AATM Arena Connector - profile-manager' },
      '/api/v1/arena/manager/new-session': { ok: true },
    })
    renderDashboard()
    const card = await screen.findByTestId('arena-manager-card')
    const onManager = (cmd: string) => calls.find((c) => c.path.endsWith('/profiles/profile-manager/command') && c.body?.command === cmd)
    fireEvent.click(within(card).getByRole('button', { name: /Continue working/ }))
    await waitFor(() => expect(onManager('keep_working')).toBeDefined())
    fireEvent.click(within(card).getByRole('button', { name: /Selector test/ }))
    await waitFor(() => expect(screen.getByTestId('arena-output-manager-0').textContent).toContain('All required elements were found'))
    expect(onManager('selector_test')).toBeDefined()
    fireEvent.click(within(card).getByRole('button', { name: /Read last reply/ }))
    await waitFor(() => expect(screen.getByTestId('arena-output-manager-0').textContent).toContain('تم إنشاء الخطة'))
    fireEvent.click(within(card).getByRole('button', { name: /Rotate userscript/ }))
    await waitFor(() => expect((within(card).getByLabelText(/Review this script/) as HTMLTextAreaElement).value).toContain('profile-manager'))
    fireEvent.click(within(card).getByRole('button', { name: /Start new session/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/arena/manager/new-session'))?.method).toBe('POST'))
    expect(onManager('new_session')).toBeUndefined() // the runtime opens the session and sends the context prompt
    // worker cards keep their own controls
    expect(within(screen.getByTestId('arena-employee-worker-1')).getByRole('button', { name: /Selector test/ })).toBeTruthy()
  })

  it('shows the Arabic not-connected message and disables page commands', async () => {
    installFetchMock({ '/api/v1/arena/agent/overview': overview([], { manager: manager({ connected: false, page_state: 'offline', session: null,
      selector_test: { ran: false, ok: false }, activity: { kind: 'chat', status: 'waiting_connection' } }) }), '/api/v1/tasks': [] })
    renderDashboard('ar')
    const card = await screen.findByTestId('arena-manager-card')
    expect(within(card).getByTestId('arena-manager-connection').textContent).toBe('غير متصل')
    expect(within(card).getByTestId('arena-manager-hint').textContent).toBe('المدير غير متصل بـ Arena — افتح ملف Chrome الخاص بالمدير')
    expect(within(card).getByTestId('arena-manager-activity').textContent).toContain('بانتظار الاتصال')
    expect((within(card).getByRole('button', { name: /اختبار/ }) as HTMLButtonElement).disabled).toBe(true)
    expect(document.documentElement.dir).toBe('rtl')
  })
})
