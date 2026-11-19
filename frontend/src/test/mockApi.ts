import { act } from '@testing-library/react'
import { vi } from 'vitest'

export interface RecordedCall {
  path: string
  method: string
  body?: any
  authorization?: string
}

export type RouteValue = any | ((path: string, init: RequestInit) => any)

/**
 * Minimal API-ish fixtures that mirror the FastAPI payload shapes consumed by App.tsx / views.
 * Any endpoint not listed still resolves to `{}` so the shell never crashes on unmounted panels.
 */
export const fixtures: Record<string, RouteValue> = {
  '/api/v1/scaling/status': {
    mode: 'approval',
    default_workers: 4,
    extra_workers: 2,
    total_workers: 6,
    max_workers: 25,
    ready_backlog: 5,
    pending_approval_id: null,
    database_backend: 'sqlite',
    sqlite_warning: true,
    queue: { configured_backend: 'memory' },
    coordination: { backend: 'memory' },
  },
  '/api/v1/dashboard/overview': {
    kpis: {
      active_workers: 2,
      idle_workers: 1,
      total_agents: 4,
      total_tasks: 10,
      completed_tasks: 8,
      completion_rate: 87,
      healthy_providers: 2,
      total_providers: 2,
      running_browsers: 1,
      total_browsers: 2,
      pending_approvals: 1,
    },
    divisions: [{ code: 'D03', name: 'Frontend', progress_percent: 42, completed_tasks: 4, total_tasks: 10 }],
    resources: {
      cpu_count: 8,
      cpu_percent: 21,
      ram_used_gb: 4,
      ram_total_gb: 16,
      ram_percent: 25,
      disk_free_gb: 120,
      disk_percent: 40,
    },
    recommendations: ['Scale workers before the merge window'],
    recent_events: [],
    sandbox: {
      status: 'READY',
      available: true,
      mode: 'docker',
      image: 'aatm-sandbox:latest',
      reason: null,
    },
    model_status: {
      status: 'CONFIGURED',
      configured: true,
      configured_providers: ['anthropic-claude'],
      diagnostic_only_providers: [],
      reason: null,
    },
  },
  '/api/v1/agents': [
    {
      id: 'manager-0',
      name: 'Chief AI Engineering Manager',
      role: 'manager',
      specialty: 'Orchestration',
      status: 'working',
      divisions: ['D00'],
      model_name: 'arena-agent',
      git_branch: 'main',
      tasks_completed: 12,
      autonomy_mode: 'highly_autonomous',
      performance_score: 96,
      is_emergency_stopped: false,
    },
    {
      id: 'worker-1',
      name: 'Frontend Engineer',
      role: 'worker',
      role_template: 'frontend_developer',
      specialty: 'React + RTL',
      status: 'idle',
      divisions: ['D03'],
      model_name: 'arena-agent',
      git_branch: 'arena/51325356-aatm',
      tasks_completed: 7,
      autonomy_mode: 'semi_autonomous',
      performance_score: 88,
      is_emergency_stopped: false,
    },
  ],
  '/api/v1/employees/templates': [
    { id: 'backend_developer', title: 'Backend Developer' },
    { id: 'frontend_developer', title: 'Frontend Developer' },
  ],
  '/api/v1/employees': (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body || '{}'))
    return { id: 'worker-9', name: body.name, role_template_id: body.role_template_id, workspace_path: 'workspaces/worker-9' }
  },
  '/api/v1/tasks': [],
  '/api/v1/messages': [],
  '/api/v1/projects': [{ id: 'proj-1', name: 'AATM v2.1', description: 'Enterprise AI OS', status: 'active' }],
  '/api/v1/chat': [],
  '/api/v1/browsers': [],
  '/api/v1/browsers/capabilities': { chromium_installed: true, engine_mode: 'playwright' },
  '/api/v1/browsers/engines': { playwright: { status: 'ready' }, chromium_cdp: { status: 'ready' } },
  '/api/v1/userscripts': [],
  '/api/v1/providers': [],
  '/api/v1/providers/wizard/presets': {
    model_status: { status: 'CONFIGURED', configured: true, configured_providers: ['anthropic-claude'], diagnostic_only_providers: [] },
    options: [
      {
        id: 'anthropic-claude',
        provider_type: 'anthropic_claude',
        title: 'Anthropic Claude API',
        endpoint_url: 'https://api.anthropic.com/v1',
        credential_ref: 'env:ANTHROPIC_API_KEY',
        default_model: 'claude-sonnet-4-5',
      },
      {
        id: 'openai-compatible',
        provider_type: 'openai_compatible',
        title: 'OpenAI-Compatible',
        endpoint_url: 'https://openrouter.ai/api/v1',
        credential_ref: 'env:OPENROUTER_API_KEY',
        default_model: 'qwen2.5-coder:7b',
        sub_presets: [
          { id: 'ollama', label: 'Ollama (Local)', endpoint_url: 'https://ollama.example.test/v1', credential_ref: 'env:OLLAMA_API_KEY', default_model: 'qwen2.5-coder:7b' },
        ],
      },
      {
        id: 'arena-local-bridge',
        provider_type: 'arena_local_bridge',
        title: 'Arena Local Bridge',
        endpoint_url: '',
        credential_ref: 'env:ARENA_BRIDGE_TOKEN',
        default_model: 'arena-bridge-default',
      },
    ],
  },
  '/api/v1/providers/wizard/test-connection': (_path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body || '{}'))
    return { success: true, provider_type: body.provider_type, model: body.model, latency_ms: 42.5, detail: `Connected to ${body.model} in 42.5 ms` }
  },
  '/api/v1/providers/wizard/configure': (_path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body || '{}'))
    return {
      provider: { id: body.provider_type === 'anthropic_claude' ? 'anthropic-claude' : 'openai-compatible', name: body.provider_type, default_model: body.default_model },
      model_status: { status: 'CONFIGURED', configured: true },
    }
  },
  '/api/v1/providers/usage': {
    summary: { total_input_tokens: 1200, total_output_tokens: 800, total_estimated_cost_usd: 0.02, pricing_known: true, has_unknown_pricing: false },
    logs: [],
  },
  '/api/v1/settings': {
    budgets: {
      value: { daily_limit_usd: 25, monthly_limit_usd: 400, require_approval_above_usd: 5 },
      description: 'AI provider spend controls and approval thresholds',
      updated_at: '2026-11-01T00:00:00Z',
    },
  },
  '/api/v1/github/branches': (_path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body || '{}'))
    return { ok: true, allowed: true, repo: body.repo, branch_name: body.branch_name, approval_id: body.approval_id }
  },
  '/api/v1/github/prs': (path: string, init: RequestInit) => {
    if ((init.method || 'GET').toUpperCase() === 'POST') {
      const body = JSON.parse(String(init.body || '{}'))
      return {
        pr_number: 12,
        url: 'https://github.com/oldman-088/AATM/pull/12',
        approval_id: body.approval_id,
        title: body.title,
      }
    }
    return []
  },
  '/api/v1/github/issues': (path: string, init: RequestInit) => {
    if ((init.method || 'GET').toUpperCase() === 'POST') {
      const body = JSON.parse(String(init.body || '{}'))
      return {
        issue_number: 34,
        url: 'https://github.com/oldman-088/AATM/issues/34',
        approval_id: body.approval_id,
        title: body.title,
      }
    }
    return []
  },

  '/api/v1/employees/skill-gaps': { covered_skills: ['react'], missing_skills: ['python'], coverage_ratio: 0.5 },
  '/api/v1/employees/provision-specialists': { provisioned_count: 1, provisioned_employees: [{ id: 'worker-9' }], after: { covered_skills: ['react', 'python'], missing_skills: [], coverage_ratio: 1 } },
  '/api/v1/providers/sessions/recover': { recovered_count: 2, sessions: [] },
  '/api/v1/git/worktrees': (path: string, init: RequestInit) => ({ ok: true, allowed: true, agent_id: path.split('/').pop(), method: init.method }),
  '/api/v1/browsers/profile-1/workflow': { engine: 'automa', status: 'completed', steps_completed: 2, steps_total: 2, trace: [
    { step_index: 0, type: 'extract_text', status: 'completed', text: 'redacted result', redacted_count: 0, engine_mode: 'playwright' },
    { step_index: 1, type: 'navigate', status: 'completed', engine_mode: 'playwright' },
  ] },
  '/api/v1/github/prs/12/comments': { ok: true, allowed: true, url: 'https://github.com/oldman-088/AATM/pull/12#issuecomment-1' },
  '/api/v1/accounts': [],
  '/api/v1/sessions': [],
  '/api/v1/devices': (path: string, init: RequestInit) => {
    if (path.includes('/revoke')) {
      return { id: path.split('/devices/')[1]?.split('/')[0], token_version: 1, device_token: 'aatm_dev_rotated_token_9f2c' }
    }
    if ((init.method || 'GET').toUpperCase() === 'POST' && path === '/api/v1/devices') {
      const body = JSON.parse(String(init.body || '{}'))
      return { device: { id: body.id, name: body.name, status: 'online' }, device_token: 'aatm_dev_bootstrap_token', token_hardened: true, warning: null }
    }
    return []
  },
  '/api/v1/devices/heartbeat': { status: 'online' },
  '/api/v1/git/status': {
    repository: { current_branch: 'arena/51325356-aatm', recent_commits: ['83c14f4 chore: snapshot'] },
    workflows: [],
  },
  '/api/v1/qa/summary': {
    security_scan: { status: 'passed', files_scanned: 42, findings: [] },
    intelligence: { recommendations: ['No blocking findings'] },
  },
  '/api/v1/governance/security-posture': {
    environment: 'development',
    sandbox: { mode: 'process', status: 'READY' },
    users_requiring_password_change: 0,
    pending_approvals: 0,
    security_warnings_count: 1,
    active_devices: 1,
    offline_or_degraded_devices: 0,
    headers_enforced: [
      'X-Content-Type-Options',
      'X-Frame-Options',
      'Referrer-Policy',
      'Permissions-Policy',
      'Content-Security-Policy',
    ],
  },
  '/api/v1/auth/logout': { ok: true },
  '/api/v1/approvals': (path: string, init: RequestInit) => {
    if ((init.method || 'GET').toUpperCase() === 'POST') {
      const body = JSON.parse(String(init.body || '{}'))
      return { id: 'appr-1', status: 'pending', action_type: body.action_type, title: body.title }
    }
    return []
  },
  '/api/v1/audit': [],
  '/api/v1/notifications': [],
  '/api/v1/memory': [],
  '/api/v1/auth/me': {
    id: 'director-1',
    email: 'director@aatm.ai',
    full_name: 'Chief Engineering Director',
    role: 'director',
    language: 'en',
    theme: 'dark',
    must_change_password: false,
  },
  '/api/v1/auth/login': {
    access_token: 'test-jwt-token',
    token_type: 'bearer',
    user: {
      id: 'director-1',
      email: 'director@aatm.ai',
      full_name: 'Chief Engineering Director',
      role: 'director',
      language: 'en',
      theme: 'dark',
      must_change_password: false,
    },
  },
  '/api/v1/auth/change-password': {
    ok: true,
    access_token: 'dummy-rotated-jwt-token',
    token_type: 'bearer',
    user: {
      id: 'director-1',
      email: 'director@aatm.ai',
      full_name: 'Chief Engineering Director',
      role: 'director',
      language: 'en',
      theme: 'dark',
      must_change_password: false,
    },
  },
  '/api/v1/emergency-stop': { scope: 'global', active: true },
  '/api/v1/emergency-stop/clear': { scope: 'global', active: false },
  '/api/v1/sdlc/execute': (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body || '{}'))
    return {
      project_name: body.project_name,
      team_created: ['worker-1', 'worker-2'],
      pytest_result: { exit_code: 0 },
    }
  },
  '/api/health': {
    status: 'ok',
    service: 'aatm-backend',
    version: '3.5.0',
    environment: 'development',
    sandbox: { status: 'READY', available: true, mode: 'process', image: 'aatm-sandbox:latest', reason: null },
    model_status: {
      status: 'CONFIGURED',
      configured: true,
      configured_providers: ['anthropic-claude'],
      diagnostic_only_providers: [],
      reason: null,
    },
  },
  '/api/v1/dashboard/employees': { employees: [] },
  '/api/v1/dashboard/home': {
    architecture: [{ id: 'dashboard', count: null }],
    counters: { active_employees: 1, total_employees: 2, tasks_by_status: {}, open_prs: 0, cost_today_usd: 0, daily_budget_usd: 20, needs_human: 0 },
  },
  '/api/v1/code-stream': { agents: {}, pending: 0 },
  '/api/v1/arena/connector/status': {
    enabled: false,
    real_enabled: false,
    status_label: 'disabled',
    bridge: { ok: false, code: 'not_enabled' },
  },
  '/api/v1/arena/profiles': [],
  '/api/v1/security/sanitize': { injection_detected: true, quarantined_text: '[QUARANTINED]' },
  '/api/v1/workspaces/worker-1/files': { files: [{ path: 'hello_aatm.py', size_bytes: 44 }] },
  '/api/v1/workspaces/worker-1/exec': { exit_code: 0, stdout: 'AATM isolated workspace verified' },
}

function jsonResponse(data: any, ok = true, status = 200) {
  return { ok, status, json: async () => data }
}

/**
 * Installs a URL-routing fetch mock. Returns every call recorded so tests can assert on
 * endpoint + method + JWT header (used to prove mutations go through `apiFetch`).
 */
export function installFetchMock(overrides: Record<string, RouteValue> = {}) {
  const calls: RecordedCall[] = []
  const routes = { ...fixtures, ...overrides }
  const keys = Object.keys(routes).sort((a, b) => b.length - a.length)

  const mock = vi.fn(async (input: any, init: RequestInit = {}) => {
    const path = String(input)
    const method = (init.method || 'GET').toUpperCase()
    const headers = (init.headers || {}) as Record<string, string>
    let body: any
    try {
      body = init.body ? JSON.parse(String(init.body)) : undefined
    } catch {
      body = init.body
    }
    calls.push({ path, method, body, authorization: headers.Authorization })

    const key = keys.find((k) => path === k || path.startsWith(`${k}?`) || path.startsWith(`${k}/`))
    if (!key) return jsonResponse({})
    const value = routes[key]
    const data = typeof value === 'function' ? value(path, init) : value
    return jsonResponse(data)
  })

  ;(globalThis as any).fetch = mock
  return { calls, mock, callsTo: (suffix: string) => calls.filter((c) => c.path.endsWith(suffix)) }
}

export class MockWebSocket {
  static instances: MockWebSocket[] = []
  url: string
  sent: string[] = []
  onopen: ((ev: any) => void) | null = null
  onmessage: ((ev: any) => void) | null = null
  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {}
}

/**
 * Flushes pending promise chains (telemetry loaders, WS refreshes) inside `act` so React
 * does not warn about state updates that land after a test's assertions complete.
 */
export async function settle() {
  await act(async () => {
    await Promise.resolve()
    await new Promise((resolve) => setTimeout(resolve, 0))
    await Promise.resolve()
  })
}

export function installWebSocketMock() {
  MockWebSocket.instances = []
  ;(globalThis as any).WebSocket = MockWebSocket
  return MockWebSocket
}
