import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import App from './App'
import { installFetchMock, installWebSocketMock, settle } from './test/mockApi'

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

describe('AATM Frontend & Multilingual RTL System', () => {
  beforeEach(() => {
    localStorage.clear()
    installWebSocketMock()
  })

  // absorb late telemetry state updates from loadAll / WS refresh
  afterEach(async () => {
    await settle()
  })

  it('renders navigation and switches cleanly between EN (LTR), AR (RTL), and FA (RTL)', async () => {
    installFetchMock()
    render(<App />)

    expect(screen.getByText('AATM')).toBeInTheDocument()
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
    render(<App />)

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
    render(<App />)

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
    render(<App />)

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
    render(<App />)

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
    render(<App />)

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
    render(<App />)

    fireEvent.click(screen.getByText('AR'))
    fireEvent.click(navButton('الموظفون'))

    expect(await screen.findByPlaceholderText('اسم الموظف (مثل: متخصص واجهات برمجية)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /إنشاء موظف/ })).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
    await settle()
  })

  it('launches an SDLC build from the Projects tab', async () => {
    const { calls } = installFetchMock()
    render(<App />)

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
    render(<App />)

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
    const { container } = render(<App />)
    for (const label of EN_NAV) {
      fireEvent.click(navButton(label))
      expect(container.innerHTML).not.toMatch(/localhost|127\.0\.0\.1/)
    }
    await settle()
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
    const { calls } = installFetchMock()
    render(<App />)

    fireEvent.click(navButton('GitHub Integration'))

    fireEvent.change(await screen.findByPlaceholderText('Pull request title'), {
      target: { value: 'feat(frontend): sprint v2.2 governance controls' },
    })

    // 1) operator has no approval yet -> request one from governance
    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))

    await waitFor(() => {
      const approval = calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')
      expect(approval).toBeDefined()
      expect(approval?.body).toMatchObject({ action_type: 'merge' })
      expect(approval?.authorization).toBe('Bearer test-jwt')
    })

    // the quick action must populate the approval id field for the operator
    await waitFor(() =>
      expect(screen.getByPlaceholderText('Approval UUID')).toHaveValue('appr-1')
    )
    expect(await screen.findByText(/Approval requested: appr-1/)).toBeInTheDocument()

    // 2) submit the write, referencing the approval
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

    expect(await screen.findByText(/pull\/12/)).toBeInTheDocument()
    await settle()
  })

  it('creates an approval-gated GitHub issue with deploy-class approval', async () => {
    const { calls } = installFetchMock()
    render(<App />)

    fireEvent.click(navButton('GitHub Integration'))
    fireEvent.click(await screen.findByRole('button', { name: /^Issue$/ }))
    fireEvent.change(await screen.findByPlaceholderText('Issue title'), {
      target: { value: 'RTL audit follow-ups' },
    })

    fireEvent.click(screen.getByRole('button', { name: /Request Approval/ }))
    await waitFor(() => {
      const approval = calls.find((c) => c.path === '/api/v1/approvals' && c.method === 'POST')
      expect(approval?.body).toMatchObject({ action_type: 'deploy' })
    })

    await waitFor(() => expect(screen.getByPlaceholderText('Approval UUID')).toHaveValue('appr-1'))
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

    expect(await screen.findByText(/issues\/34/)).toBeInTheDocument()
    await settle()
  })

  it('blocks GitHub writes until an approval id is present', async () => {
    const { calls } = installFetchMock()
    render(<App />)

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
    render(<App />)

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
    render(<App />)

    fireEvent.click(navButton('Arena & AI Providers'))
    expect(await screen.findByText(/\$0\.02 \/ \$30\.00/)).toBeInTheDocument()
    // the budgets setting was requested, failed, and the UI degraded to summed provider caps
    expect(failing.calls.some((c) => c.path === '/api/v1/settings')).toBe(true)
    await settle()
  })
})
