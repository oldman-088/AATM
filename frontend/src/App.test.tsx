import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import App from './App'
import { MockWebSocket, installFetchMock, installWebSocketMock, settle } from './test/mockApi'

const AR_DASHBOARD_TITLE = 'مركز القيادة التنفيذي'

const EN_NAV = [
  'Executive Dashboard',
  'Manager Chat',
  'AI Employees & Org',
  'Tasks & Messaging',
  'Workspaces & Files',
  'Browsers & Userscripts',
  'Arena & AI Providers',
  'Projects, SDLC & Memory',
  'GitHub Integration',
  'Monitoring, QA & Devices',
  'Approvals & Activity Logs',
  'Security & Settings',
]

/** Sidebar nav buttons are the ones that carry an icon + label pair. */
function navButton(label: string) {
  const buttons = screen.getAllByRole('button', { name: new RegExp(label) })
  return buttons[0]
}

async function renderWorkspace() {
  const view = render(<App />)
  await screen.findByText('AATM')
  return view
}

describe('AATM Frontend & Multilingual RTL System', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    installWebSocketMock()
  })

  // absorb late telemetry state updates from loadAll / WS refresh
  afterEach(async () => {
    await settle()
  })

  it('renders navigation and switches cleanly between EN (LTR), AR (RTL), and FA (RTL)', async () => {
    installFetchMock()
    await renderWorkspace()
    expect(document.documentElement.dir).toBe('ltr')
    expect(document.documentElement.lang).toBe('en')

    // Switch to Arabic
    fireEvent.click(screen.getByText('AR'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('ar')
    expect(screen.getByText('مدير فريق وكلاء الذكاء الاصطناعي')).toBeInTheDocument()

    // Switch to Persian
    fireEvent.click(screen.getByText('FA'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('fa')
    expect(screen.getByText('مدیر تیم عامل‌های هوش مصنوعی')).toBeInTheDocument()

    // Switch back to English
    fireEvent.click(screen.getByText('EN'))
    expect(document.documentElement.dir).toBe('ltr')
    expect(document.documentElement.lang).toBe('en')
    await settle()
  })

  it('renders all 12 navigation tabs and keeps labels localized in each locale', async () => {
    installFetchMock()
    await renderWorkspace()
    // EN: every tab label is present in the sidebar
    for (const label of EN_NAV) {
      expect(navButton(label)).toBeInTheDocument()
    }

    // AR: nav labels translate and document flips to RTL
    fireEvent.click(screen.getByText('AR'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getAllByRole('button', { name: /لوحة القيادة/ }).length).toBeGreaterThan(0)

    // FA: nav labels translate and remain RTL
    fireEvent.click(screen.getByText('FA'))
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getAllByRole('button', { name: /داشبورد/ }).length).toBeGreaterThan(0)

    fireEvent.click(screen.getByText('EN'))
    expect(document.documentElement.dir).toBe('ltr')
    await settle()
  })

  it('mounts every one of the 12 tabs without crashing', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()

    for (const label of EN_NAV) {
      fireEvent.click(navButton(label))
      // each tab click must not throw and must keep the shell mounted
      expect(screen.getByText('AATM')).toBeInTheDocument()
    }

    // The telemetry loader must have hit the core sections for each mounted tab
    const paths = calls.map((c) => c.path)
    expect(paths).toContain('/api/v1/dashboard/overview')
    expect(paths).toContain('/api/v1/qa/summary')
    expect(paths).toContain('/api/v1/git/status')
    expect(paths).toContain('/api/v1/devices')
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    await settle()
  })

  it('switches tab content between English and Arabic renderings', async () => {
    installFetchMock()
    await renderWorkspace()

    fireEvent.click(navButton('Executive Dashboard'))
    await screen.findByText('Executive Command Center', { selector: 'h2' })

    fireEvent.click(screen.getByText('AR'))
    await screen.findByText(AR_DASHBOARD_TITLE, { selector: 'h2' })
    expect(document.documentElement.dir).toBe('rtl')
    await settle()
  })

  it('sends a Global Emergency Stop through apiFetch and then clears it', async () => {
    const { calls } = installFetchMock()
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    await renderWorkspace()

    fireEvent.click(screen.getByRole('button', { name: /Emergency Stop/ }))

    await waitFor(() => {
      const stop = calls.find((c) => c.path === '/api/v1/emergency-stop' && c.method === 'POST')
      expect(stop).toBeDefined()
      expect(stop?.body).toMatchObject({ scope: 'global' })
      expect(stop?.body.reason).toBe('Operator clicked Emergency Stop in Command Center')
      // JWT must be attached by apiFetch (Worker 4 hardening)
      expect(stop?.authorization).toBe('Bearer test-jwt')
    })

    // Simulate the backend reporting a stopped agent -> the control becomes "Resume System"
    installFetchMock({
      '/api/v1/agents': [
        {
          id: 'manager-0',
          name: 'Chief AI Engineering Manager',
          role: 'manager',
          status: 'paused',
          divisions: ['D00'],
          tasks_completed: 1,
          is_emergency_stopped: true,
        },
      ],
    })
    fireEvent.click(navButton('Executive Dashboard'))
    fireEvent.click(screen.getByRole('button', { name: /Run Orchestration Cycle/ }))

    const resume = await screen.findByRole('button', { name: /Resume System/ })
    const { calls: resumeCalls } = installFetchMock()
    fireEvent.click(resume)
    await waitFor(() => {
      expect(resumeCalls.find((c) => c.path === '/api/v1/emergency-stop/clear' && c.method === 'POST')).toBeDefined()
    })
    await settle()
  })

  it('creates an AI employee with isolated workspace from the Agents tab', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()

    fireEvent.click(navButton('AI Employees & Org'))
    const nameInput = await screen.findByPlaceholderText('Employee Name (e.g., API Specialist)')
    fireEvent.change(nameInput, { target: { value: 'RTL Specialist' } })
    fireEvent.click(screen.getByRole('button', { name: /Create Employee/ }))

    await waitFor(() => {
      const post = calls.find((c) => c.path === '/api/v1/employees' && c.method === 'POST')
      expect(post).toBeDefined()
      expect(post?.body).toMatchObject({
        name: 'RTL Specialist',
        role_template_id: 'backend_developer',
        allocation_mode: 'hybrid',
        autonomy_mode: 'semi_autonomous',
      })
    })

    expect(await screen.findByText(/isolated workspace & session provisioned/)).toBeInTheDocument()
    await settle()
  })

  it('localizes the AI employee creation form into Arabic (RTL)', async () => {
    installFetchMock()
    await renderWorkspace()

    fireEvent.click(screen.getByText('AR'))
    fireEvent.click(navButton('الموظفون'))

    expect(await screen.findByPlaceholderText('اسم الموظف (مثل: متخصص واجهات برمجية)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /إنشاء موظف/ })).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
    await settle()
  })

  it('launches an SDLC build from the Projects tab', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()

    fireEvent.click(navButton('Projects, SDLC & Memory'))
    fireEvent.change(await screen.findByPlaceholderText('Project Name'), { target: { value: 'AATM v2.1' } })
    fireEvent.change(screen.getByPlaceholderText(/Describe your software idea/), {
      target: { value: 'Ship the multilingual enterprise console' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Launch SDLC Build/ }))

    await waitFor(() => {
      const post = calls.find((c) => c.path === '/api/v1/sdlc/execute' && c.method === 'POST')
      expect(post?.body).toMatchObject({
        project_name: 'AATM v2.1',
        idea: 'Ship the multilingual enterprise console',
      })
    })

    const banner = await screen.findByText(/Delivered/, { exact: false })
    expect(within(banner).getByText('AATM v2.1', { exact: false })).toBeInTheDocument()
    await settle()
  })

  it('registers a device node and sends an authenticated heartbeat from the QA tab', async () => {
    const devices = [
      {
        id: 'node-1',
        name: 'GPU Worker',
        device_type: 'remote_server',
        host_address: '10.0.0.20',
        os_name: 'Ubuntu 24.04',
        cpu_cores: 16,
        status: 'online',
      },
    ]
    const { calls } = installFetchMock({ '/api/v1/devices': devices })
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    await renderWorkspace()

    fireEvent.click(navButton('Monitoring, QA & Devices'))
    fireEvent.change(await screen.findByPlaceholderText('Node Name'), { target: { value: 'Edge Node' } })
    fireEvent.click(screen.getByRole('button', { name: /Register Node/ }))

    await waitFor(() => {
      const post = calls.find((c) => c.path === '/api/v1/devices' && c.method === 'POST')
      expect(post?.body).toMatchObject({ name: 'Edge Node', device_type: 'remote_server' })
    })

    // Heartbeat must use apiFetch so the JWT is attached (Worker 4 hardening)
    fireEvent.click(screen.getByRole('button', { name: /Heartbeat/ }))
    await waitFor(() => {
      const beat = calls.find((c) => c.path === '/api/v1/devices/node-1/heartbeat' && c.method === 'POST')
      expect(beat).toBeDefined()
      expect(beat?.authorization).toBe('Bearer test-jwt')
    })
    await settle()
  })

  it('never renders a localhost or 127.0.0.1 URL in the UI', async () => {
    installFetchMock()
    const { container } = await renderWorkspace()
    for (const label of EN_NAV) {
      fireEvent.click(navButton(label))
      expect(container.innerHTML).not.toMatch(/localhost|127\.0\.0\.1/)
    }
    await settle()
  })
})

describe('Manager Chat project selection', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    installWebSocketMock()
  })
  afterEach(async () => {
    await settle()
  })

  async function liveRefresh() {
    await act(async () => {
      MockWebSocket.instances.forEach((ws) => ws.onmessage?.({ data: 'refresh' }))
    })
    await settle()
  }

  it('follows the project marked active when it changes, but never overrides a hand-picked project', async () => {
    // A page that stays open used to keep the project it chose at load, so orders went to the wrong project.
    const oldP = { id: 'p-old', name: 'Old project', status: 'active' }
    const newP = { id: 'p-new', name: 'New project', status: 'active' }
    let projects = [oldP, newP] // the API lists the project marked active first
    installFetchMock({ '/api/v1/projects': () => projects })
    await renderWorkspace()
    fireEvent.click(navButton('Manager Chat'))
    const select = (await screen.findByLabelText('Project')) as HTMLSelectElement
    await waitFor(() => expect(select.value).toBe('p-old'))

    projects = [newP, oldP] // the owner activates the other project elsewhere
    await liveRefresh()
    await waitFor(() => expect(select.value).toBe('p-new'))

    fireEvent.change(select, { target: { value: 'p-old' } }) // now the owner chooses by hand
    expect(select.value).toBe('p-old')
    projects = [newP, oldP]
    await liveRefresh()
    expect(select.value).toBe('p-old')
  })
})

describe('Sprint v2.2 — approval-gated GitHub writes & provider budgets', () => {
  beforeEach(() => {
    localStorage.clear()
    installWebSocketMock()
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
  })

  afterEach(async () => {
    await settle()
  })

  it('requests a pending approval and then opens an approval-gated pull request', async () => {
    const { calls } = installFetchMock({
      '/api/v1/approvals': (_path: string, init: RequestInit) => (init.method || 'GET') === 'POST'
        ? { id: 'appr-1', status: 'pending' }
        : [{ id: 'appr-1', status: 'approved' }],
    })
    await renderWorkspace()

    fireEvent.click(navButton('GitHub Integration'))

    fireEvent.change(await screen.findByPlaceholderText('Pull request title'), {
      target: { value: 'feat(frontend): sprint v2.2 governance controls' },
    })

    // 1) operator has no approval yet -> request one from governance
    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))

    await waitFor(() => {
      const approval = calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')
      expect(approval).toBeDefined()
      expect(approval?.body).toMatchObject({ action_type: 'merge', payload: { kind: 'pr', base: 'main', target_branch: 'main' } })
      expect(approval?.body.payload).not.toHaveProperty('target')
      expect(approval?.authorization).toBe('Bearer test-jwt')
    })

    // the quick action must populate the approval id field for the operator
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Approval UUID')).toHaveValue('appr-1')
    )
    expect(await screen.findByText(/Approval requested: appr-1/)).toBeInTheDocument()

    // 2) submit after the approvals list reports the approved status
    await waitFor(() => expect(screen.getByText('Approval status: Approved')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Create Pull Request/ }))

    await waitFor(() => {
      const pr = calls.find((c) => c.path === '/api/v1/github/prs' && c.method === 'POST')
      expect(pr).toBeDefined()
      expect(pr?.body).toMatchObject({
        approval_id: 'appr-1',
        repo: 'oldman-088/AATM',
        title: 'feat(frontend): sprint v2.2 governance controls',
        head: 'arena/51325356-aatm',
        base: 'main',
      })
      expect(pr?.authorization).toBe('Bearer test-jwt')
    })

    expect(await screen.findByRole('link', { name: /pull\/12/ })).toHaveAttribute('href', 'https://github.com/oldman-088/AATM/pull/12')
    await settle()
  })

  it('creates an approval-gated GitHub issue with merge-class approval', async () => {
    const { calls } = installFetchMock({
      '/api/v1/approvals': (_path: string, init: RequestInit) => (init.method || 'GET') === 'POST'
        ? { id: 'appr-1', status: 'pending' }
        : [{ id: 'appr-1', status: 'approved' }],
    })
    await renderWorkspace()

    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: /^Issue$/ }))
    fireEvent.change(await screen.findByPlaceholderText('Issue title'), {
      target: { value: 'RTL audit follow-ups' },
    })

    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))
    await waitFor(() => {
      const approval = calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')
      expect(approval?.body).toMatchObject({ action_type: 'merge', payload: { kind: 'issue' } })
      expect(approval?.body.payload).not.toHaveProperty('target_branch')
    })

    await waitFor(() => expect(screen.getByPlaceholderText('Approval UUID')).toHaveValue('appr-1'))
    await waitFor(() => expect(screen.getByText('Approval status: Approved')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Create Issue/ }))

    await waitFor(() => {
      const issue = calls.find((c) => c.path === '/api/v1/github/issues' && c.method === 'POST')
      expect(issue?.body).toMatchObject({
        approval_id: 'appr-1',
        repo: 'oldman-088/AATM',
        title: 'RTL audit follow-ups',
      })
      // issues are not branch-scoped
      expect(issue?.body).not.toHaveProperty('head')
      expect(issue?.body).not.toHaveProperty('base')
    })

    expect(await screen.findByRole('link', { name: /issues\/34/ })).toHaveAttribute('href', 'https://github.com/oldman-088/AATM/issues/34')
    await settle()
  })

  it('shows pending and blocks submission until approval', async () => {
    installFetchMock()
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.change(await screen.findByPlaceholderText('Pull request title'), { target: { value: 'Pending PR' } })
    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))
    expect(await screen.findByText('Approval status: Pending')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create Pull Request/ })).toBeDisabled()
  })

  it('requests a branch-scoped approval and submits the approved branch write', async () => {
    const { calls } = installFetchMock({
      '/api/v1/approvals': (_path: string, init: RequestInit) => (init.method || 'GET') === 'POST'
        ? { id: 'branch-approval', status: 'pending' }
        : [{ id: 'branch-approval', status: 'approved', action_type: 'branch' }],
    })
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: /^Branch$/ }))
    expect(screen.getByLabelText('From branch')).toHaveValue('main')
    expect(screen.getByRole('button', { name: /Request Approval/ })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Branch name'), { target: { value: 'feature/rtl' } })
    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')?.body).toMatchObject({
      action_type: 'branch', payload: { kind: 'branch', repo: 'oldman-088/AATM', branch_name: 'feature/rtl', from_branch: 'main' },
    }))
    await waitFor(() => expect(screen.getByText('Approval status: Approved')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Create Branch' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/github/branches')?.body).toEqual({
      approval_id: 'branch-approval', repo: 'oldman-088/AATM', branch_name: 'feature/rtl', from_branch: 'main',
    }))
    expect(await screen.findByText(/feature\/rtl/)).toBeInTheDocument()
  })

  it('surfaces a branch write rejected by the backend policy', async () => {
    const mock = installFetchMock({ '/api/v1/approvals': [{ id: 'approved-branch', status: 'approved' }] })
    const original = mock.mock.getMockImplementation()!
    mock.mock.mockImplementation(async (input: any, init: RequestInit = {}) => String(input) === '/api/v1/github/branches'
      ? { ok: false, status: 403, json: async () => ({ detail: 'Approval does not match the requested branch write' }) }
      : original(input, init))
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: /^Branch$/ }))
    fireEvent.change(screen.getByLabelText('Branch name'), { target: { value: 'feature/rejected' } })
    fireEvent.change(screen.getByPlaceholderText('Approval UUID'), { target: { value: 'approved-branch' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create Branch' }))
    expect(await screen.findByText('Approval does not match the requested branch write')).toBeInTheDocument()
  })

  it('blocks branch submission while its approval is pending', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: /^Branch$/ }))
    fireEvent.change(screen.getByLabelText('Branch name'), { target: { value: 'feature/a' } })
    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))
    expect(await screen.findByText('Approval status: Pending')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create Branch' })).toBeDisabled()
    expect(calls.some((c) => c.path === '/api/v1/github/branches')).toBe(false)
  })

  it('blocks GitHub writes until an approval id is present', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()

    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.change(await screen.findByPlaceholderText('Pull request title'), {
      target: { value: 'Needs governance approval' },
    })

    const submit = screen.getByRole('button', { name: /Create Pull Request/ })
    expect(submit).toBeDisabled()
    fireEvent.click(submit)
    expect(calls.find((c) => c.path === '/api/v1/github/prs')).toBeUndefined()
    await settle()
  })

  it('renders daily & monthly provider budget headroom from settings and usage logs', async () => {
    const now = new Date()
    const logs = [{ created_at: now.toISOString(), estimated_cost_usd: 3.5 }]
    let monthlySpend = 3.5
    if (now.getDate() > 1) {
      logs.push({
        created_at: new Date(now.getFullYear(), now.getMonth(), 1, 9, 0, 0).toISOString(),
        estimated_cost_usd: 4.25,
      })
      monthlySpend = 7.75
    }

    installFetchMock({
      '/api/v1/providers': [
        {
          id: 'arena-agent',
          name: 'Arena Agent',
          provider_type: 'arena',
          default_model: 'arena-agent',
          credential_status: 'configured',
          health_status: 'healthy',
          daily_limit_usd: 20,
        },
      ],
      '/api/v1/providers/usage': {
        summary: { total_input_tokens: 1200, total_output_tokens: 800, total_estimated_cost_usd: 7.75 },
        logs,
      },
    })
    await renderWorkspace()

    fireEvent.click(navButton('Arena & AI Providers'))

    expect(await screen.findByText('Provider Budget & Headroom')).toBeInTheDocument()
    // daily: $3.50 spent against the $25 settings limit -> $21.50 headroom
    expect(screen.getByText('$3.50 / $25.00')).toBeInTheDocument()
    expect(screen.getByText('$21.50')).toBeInTheDocument()
    // monthly: spend within the current month against the $400 limit
    expect(
      screen.getByText(`$${monthlySpend.toFixed(2)} / $400.00`)
    ).toBeInTheDocument()
    expect(screen.getByText(`$${(400 - monthlySpend).toFixed(2)}`)).toBeInTheDocument()
    // governance threshold surfaced next to the limits
    expect(screen.getByText('$5.00')).toBeInTheDocument()
    await settle()
  })

  it('prefers the usage budget summary and its returned remaining headroom', async () => {
    installFetchMock({
      '/api/v1/providers/usage': {
        summary: { total_input_tokens: 0, total_output_tokens: 0, total_estimated_cost_usd: 99 },
        logs: [],
        budget_summary: {
          daily_limit_usd: 50, global_daily_spend_usd: 8, daily_remaining_usd: 42,
          monthly_limit_usd: 500, global_monthly_spend_usd: 75, monthly_remaining_usd: 425,
        },
      },
    })
    await renderWorkspace()
    fireEvent.click(navButton('Arena & AI Providers'))
    expect(await screen.findByText('$8.00 / $50.00')).toBeInTheDocument()
    expect(screen.getByText('$42.00')).toBeInTheDocument()
    expect(screen.getByText('$75.00 / $500.00')).toBeInTheDocument()
    expect(screen.getByText('$425.00')).toBeInTheDocument()
  })

  it('renders zero budget limits and returned zero headroom', async () => {
    installFetchMock({ '/api/v1/providers/usage': {
      summary: { total_input_tokens: 0, total_output_tokens: 0, total_estimated_cost_usd: 0 }, logs: [],
      budget_summary: { daily_limit_usd: 0, monthly_limit_usd: 0, global_daily_spend_usd: 0, global_monthly_spend_usd: 0, daily_remaining_usd: 0, monthly_remaining_usd: 0 },
    } })
    await renderWorkspace()
    fireEvent.click(navButton('Arena & AI Providers'))
    expect(await screen.findAllByText('$0.00 / $0.00')).toHaveLength(2)
    expect(screen.getAllByText('$0.00').length).toBeGreaterThanOrEqual(2)
  })

  it('passes selected agent and session to provider dry run', async () => {
    const { calls } = installFetchMock({
      '/api/v1/providers': [{ id: 'p1', name: 'P1', provider_type: 'arena', default_model: 'model', credential_status: 'set', health_status: 'healthy', daily_limit_usd: 10 }],
      '/api/v1/sessions': [{ id: 'session-1' }],
      '/api/v1/providers/p1/test': { provider_id: 'p1', model: 'model', content: 'ok' },
    })
    await renderWorkspace()
    fireEvent.click(navButton('Arena & AI Providers'))
    await screen.findByRole('option', { name: 'session-1' })
    fireEvent.change(screen.getByLabelText('Optional agent'), { target: { value: 'worker-1' } })
    fireEvent.change(screen.getByLabelText('Optional session'), { target: { value: 'session-1' } })
    fireEvent.click(screen.getByRole('button', { name: /Safe Dry-Run Test/i }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/providers/p1/test')?.body).toMatchObject({ agent_id: 'worker-1', session_id: 'session-1', dry_run: true }))
  })

  it('falls back to provider caps when the budgets setting is unavailable', async () => {
    const failing = installFetchMock({
      '/api/v1/settings': () => {
        throw new Error('settings unavailable')
      },
      '/api/v1/providers': [
        { id: 'p1', name: 'P1', provider_type: 'openai', default_model: 'gpt', credential_status: 'set', health_status: 'healthy', daily_limit_usd: 20 },
        { id: 'p2', name: 'P2', provider_type: 'anthropic', default_model: 'claude', credential_status: 'set', health_status: 'healthy', daily_limit_usd: 10 },
      ],
    })
    await renderWorkspace()

    fireEvent.click(navButton('Arena & AI Providers'))
    expect(await screen.findByText(/\$0\.02 \/ \$30\.00/)).toBeInTheDocument()
    // the budgets setting was requested, failed, and the UI degraded to summed provider caps
    expect(failing.calls.some((c) => c.path === '/api/v1/settings')).toBe(true)
    await settle()
  })
})


describe('Round 4 workflow and Git controls', () => {
  beforeEach(() => { localStorage.clear(); localStorage.setItem('aatm_jwt_token', 'test-jwt'); installWebSocketMock() })
  afterEach(async () => { await settle() })

  it('edits, reorders and submits a structured browser workflow with safe text output', async () => {
    const { calls } = installFetchMock({ '/api/v1/browsers': [{ id: 'profile-1', name: 'Profile One', status: 'stopped', mode: 'background', current_url: 'about:blank' }] })
    await renderWorkspace()
    fireEvent.click(navButton('Browsers & Userscripts'))
    fireEvent.change(await screen.findByLabelText('Browser profile'), { target: { value: 'profile-1' } })
    fireEvent.change(screen.getByLabelText('Engine'), { target: { value: 'automa' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    fireEvent.change(screen.getByLabelText('URL 1'), { target: { value: 'https://example.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }))
    fireEvent.change(screen.getByLabelText('Step type 2'), { target: { value: 'extract_text' } })
    fireEvent.change(screen.getByLabelText('Selector 2'), { target: { value: 'h1' } })
    fireEvent.click(screen.getByLabelText('Move up 2'))
    fireEvent.click(screen.getByRole('button', { name: 'Run workflow' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/browsers/profile-1/workflow')?.body).toEqual({
      engine: 'automa', steps: [{ type: 'extract_text', selector: 'h1' }, { type: 'navigate', url: 'https://example.com' }],
    }))
    expect(await screen.findByText('redacted result')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Remove step 1'))
    expect(screen.queryByLabelText('Selector 1')).not.toBeInTheDocument()
  })

  it('creates and confirms removal of an agent worktree with authenticated requests', async () => {
    const { calls } = installFetchMock()
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.change(await screen.findByLabelText('Agent'), { target: { value: 'worker-1' } })
    fireEvent.change(screen.getByLabelText('Worktree branch'), { target: { value: 'worker-1/task' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create Worktree' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/git/worktrees' && c.method === 'POST')?.body).toEqual({ agent_id: 'worker-1', branch_name: 'worker-1/task', base_ref: 'HEAD' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove Worktree' }))
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/git/worktrees/worker-1' && c.method === 'DELETE')?.authorization).toBe('Bearer test-jwt'))
  })

  it('requests approval and posts a PR comment', async () => {
    const { calls } = installFetchMock({ '/api/v1/approvals': (_path: string, init: RequestInit) => (init.method || 'GET') === 'POST'
      ? { id: 'comment-approval', status: 'pending' } : [{ id: 'comment-approval', status: 'approved' }] })
    await renderWorkspace()
    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: 'PR Comment / Review' }))
    fireEvent.change(screen.getByLabelText('PR number'), { target: { value: '12' } })
    fireEvent.change(screen.getByLabelText('Comment body'), { target: { value: 'Looks good.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Request Approval' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')?.body).toMatchObject({ action_type: 'merge', payload: { kind: 'comment', repo: 'oldman-088/AATM', pr_number: 12, event: 'COMMENT', body: 'Looks good.' } }))
    await waitFor(() => expect(screen.getByText('Approval status: Approved')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Submit Comment' }))
    await waitFor(() => expect(calls.find((c) => c.path === '/api/v1/github/prs/12/comments')?.body).toEqual({ approval_id: 'comment-approval', repo: 'oldman-088/AATM', event: 'COMMENT', body: 'Looks good.' }))
    expect(await screen.findByRole('link', { name: /issuecomment-1/ })).toBeInTheDocument()
  })
})

describe('Phase 1 Frontend Security & Auth Gates', () => {
  beforeEach(() => {
    localStorage.clear()
    installWebSocketMock()
  })

  afterEach(async () => {
    await settle()
  })

  it('renders an empty login form with no pre-filled credentials and blocks data/WS calls until authenticated', async () => {
    const { calls } = installFetchMock()
    const wsMock = installWebSocketMock()
    render(<App />)

    const emailInput = await screen.findByLabelText('Email') as HTMLInputElement
    const passwordInput = screen.getByLabelText('Password') as HTMLInputElement
    expect(emailInput.value).toBe('')
    expect(passwordInput.value).toBe('')
    expect(screen.queryByRole('button', { name: /Demo Director Login/i })).not.toBeInTheDocument()
    expect(calls.some((c) => c.path.includes('/dashboard/overview'))).toBe(false)
    expect(wsMock.instances.length).toBe(0)

    fireEvent.change(emailInput, { target: { value: 'director@aatm.ai' } })
    fireEvent.change(passwordInput, { target: { value: 'super-secure-password-2026' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign In' }))

    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/dashboard/overview')).toBe(true))
    expect(wsMock.instances.length).toBe(1)
    expect(wsMock.instances[0].url).not.toContain('?token=')
    wsMock.instances[0].onopen?.({})
    expect(wsMock.instances[0].sent[0]).toBe(JSON.stringify({ token: 'test-jwt-token' }))
  })

  it('forces password change when must_change_password is true before revealing workspace', async () => {
    const wsMock = installWebSocketMock()
    localStorage.setItem('aatm_jwt_token', 'first-login-jwt')
    const { calls } = installFetchMock({
      '/api/v1/auth/me': {
        id: 'admin-1',
        email: 'admin@aatm.local',
        full_name: 'Chief Engineering Director',
        role: 'admin',
        language: 'en',
        theme: 'dark',
        must_change_password: true,
      },
    })
    render(<App />)

    expect(await screen.findByText('Password Change Required')).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/api/v1/dashboard/overview')).toBe(false)
    expect(wsMock.instances.length).toBe(0)

    fireEvent.change(screen.getByLabelText('Current Password'), { target: { value: 'initial-pw-123' } })
    fireEvent.change(screen.getByLabelText('New Password'), { target: { value: 'short' } })
    fireEvent.change(screen.getByLabelText('Confirm New Password'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: 'Update Password' }))
    expect(await screen.findByText('New password must be at least 12 characters')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('New Password'), { target: { value: 'NewStrongPassword!2026' } })
    fireEvent.change(screen.getByLabelText('Confirm New Password'), { target: { value: 'DifferentPassword!2026' } })
    fireEvent.click(screen.getByRole('button', { name: 'Update Password' }))
    expect(await screen.findByText('New passwords do not match')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Confirm New Password'), { target: { value: 'NewStrongPassword!2026' } })
    fireEvent.click(screen.getByRole('button', { name: 'Update Password' }))

    await waitFor(() => {
      const changeCall = calls.find((c) => c.path === '/api/v1/auth/change-password')
      expect(changeCall?.authorization).toBe('Bearer first-login-jwt')
      expect(changeCall?.body).toEqual({
        current_password: 'initial-pw-123',
        new_password: 'NewStrongPassword!2026',
      })
    })
    await waitFor(() => expect(screen.getAllByText('Executive Dashboard').length).toBeGreaterThan(0))
    expect(localStorage.getItem('aatm_jwt_token')).toBe('dummy-rotated-jwt-token')
    expect(wsMock.instances.length).toBe(1)
    expect(wsMock.instances[0].url).not.toContain('?token=')
    wsMock.instances[0].onopen?.({})
    expect(wsMock.instances[0].sent[0]).toBe(JSON.stringify({ token: 'dummy-rotated-jwt-token' }))
  })

  it('displays SANDBOX_UNAVAILABLE status banner on Dashboard when sandbox image or Docker is missing', async () => {
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    installWebSocketMock()
    installFetchMock({
      '/api/v1/dashboard/overview': {
        kpis: {
          active_workers: 0,
          idle_workers: 3,
          total_agents: 3,
          completion_rate: 0,
          healthy_providers: 0,
          total_providers: 3,
          running_browsers: 0,
          total_browsers: 1,
          pending_approvals: 0,
        },
        divisions: [{ code: 'D01', progress_percent: 0, completed_tasks: 0, total_tasks: 0 }],
        resources: {
          cpu_count: 4,
          cpu_percent: 10,
          ram_used_gb: 2,
          ram_total_gb: 8,
          ram_percent: 25,
          disk_free_gb: 50,
          disk_percent: 20,
        },
        recommendations: [],
        recent_events: [],
        sandbox: {
          status: 'SANDBOX_UNAVAILABLE',
          available: false,
          mode: 'docker',
          image: 'aatm-sandbox:latest',
          reason: "Sandbox image 'aatm-sandbox:latest' is missing.",
        },
      },
    })
    await renderWorkspace()
    fireEvent.click(navButton('Executive Dashboard'))
    expect(await screen.findByText('SANDBOX_UNAVAILABLE')).toBeInTheDocument()
    expect(screen.getByText(/Sandbox image 'aatm-sandbox:latest' is missing\./)).toBeInTheDocument()
  })

  it('displays NO_MODEL_CONFIGURED banner with link to Provider Setup Wizard and renders Unknown cost', async () => {
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    installWebSocketMock()
    const { calls } = installFetchMock({
      '/api/v1/dashboard/overview': {
        kpis: {
          active_workers: 0,
          idle_workers: 4,
          total_agents: 4,
          completion_rate: 0,
          healthy_providers: 0,
          total_providers: 3,
          running_browsers: 0,
          total_browsers: 4,
          pending_approvals: 0,
        },
        divisions: [{ code: 'D01', progress_percent: 0, completed_tasks: 0, total_tasks: 0 }],
        resources: {
          cpu_count: 4,
          cpu_percent: 10,
          ram_used_gb: 2,
          ram_total_gb: 8,
          ram_percent: 25,
          disk_free_gb: 50,
          disk_percent: 20,
        },
        recommendations: [],
        recent_events: [],
        sandbox: { status: 'READY', available: true, mode: 'docker', image: 'aatm-sandbox:latest', reason: null },
        model_status: {
          status: 'NO_MODEL_CONFIGURED',
          configured: false,
          configured_providers: [],
          diagnostic_only_providers: ['arena-local-bridge'],
          reason: 'No real model configured.',
        },
      },
      '/api/v1/providers/usage': {
        summary: {
          total_input_tokens: 500,
          total_output_tokens: 100,
          total_estimated_cost_usd: 0,
          pricing_known: false,
          has_unknown_pricing: true,
        },
        logs: [],
      },
    })
    await renderWorkspace()
    fireEvent.click(navButton('Executive Dashboard'))
    expect(await screen.findByText('NO_MODEL_CONFIGURED')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Open Provider Setup Wizard/i }))

    expect(await screen.findByText('Model Provider Setup Wizard')).toBeInTheDocument()
    expect(screen.getByText('Unknown cost')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Test Connection/i }))
    await waitFor(() => {
      const testCall = calls.find((c) => c.path === '/api/v1/providers/wizard/test-connection')
      expect(testCall).toBeDefined()
    })
    expect(await screen.findByText(/Connected to claude-sonnet-4-5 in 42\.5 ms/)).toBeInTheDocument()
  })

  it('Phase 7: renders all 12 mobile navigation tabs, toggles light mode, and displays QA security posture', async () => {
    installFetchMock()
    localStorage.setItem('aatm_jwt_token', 'test-jwt')
    await renderWorkspace()

    // Mobile navigation exposes the Command Bridge plus the 12 advanced screens.
    const mobileNav = await screen.findByLabelText('Mobile navigation')
    expect(mobileNav.querySelectorAll('button')).toHaveLength(13)

    // Light/dark theme toggle updates root element class cleanly
    const themeBtn = screen.getByRole('button', { name: /Theme Mode/i })
    fireEvent.click(themeBtn)
    expect(document.documentElement.classList.contains('light')).toBe(true)
    fireEvent.click(themeBtn)
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    // QA view displays Runtime Security Posture from /governance/security-posture
    fireEvent.click(navButton('Monitoring, QA & Devices'))
    expect(await screen.findByText('Runtime Security Posture')).toBeInTheDocument()
    expect(screen.getByText(/HTTP Headers Enforced/)).toBeInTheDocument()
  })
})
