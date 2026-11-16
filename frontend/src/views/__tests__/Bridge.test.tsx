import React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock, settle } from '../../test/mockApi'
import CommandBridge from '../../bridge/Bridge'
import { LIVE_EVENT } from '../DashboardViews'
import { roleLabel } from '../../bridge/station'

const STAGE_IDS = ['request', 'analyze_split', 'distribute', 'execute', 'collect', 'pull_request', 'review_merge', 'completed']

const agents = [
  {
    id: 'manager-0',
    name: 'Chief AI Engineering Manager',
    role: 'manager',
    specialty: 'Orchestration',
    status: 'working',
    model_name: 'claude-sonnet-4-5',
    is_enabled: true,
    is_emergency_stopped: false,
  },
  {
    id: 'worker-1',
    name: 'Frontend Engineer',
    role: 'worker',
    specialty: 'React + RTL',
    status: 'working',
    model_name: 'claude-sonnet-4-5',
    git_branch: 'aatm/abc-frontend',
    is_enabled: true,
    is_emergency_stopped: false,
  },
  {
    id: 'worker-2',
    name: 'Backend Engineer',
    role: 'worker',
    specialty: 'FastAPI',
    status: 'needs_human',
    is_enabled: false,
    is_emergency_stopped: true,
  },
]

const employees = {
  employees: [
    {
      id: 'worker-1',
      name: 'Frontend Engineer',
      role: 'worker',
      color: '#22D3EE',
      provider: 'anthropic-claude',
      model: 'claude-sonnet-4-5',
      profile_status: 'running',
      connector_status: 'busy',
      current_task: { id: 'task-1', title: 'Build the bridge', status: 'in_progress', progress: 40 },
      cost_usd: 0.42,
      is_enabled: true,
      is_emergency_stopped: false,
    },
    {
      id: 'worker-2',
      name: 'Backend Engineer',
      role: 'worker',
      color: '#34D399',
      provider: null,
      model: null,
      profile_status: 'crashed',
      connector_status: 'needs_human',
      current_task: null,
      cost_usd: 0.1,
      is_enabled: false,
      is_emergency_stopped: true,
    },
  ],
}

const timeline = {
  project_id: 'proj-1',
  project_name: 'AATM v2.1',
  status: 'active',
  steps: STAGE_IDS.map((id, index) => ({
    id,
    state: index < 3 ? 'done' : index === 3 ? 'active' : 'pending',
    done: index < 3 ? 1 : 0,
    total: 1,
  })),
}

const prs = [
  {
    task_id: 'task-1',
    pr_number: 14,
    pr_url: 'https://github.com/oldman-088/AATM/pull/14',
    branch_name: 'aatm/abc-frontend',
    status: 'pr_open',
    assigned_agent_id: 'worker-1',
    merge_approvals: [{ id: 'appr-merge', status: 'pending', title: 'Merge PR #14' }],
  },
]

const approvals = [
  { id: 'appr-merge', action_type: 'merge', title: 'Merge PR #14', status: 'pending', payload: { task_id: 'task-1', pr_number: 14 } },
]

const chats = [
  { id: 'm1', sender_type: 'user', content: 'أضف شاشة جسر القيادة', created_at: '2026-11-16T10:00:00Z' },
  {
    id: 'm2',
    sender_type: 'manager',
    content: 'Chief AI Engineering Manager online. All 4 Worker Agents initialized across 12 Divisions.',
    metadata_json: { i18n_key: 'managerWelcome' },
    created_at: '2026-11-16T10:00:05Z',
  },
  { id: 'm3', sender_type: 'manager', content: 'تم توزيع المهمة على worker-1', created_at: '2026-11-16T10:00:09Z' },
]

function bridgeFixtures(overrides: Record<string, any> = {}) {
  return {
    '/api/v1/agents': agents,
    '/api/v1/dashboard/employees': employees,
    '/api/v1/projects': [{ id: 'proj-1', name: 'AATM v2.1', status: 'active', created_at: '2026-11-16T09:00:00Z' }],
    '/api/v1/projects/proj-1/timeline': timeline,
    '/api/v1/reviews/pull-requests': prs,
    '/api/v1/approvals': approvals,
    '/api/v1/chat': chats,
    '/api/v1/arena/profiles': {
      gate: { mode: 'mock', enabled: true, real_enabled: false },
      profiles: [{ agent_id: 'worker-2', profile_id: 'profile-2', connector_status: 'needs_human' }],
    },
    '/api/v1/arena/connector/status': { enabled: true, real_enabled: false, mode: 'mock', code: 'mock_mode' },
    '/api/v1/code-stream': { agents: {}, pending: 0 },
    '/api/health': {
      status: 'ok',
      model_status: { status: 'CONFIGURED', configured: true, configured_providers: ['anthropic-claude'], reason: null },
    },
    ...overrides,
  }
}

async function renderBridge(language: 'ar' | 'en' = 'en', overrides: Record<string, any> = {}) {
  localStorage.setItem('aatm_language', language)
  const mock = installFetchMock(bridgeFixtures(overrides))
  const onOpenAdvanced = vi.fn()
  const view = render(
    <I18nProvider>
      <CommandBridge wsStatus="live" onOpenAdvanced={onOpenAdvanced} />
    </I18nProvider>,
  )
  await settle()
  return { ...view, ...mock, onOpenAdvanced }
}

function emitCode(payload: any) {
  act(() => {
    window.dispatchEvent(new CustomEvent(LIVE_EVENT, { detail: { type: 'agent.code', payload } }))
  })
}

function frame(overrides: Record<string, any> = {}) {
  return {
    agent_id: 'worker-1',
    task_id: 'task-1',
    path: 'frontend/src/views/Bridge.tsx',
    language: 'tsx',
    action: 'write',
    content: 'export function CommandBridge() {\n  return null\n}\n',
    ...overrides,
  }
}

function mockMatchMedia(matches: boolean, reducedMotion = false) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes('prefers-reduced-motion') ? reducedMotion : query.includes('max-width') ? matches : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as any
}

describe('Command Bridge (جسر القيادة)', () => {
  beforeEach(() => {
    localStorage.clear()
    mockMatchMedia(false)
  })

  it('renders the real crew, mission, engine and Arena state in the HUD', async () => {
    await renderBridge('ar')
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByTestId('command-bridge')).toBeTruthy()
    expect(screen.getByText('المهمة: AATM v2.1')).toBeTruthy()
    expect(screen.getByTestId('bridge-crew-chip').textContent).toContain('2/3')
    expect(screen.getByTestId('bridge-engine-chip').textContent).toContain('anthropic-claude')
    expect(screen.getByTestId('bridge-arena-chip').textContent).toContain('محاكاة')
    expect(screen.getByTestId('bridge-ws-chip').textContent).toBe('مباشر')
  })

  it('streams live code from agent.code WS events into the right station only', async () => {
    await renderBridge('en')
    expect(screen.getByTestId('bridge-screen-worker-1').textContent).toContain('Waiting for a task')

    emitCode({ frames: [frame()] })
    await waitFor(() => expect(screen.getByTestId('bridge-screen-worker-1').textContent).toContain('export function CommandBridge'))

    // The other station must keep its own state.
    expect(screen.getByTestId('bridge-screen-worker-2').textContent).toContain('Waiting for a task')
    expect(screen.getByTestId('bridge-screen-worker-1').textContent).toContain('aatm/abc-frontend')
    expect(screen.getByTestId('bridge-station-worker-1').getAttribute('data-state')).toBe('Writing code')
  })

  it('shows real pytest output frames on the station screen', async () => {
    await renderBridge('en')
    emitCode({
      frames: [
        frame({
          action: 'test',
          path: null,
          command: 'pytest -q tests/test_bridge.py',
          exit_code: 0,
          ok: true,
          output_lines: ['11 passed, 1 warning in 3.43s'],
        }),
      ],
    })
    await waitFor(() => expect(screen.getByTestId('bridge-screen-worker-1').textContent).toContain('11 passed'))
    expect(screen.getByTestId('bridge-screen-worker-1').textContent).toContain('exit 0')
    expect(screen.getByTestId('bridge-station-worker-1').getAttribute('data-state')).toBe('Running tests')
  })

  it('opens the focus view when a robot is clicked and returns with Esc', async () => {
    await renderBridge('en')
    fireEvent.click(screen.getByTestId('bridge-station-open-worker-1'))
    expect(screen.getByTestId('bridge-focus')).toBeTruthy()
    expect(screen.getByTestId('bridge-side-panel')).toBeTruthy()
    // other stations are blurred (they keep their state, just dimmed)
    expect(screen.getByTestId('bridge-station-worker-2').className).toContain('bridge-station--dimmed')

    act(() => {
      fireEvent.keyDown(window, { key: 'Escape' })
    })
    await waitFor(() => expect(screen.queryByTestId('bridge-focus')).toBeNull())
    expect(screen.getByTestId('bridge-station-worker-1')).toBeTruthy()
  })

  it('side panel shows only the controls the backend supports', async () => {
    await renderBridge('en')

    // worker-1 is healthy: pause + replace + stop, no connector resume (no needs_human profile).
    fireEvent.click(screen.getByTestId('bridge-station-open-worker-1'))
    expect(screen.getByTestId('bridge-control-pause')).toBeTruthy()
    expect(screen.getByTestId('bridge-control-replace')).toBeTruthy()
    expect(screen.getByTestId('bridge-control-stop')).toBeTruthy()
    expect(screen.queryByTestId('bridge-control-resume-session')).toBeNull()

    // worker-2 needs a human and has a real browser profile -> resume appears.
    fireEvent.click(screen.getByTestId('bridge-control-back'))
    fireEvent.click(screen.getByTestId('bridge-station-open-worker-2'))
    expect(screen.getByTestId('bridge-control-resume-session')).toBeTruthy()

    // the manager cannot be replaced (backend has no manager replacement)
    fireEvent.click(screen.getByTestId('bridge-control-back'))
    fireEvent.click(screen.getAllByLabelText(/Manager —/)[0])
    expect(screen.queryByTestId('bridge-control-replace')).toBeNull()
  })

  it('pause and emergency stop call the real endpoints', async () => {
    const { calls } = await renderBridge('en')
    fireEvent.click(screen.getByTestId('bridge-station-open-worker-1'))
    fireEvent.click(screen.getByTestId('bridge-control-pause'))
    await waitFor(() => {
      const patch = calls.find((call) => call.path === '/api/v1/agents/worker-1' && call.method === 'PATCH')
      expect(patch?.body).toMatchObject({ is_enabled: false })
    })
    fireEvent.click(screen.getByTestId('bridge-control-stop'))
    await waitFor(() => {
      const stop = calls.find((call) => call.path === '/api/v1/emergency-stop' && call.method === 'POST')
      expect(stop?.body).toMatchObject({ scope: 'employee', target_id: 'worker-1' })
    })
  })

  it('sends a real directive to the manager from the console', async () => {
    const { calls, onOpenAdvanced } = await renderBridge('ar')
    expect(screen.getByTestId('bridge-chat').textContent).toContain('أضف شاشة جسر القيادة')

    fireEvent.change(screen.getByTestId('bridge-directive-input'), { target: { value: 'أضف اختباراً للجسر' } })
    fireEvent.click(screen.getByTestId('bridge-send'))

    await waitFor(() => {
      const sent = calls.find((call) => call.path === '/api/v1/chat' && call.method === 'POST')
      expect(sent?.body).toMatchObject({ content: 'أضف اختباراً للجسر', project_id: 'proj-1' })
    })
    // the advanced menu is reachable from the HUD
    fireEvent.click(screen.getByTestId('bridge-advanced'))
    expect(onOpenAdvanced).toHaveBeenCalledWith('dashboard')
  })

  it('maps the 8 holo-table lights to the real mission stages', async () => {
    await renderBridge('en')
    expect(screen.getByTestId('bridge-light-request').getAttribute('data-state')).toBe('done')
    expect(screen.getByTestId('bridge-light-execute').getAttribute('data-state')).toBe('active')
    expect(screen.getByTestId('bridge-light-completed').getAttribute('data-state')).toBe('pending')
    expect(screen.getByTestId('bridge-light-execute').getAttribute('title')).toBe('4. Execution in isolated sessions')

    const cells = screen.getByTestId('console-stages').querySelectorAll('[data-state]')
    expect(cells).toHaveLength(8)
    expect(cells[3].getAttribute('data-state')).toBe('active')
  })

  it('shows the approvals panel and decides a pending approval', async () => {
    const { calls } = await renderBridge('en')
    expect(screen.getByTestId('bridge-approvals-button').textContent).toContain('(1)')
    expect(screen.getByTestId('bridge-status-line').textContent).toContain('PR #14')
    fireEvent.click(screen.getByTestId('bridge-approvals-button'))
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => {
      const decision = calls.find((call) => call.path === '/api/v1/approvals/appr-merge/decide')
      expect(decision?.body).toMatchObject({ decision: 'approved' })
    })
  })

  it('renders the 3-step onboarding when no engine or project is configured', async () => {
    await renderBridge('ar', {
      '/api/health': { status: 'ok', model_status: { status: 'NO_MODEL_CONFIGURED', configured: false, configured_providers: [] } },
      '/api/v1/projects': [],
      '/api/v1/chat': [],
    })
    const onboarding = await screen.findByTestId('bridge-onboarding')
    expect(onboarding.textContent).toContain('اربط محرك الذكاء الاصطناعي')
    expect(onboarding.textContent).toContain('اربط مستودع GitHub')
    expect(onboarding.textContent).toContain('أصدر أول أمر للمدير')
    expect(screen.getByTestId('bridge-engine-chip').textContent).toContain('غير متوفر')
  })

  it('switches to the mobile card layout below 900px', async () => {
    mockMatchMedia(true)
    await renderBridge('en')
    expect(screen.getByTestId('bridge-mobile')).toBeTruthy()
    expect(screen.getByTestId('bridge-mobile-manager')).toBeTruthy()
    const card = screen.getByTestId('bridge-mobile-station-worker-1')
    expect(card.textContent).toContain('Robot-1 · Frontend')
    expect(card.getAttribute('title')).toBe('Frontend Engineer')
    expect(screen.queryByTestId('bridge-station-worker-1')).toBeNull()

    fireEvent.click(screen.getByTestId('bridge-mobile-station-worker-1'))
    expect(screen.getByTestId('bridge-focus')).toBeTruthy()
    fireEvent.click(screen.getByTestId('bridge-control-back'))
    await waitFor(() => expect(screen.queryByTestId('bridge-focus')).toBeNull())
  })

  it('shows short seat names on the bridge and keeps the long seeded name in the tooltip', async () => {
    await renderBridge('ar')
    const station = screen.getByTestId('bridge-station-worker-1')
    expect(station.textContent).toContain('روبوت-1')
    expect(station.textContent).toContain('الواجهة')
    expect(station.textContent).not.toContain('Frontend Engineer')
    expect(screen.getByTestId('bridge-station-open-worker-1').getAttribute('title')).toBe('Frontend Engineer')

    // the manager seat is titled "المدير" only
    expect(screen.getByTestId('bridge-manager').textContent).toContain('المدير')
    expect(screen.getByTestId('bridge-manager').textContent).toContain('قائد الطاقم')

  })

  it('maps the seeded role templates to short localised seat roles', async () => {
    const { ar } = await import('../../i18n/ar')
    const seeded = [
      ['backend_developer', 'Worker 1 — Backend Engineer', 'الخادم'],
      ['frontend_developer', 'Worker 2 — Frontend Engineer', 'الواجهة'],
      ['ai_engineer', 'Worker 3 — AI Provider & Browser Engineer', 'المتصفح'],
      ['qa_engineer', 'Worker 4 — Orchestration & QA Engineer', 'الاختبارات والتنسيق'],
    ] as const
    for (const [role_template, name, expected] of seeded) {
      expect(roleLabel({ role: 'worker', role_template, name, specialty: '' }, ar as any)).toBe(expected)
    }
    expect(roleLabel({ role: 'manager', role_template: 'ceo_manager' }, ar as any)).toBe(ar.bridge.managerTag)
    expect(roleLabel({ role: 'worker', role_template: 'mystery', name: 'x' }, ar as any)).toBe(ar.bridge.roleOther)
  })

  it('renders 8 compact numbered stage cells with the stage name as tooltip', async () => {
    await renderBridge('en')
    const cells = screen.getByTestId('console-stages').querySelectorAll('[data-testid^="bridge-stage-cell-"]')
    expect(cells).toHaveLength(8)
    expect(Array.from(cells).map((cell) => cell.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8'])
    expect(cells[3].getAttribute('title')).toBe('4. Execution in isolated sessions')
    expect(cells[3].getAttribute('data-state')).toBe('active')
    // no long labels are printed in the console, so nothing can be clipped
    expect(screen.getByTestId('console-stages').textContent).toBe('12345678')
  })

  it('keeps the onboarding strip inside the console and hides it in the focus view', async () => {
    await renderBridge('ar', {
      '/api/health': { status: 'ok', model_status: { status: 'NO_MODEL_CONFIGURED', configured: false, configured_providers: [] } },
      '/api/v1/projects': [],
      '/api/v1/chat': [],
    })
    const banner = await screen.findByTestId('bridge-onboarding')
    const consoleEl = screen.getByTestId('bridge-console')
    expect(consoleEl.contains(banner)).toBe(true)
    // it is a slim strip, not a card over the scene
    expect(banner.className).toContain('bridge-onboard')
    expect(banner.textContent).toContain('لنبدأ')

    fireEvent.click(screen.getByTestId('bridge-station-open-worker-1'))
    expect(screen.getByTestId('bridge-focus')).toBeTruthy()
    expect(screen.queryByTestId('bridge-onboarding')).toBeNull()
  })

  it('shows the manager welcome in the operator language', async () => {
    await renderBridge('ar')
    expect(screen.getByTestId('bridge-chat').textContent).toContain('مدير فريق وكلاء الذكاء الاصطناعي جاهز')
    expect(screen.getByTestId('bridge-chat').textContent).not.toContain('Chief AI Engineering Manager online')
  })

  it('localises the bridge (Arabic RTL and English LTR)', async () => {
    const arabic = await renderBridge('ar')
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByText('مراحل المهمة', { exact: false })).toBeTruthy()
    expect(screen.getByTestId('bridge-approvals-button').textContent).toContain('الموافقات')
    arabic.unmount()

    await renderBridge('en')
    await waitFor(() => expect(screen.getAllByText(/Mission stages/).length).toBeGreaterThan(0))
    expect(document.documentElement.dir).toBe('ltr')
    expect(screen.getByTestId('bridge-approvals-button').textContent).toContain('Approvals')
  })

  it('respects prefers-reduced-motion and pauses animations on a hidden tab', async () => {
    mockMatchMedia(false, true)
    const reduced = await renderBridge('en')
    expect(screen.getByTestId('command-bridge').getAttribute('data-reduced-motion')).toBe('true')
    reduced.unmount()

    mockMatchMedia(false, false)
    await renderBridge('en')
    expect(screen.getByTestId('command-bridge').getAttribute('data-reduced-motion')).toBe('false')

    act(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(screen.getByTestId('command-bridge').className).toContain('is-hidden'))
    act(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(screen.getByTestId('command-bridge').className).not.toContain('is-hidden'))
  })
})
