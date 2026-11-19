import { test, expect, type Page } from '@playwright/test'

/**
 * Command Bridge layout proof + screenshots (owner request):
 *
 *  - 1440×900 Arabic: the four workstations, the manager, the console, no horizontal overflow.
 *  - 1440×900 Arabic focus view: the side panel sits on the RIGHT in RTL.
 *  - 390×844 Arabic: the card list is visible (it used to render off-screen) and nothing overflows.
 *
 * The API is mocked with fixtures so the test is deterministic and needs no backend; the real data
 * path is covered by the backend suites and the vitest component tests.
 */

const STAGES = ['request', 'analyze_split', 'distribute', 'execute', 'collect', 'pull_request', 'review_merge', 'completed']

const agents = [
  { id: 'manager-0', name: 'Chief AI Engineering Manager', role: 'manager', role_template: 'ceo_manager', specialty: 'Architecture, Planning, Coordination', status: 'working', is_enabled: true },
  { id: 'worker-1', name: 'Worker 1 — Backend Engineer', role: 'worker', role_template: 'backend_developer', specialty: 'FastAPI, PostgreSQL', status: 'working', is_enabled: true, git_branch: 'aatm/e2e-backend' },
  { id: 'worker-2', name: 'Worker 2 — Frontend Engineer', role: 'worker', role_template: 'frontend_developer', specialty: 'React, RTL', status: 'working', is_enabled: true },
  { id: 'worker-3', name: 'Worker 3 — AI Provider & Browser Engineer', role: 'worker', role_template: 'ai_engineer', specialty: 'Arena bridge', status: 'idle', is_enabled: true },
  { id: 'worker-4', name: 'Worker 4 — Orchestration & QA Engineer', role: 'worker', role_template: 'qa_engineer', specialty: 'QA engine', status: 'idle', is_enabled: true },
]

const employee = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  name: agents.find((a) => a.id === id)?.name,
  role: 'worker',
  color: '#22D3EE',
  provider: 'anthropic-claude',
  model: 'claude-sonnet-4-5',
  profile_status: 'running',
  connector_status: 'busy',
  current_task: { id: `task-${id}`, title: 'Ship the bridge', status: 'in_progress', progress: 62 },
  cost_usd: 0.2137,
  is_enabled: true,
  is_emergency_stopped: false,
  ...extra,
})

const sampleCode = {
  agent_id: 'worker-1',
  task_id: 'task-worker-1',
  path: 'backend/app/runtime/code_stream.py',
  language: 'python',
  action: 'write',
  content: [
    'def build_write_frame(agent_id: str, path: str, content: str):',
    '    """Turn one agent write into a bounded, redacted code frame."""',
    '    if not is_streamable_path(path):',
    '        return None',
    '    lines = content.splitlines()',
    '    return {',
    "        'agent_id': agent_id,",
    "        'action': 'write',",
    "        'path': path,",
    "        'lines': len(lines),",
    '    }',
  ].join('\n'),
}

const routes: Array<[string, unknown]> = [
  ['/api/v1/auth/me', { id: 'u1', email: 'admin@aatm.local', full_name: 'Operator', role: 'admin', language: 'ar', theme: 'dark', must_change_password: false }],
  ['/api/v1/agents', agents],
  ['/api/v1/dashboard/employees', { employees: [employee('worker-1'), employee('worker-2'), employee('worker-3'), employee('worker-4')] }],
  ['/api/v1/dashboard/home', { architecture: [], counters: { active_employees: 4, total_employees: 5, tasks_by_status: {}, open_prs: 4, cost_today_usd: 1.42, daily_budget_usd: 20, needs_human: 0 } }],
  ['/api/v1/projects', [{ id: 'proj-1', name: 'AATM v3.2 — Command Bridge', status: 'active', created_at: '2026-11-16T06:00:00Z' }]],
  ['/api/v1/projects/proj-1/timeline', { project_id: 'proj-1', project_name: 'AATM v3.2 — Command Bridge', status: 'active', steps: STAGES.map((id, index) => ({ id, state: index < 4 ? 'done' : index === 4 ? 'active' : 'pending', done: index < 4 ? 1 : 0, total: 1 })) }],
  ['/api/v1/reviews/pull-requests', [{ task_id: 'task-worker-1', pr_number: 10, pr_url: 'https://github.com/oldman-088/AATM/pull/10', branch_name: 'aatm/e2e-backend', status: 'pr_open', assigned_agent_id: 'worker-1', merge_approvals: [{ id: 'appr-merge', status: 'pending', title: 'Merge PR #10' }] }]],
  ['/api/v1/approvals', [{ id: 'appr-merge', action_type: 'merge', title: 'Merge PR #10', status: 'pending' }]],
  ['/api/v1/chat', [
    { id: 'm1', sender_type: 'user', content: 'ابنِ جسر القيادة', created_at: '2026-11-16T06:01:00Z' },
    { id: 'm2', sender_type: 'manager', content: 'Chief AI Engineering Manager online. All 4 Worker Agents initialized across 12 Divisions.', metadata: { i18n_key: 'managerWelcome' }, created_at: '2026-11-16T06:01:05Z' },
    { id: 'm3', sender_type: 'manager', content: 'تم توزيع المهام على أربعة وكلاء، وكل واحد يعمل في مساحة مستقلة.', created_at: '2026-11-16T06:02:00Z' },
  ]],
  ['/api/v1/arena/connector/status', { mode: 'mock', enabled: true, real_enabled: false }],
  ['/api/v1/arena/profiles', { gate: { mode: 'mock', enabled: true, real_enabled: false }, profiles: [] }],
  ['/api/v1/code-stream', { agents: { 'worker-1': [sampleCode], 'worker-2': [{ ...sampleCode, agent_id: 'worker-2', path: 'frontend/src/bridge/Bridge.tsx', language: 'tsx' }] }, pending: 0 }],
  ['/api/health', { status: 'ok', model_status: { status: 'CONFIGURED', configured: true, configured_providers: ['anthropic-claude'], reason: null }, sandbox: { status: 'READY', available: true } }],
]

async function mockApi(page: Page, overrides: Record<string, unknown> = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('aatm_jwt_token', 'e2e-jwt')
    localStorage.setItem('aatm_language', 'ar')
  })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const match = routes.find(([key]) => path === key || path.startsWith(`${key}/`))
    const override = Object.keys(overrides).find((key) => path === key)
    // Unknown collections answer `[]`, unknown objects `{}` — the shell loads ~20 endpoints and
    // only the ones listed above matter for the bridge.
    const body = override
      ? overrides[override]
      : match
        ? match[1]
        : path.includes('/status') || path.startsWith('/api/health')
          ? {}
          : []
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  // The live socket is not needed for layout; the HUD shows the real "offline" state.
  await page.route('**/ws/**', (route) => route.abort())
}

test('wide bridge renders every station and never overflows', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByTestId('command-bridge')).toBeVisible()
  await expect(page.getByTestId('bridge-station-worker-1')).toBeVisible()
  await expect(page.getByTestId('bridge-station-worker-4')).toBeVisible()
  await expect(page.getByTestId('bridge-manager')).toBeVisible()
  await expect(page.getByTestId('bridge-console')).toBeVisible()
  await expect(page.getByTestId('command-bridge')).toContainText('روبوت-1')
  await expect(page.getByTestId('command-bridge')).not.toContainText('Worker 1 — Backend Engineer')

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)

  await page.screenshot({ path: '../docs/design/bridge-wide-1440x900-ar.png' })
})

test('focus view puts the side panel on the right in RTL', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await page.getByTestId('bridge-station-open-worker-1').click()
  await expect(page.getByTestId('bridge-focus')).toBeVisible()
  const panel = page.getByTestId('bridge-side-panel')
  await expect(panel).toBeVisible()
  const box = await panel.boundingBox()
  expect(box!.x).toBeGreaterThan(1440 / 2)
  await expect(panel).toContainText('الخادم')
  await expect(panel).toContainText('Worker 1 — Backend Engineer')
  await expect(page.getByTestId('bridge-onboarding')).toHaveCount(0)

  await page.screenshot({ path: '../docs/design/bridge-focus-1440x900-ar.png' })

  await page.keyboard.press('Escape')
  await expect(page.getByTestId('bridge-focus')).toHaveCount(0)
})

test('390×844 shows the station cards on screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByTestId('bridge-mobile')).toBeVisible()

  const first = page.getByTestId('bridge-mobile-manager')
  await expect(first).toBeVisible()
  const box = await first.boundingBox()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(390)
  expect(box!.y).toBeGreaterThan(0)
  expect(box!.height).toBeGreaterThan(40)

  for (const id of ['worker-1', 'worker-2', 'worker-3', 'worker-4']) {
    await expect(page.getByTestId(`bridge-mobile-station-${id}`)).toBeVisible()
  }

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)

  await page.screenshot({ path: '../docs/design/bridge-mobile-390x844-ar.png' })
  await page.getByTestId('bridge-mobile-station-worker-1').click()
  await expect(page.getByTestId('bridge-focus')).toBeVisible()
  await page.screenshot({ path: '../docs/design/bridge-mobile-focus-390x844-ar.png' })
})

test('never badges demo mode or renders the scripted feed, even when the backend reports it', async ({ page }) => {
  // The demo code-stream and its badge are display-fake surfaces and are removed.
  // Even if the backend still advertises demo_mode, the bridge must not render it.
  await mockApi(page, {
    '/api/health': {
      status: 'ok',
      demo_mode: true,
      model_status: { status: 'CONFIGURED', configured: true, configured_providers: ['demo'], reason: null },
      sandbox: { status: 'READY', available: true },
    },
    '/api/v1/demo/code-stream': {
      demo: true,
      tick_seconds: 3,
      agents: {
        'worker-1': [
          {
            agent_id: 'worker-1',
            demo: true,
            action: 'write',
            path: 'demo/stream.py',
            language: 'python',
            content: 'def demo_frame():\n    return True\n',
          },
        ],
      },
    },
  })
  await page.goto('/')
  await expect(page.getByTestId('command-bridge')).toBeVisible()
  await expect(page.getByTestId('bridge-demo-chip')).toHaveCount(0)
  await expect(page.getByTestId('bridge-screen-worker-1')).not.toContainText('demo_frame')
})
