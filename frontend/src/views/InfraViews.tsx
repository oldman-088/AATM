import React, { useEffect, useState } from 'react'
import { Brain, Cpu, FolderGit2, Globe, Monitor, Play, RefreshCw, ShieldAlert, Square, Sparkles, Wallet } from 'lucide-react'
import { Badge, Button, Card, Input, Select } from '../components/ui'
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

type WorkflowStep = { type: string; url?: string; selector?: string; value?: string; ms?: number }

/**
 * E28: a project must name the GitHub repository the team works in.
 * Accepts `https://github.com/owner/name(.git)` and `git@github.com:owner/name(.git)`; anything
 * else (a bare path, another host, a single segment) is rejected before the request is sent.
 */
export function isGitHubRepoUrl(value: string): boolean {
  const raw = String(value || '').trim()
  if (!raw || /\s/.test(raw)) return false
  const match = raw.match(/^(?:https?:\/\/)?(?:www\.)?github\.com[/:]([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/)
    || raw.match(/^git@github\.com:([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?$/)
  if (!match) return false
  return !match[1].includes('..') && !match[2].includes('..')
}

export function VisualWorkflowBuilder({ browsers }: { browsers: any[] }) {
  const { t } = useI18n()
  const [profile, setProfile] = useState('')
  const [engine, setEngine] = useState('automa')
  const [steps, setSteps] = useState<WorkflowStep[]>([])
  const [result, setResult] = useState<any>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const selected = browsers.some((b) => b.id === profile) ? profile : ''
  const types = [
    ['navigate', t.enterprise.navigateStep], ['click_element', t.enterprise.clickStep],
    ['fill_input', t.enterprise.fillStep], ['extract_text', t.enterprise.extractStep],
    ['capture_snapshot', t.enterprise.snapshotStep], ['wait', t.enterprise.waitStep],
  ]
  const update = (index: number, patch: Partial<WorkflowStep>) => setSteps((old) => old.map((step, i) => i === index ? { ...step, ...patch } : step))
  const move = (index: number, delta: number) => setSteps((old) => {
    const next = [...old]; const other = index + delta
    if (other < 0 || other >= next.length) return old
    ;[next[index], next[other]] = [next[other], next[index]]
    return next
  })
  const run = async () => {
    setRunning(true); setError(''); setResult(null)
    try {
      const payload = steps.map((step) => {
        if (step.type === 'navigate') return { type: step.type, url: step.url || '' }
        if (step.type === 'fill_input') return { type: step.type, selector: step.selector || '', value: step.value || '' }
        if (step.type === 'click_element' || step.type === 'extract_text') return { type: step.type, selector: step.selector || '' }
        if (step.type === 'wait') return { type: step.type, ms: step.ms || 0 }
        return { type: step.type }
      })
      const res = await apiFetch<any>(`/browsers/${encodeURIComponent(selected)}/workflow`, { method: 'POST', body: JSON.stringify({ engine, steps: payload }) })
      setResult(res)
    } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setRunning(false) }
  }
  return (
    <Card className="space-y-3">
      <h3 className="text-sm font-semibold text-white">{t.enterprise.workflowBuilder}</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        <Select aria-label={t.enterprise.workflowProfile} value={selected} onChange={(e) => setProfile(e.target.value)}>
          <option value="">{browsers.length ? t.enterprise.noSelection : t.enterprise.workflowNoProfiles}</option>
          {browsers.map((b) => <option key={b.id} value={b.id}>{b.name || b.id}</option>)}
        </Select>
        <Select aria-label={t.enterprise.workflowEngine} value={engine} onChange={(e) => setEngine(e.target.value)}>
          {['automa', 'browser_use', 'openbrowser'].map((name) => <option key={name} value={name}>{name}</option>)}
        </Select>
      </div>
      {steps.map((step, index) => <div key={index} className="space-y-2 rounded border border-slate-700 p-2">
        <div className="flex flex-wrap gap-2">
          <Select aria-label={`${t.enterprise.workflowStep} ${index + 1}`} value={step.type} onChange={(e) => update(index, { type: e.target.value })}>
            {types.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
          <Button size="sm" variant="secondary" aria-label={`${t.enterprise.workflowUp} ${index + 1}`} disabled={index === 0} onClick={() => move(index, -1)}>{t.enterprise.workflowUp}</Button>
          <Button size="sm" variant="secondary" aria-label={`${t.enterprise.workflowDown} ${index + 1}`} disabled={index === steps.length - 1} onClick={() => move(index, 1)}>{t.enterprise.workflowDown}</Button>
          <Button size="sm" variant="danger" aria-label={`${t.enterprise.workflowRemove} ${index + 1}`} onClick={() => setSteps((old) => old.filter((_, i) => i !== index))}>{t.enterprise.workflowRemove}</Button>
        </div>
        {step.type === 'navigate' && <Input aria-label={`${t.enterprise.workflowUrl} ${index + 1}`} value={step.url || ''} onChange={(e) => update(index, { url: e.target.value })} />}
        {['click_element', 'fill_input', 'extract_text'].includes(step.type) && <Input aria-label={`${t.enterprise.workflowSelector} ${index + 1}`} value={step.selector || ''} onChange={(e) => update(index, { selector: e.target.value })} />}
        {step.type === 'fill_input' && <Input aria-label={`${t.enterprise.workflowValue} ${index + 1}`} value={step.value || ''} onChange={(e) => update(index, { value: e.target.value })} />}
        {step.type === 'wait' && <Input type="number" min="0" aria-label={`${t.enterprise.workflowWait} ${index + 1}`} value={step.ms ?? 0} onChange={(e) => update(index, { ms: Number(e.target.value) })} />}
      </div>)}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => setSteps((old) => [...old, { type: 'navigate' }])}>{t.enterprise.workflowAdd}</Button>
        <Button disabled={!selected || !steps.length || running} onClick={run}>{t.enterprise.workflowRun}</Button>
      </div>
      {error && <div role="alert" className="text-xs text-rose-300">{error}</div>}
      {result && <div className="space-y-1 text-xs text-slate-300"><h4>{t.enterprise.workflowResults}: {result.status} ({result.steps_completed}/{result.steps_total})</h4>
        {result.reason && <p>{result.reason}</p>}
        {Array.isArray(result.trace) && result.trace.map((item: any, i: number) => <div key={i} className="rounded border border-slate-700 p-2">
          <span>{item.step_index + 1}. {item.type} · {item.status} · {item.engine_mode || ''}</span>
          {item.type === 'extract_text' && (typeof item.redacted_count === 'number' ? <pre className="whitespace-pre-wrap break-all">{String(item.text || '')}</pre> : <p>{t.enterprise.workflowUnverified}</p>)}
        </div>)}
      </div>}
    </Card>
  )
}

export function BrowserWallView({
  browsers,
  capabilities,
  error,
  onReload,
}: {
  browsers: any[]
  capabilities: any
  error?: string | null
  onReload: () => void
}) {
  const { t } = useI18n()
  const [navUrl, setNavUrl] = useState<Record<string, string>>({})
  const [pngFrames, setPngFrames] = useState<Record<string, string>>({})
  const [browserError, setBrowserError] = useState<string | null>(null)

  const action = async (id: string, endpoint: string, body?: any) => {
    setBrowserError(null)
    try {
      const res = await apiFetch<any>(`/browsers/${id}/${endpoint}`, {
        method: 'POST',
        body: body ? JSON.stringify(body) : undefined,
      })
      if (endpoint === 'snapshot' && res?.screenshot_base64) {
        setPngFrames((prev) => ({
          ...prev,
          [id]: `data:image/png;base64,${res.screenshot_base64}`,
        }))
      } else if (endpoint === 'stop') {
        setPngFrames((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        })
      }
      onReload()
    } catch (err: any) {
      setBrowserError(err?.message || t.enterprise.operationFailed)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
          <Monitor className="h-5 w-5 text-cyan-400" />
          {t.browsers.title}
        </h2>
        {capabilities && (
          <Badge variant={capabilities.chromium_installed ? 'success' : 'warning'}>
            {t.browsers.capabilities}: {capabilities.engine_mode}
          </Badge>
        )}
      </div>

      {browserError && (
        <div role="alert" className="rounded-lg border border-rose-500/40 bg-rose-950/30 px-3 py-2 text-xs text-rose-200">
          {browserError}
        </div>
      )}

      <VisualWorkflowBuilder browsers={browsers} />
      {error ? (
        <EmptyState variant="error" onRetry={onReload} testId="browsers-error" />
      ) : browsers.length === 0 ? (
        <EmptyState variant="empty" testId="browsers-empty" />
      ) : (
      <div className="grid gap-4 lg:grid-cols-2">
        {browsers.map((bp) => (
          <Card key={bp.id} className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <div className="font-semibold text-white">{bp.name}</div>
                <div className="font-mono text-xs text-slate-400">
                  {bp.id} • {t.common.agentLabel}: {bp.agent_id || t.common.shared} • {t.common.crashes}: {bp.crash_count}
                </div>
              </div>
              <Badge variant={bp.status === 'running' ? 'success' : bp.status === 'crashed' ? 'danger' : 'neutral'}>
                {bp.status} ({bp.mode})
              </Badge>
            </div>

            {pngFrames[bp.id] ? (
              <img
                src={pngFrames[bp.id]}
                alt={`${bp.name} live snapshot`}
                className="w-full rounded-lg border border-slate-800 bg-slate-950 object-contain"
              />
            ) : bp.last_snapshot_svg ? (
              <div
                className="overflow-hidden rounded-lg border border-slate-800 bg-slate-950"
                dangerouslySetInnerHTML={{ __html: bp.last_snapshot_svg }}
              />
            ) : (
              <div className="flex h-44 items-center justify-center rounded-lg border border-dashed border-slate-800 bg-slate-950/60 text-xs text-slate-500">
                {bp.current_url} — {t.common.previewHint}
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => action(bp.id, 'launch', { mode: 'visible', owner: bp.agent_id || 'manager-0' })}>
                <Play className="h-3.5 w-3.5" />
                {t.browsers.launchVisible}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => action(bp.id, 'launch', { mode: 'background', owner: bp.agent_id || 'manager-0' })}
              >
                {t.browsers.launchBackground}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => action(bp.id, 'snapshot')}>
                <RefreshCw className="h-3.5 w-3.5 text-cyan-400" />
                {t.enterprise.snapshotStep}
              </Button>
              <Button size="sm" variant="secondary" onClick={() => action(bp.id, 'recover')}>
                <ShieldAlert className="h-3.5 w-3.5 text-amber-400" />
                {t.browsers.recover}
              </Button>
              <Button size="sm" variant="danger" onClick={() => action(bp.id, 'stop')}>
                <Square className="h-3.5 w-3.5" />
                {t.browsers.stop}
              </Button>
            </div>

            <div className="flex gap-2">
              <Input
                value={navUrl[bp.id] ?? bp.current_url}
                onChange={(e) => setNavUrl({ ...navUrl, [bp.id]: e.target.value })}
                placeholder="https://..."
              />
              <Button
                size="sm"
                variant="secondary"
                onClick={() => action(bp.id, 'navigate', { url: navUrl[bp.id] || bp.current_url })}
              >
                <Globe className="h-3.5 w-3.5" />
                {t.browsers.navigate}
              </Button>
            </div>
          </Card>
        ))}
      </div>
      )}
    </div>
  )
}

export interface BudgetLimits {
  daily_limit_usd?: number
  monthly_limit_usd?: number
  require_approval_above_usd?: number
  source?: 'settings' | 'usage' | 'providers'
}

export function formatUsd(value: unknown): string {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return '0.00'
  return n.toFixed(2)
}

/** Reads the first budget object found in a `GET /settings` payload (key -> { value }). */
export function extractBudgetLimits(settings: Record<string, any> | null | undefined): BudgetLimits | null {
  if (!settings) return null
  const candidate = settings.budgets?.value ?? settings.budget?.value ?? settings.budgets ?? settings.budget
  if (!candidate || typeof candidate !== 'object') return null
  const daily = candidate.daily_limit_usd ?? candidate.daily_limit
  const monthly = candidate.monthly_limit_usd ?? candidate.monthly_limit
  const threshold = candidate.require_approval_above_usd
  if (typeof daily !== 'number' && typeof monthly !== 'number') return null
  return {
    daily_limit_usd: typeof daily === 'number' ? daily : undefined,
    monthly_limit_usd: typeof monthly === 'number' ? monthly : undefined,
    require_approval_above_usd: typeof threshold === 'number' ? threshold : undefined,
    source: 'settings',
  }
}

/**
 * Spend derived from `GET /providers/usage` logs (the endpoint returns the 50 most recent
 * usage rows). Prefers explicit aggregates when the backend exposes them.
 */
export function computeSpend(usage: any, now = new Date()): { daily: number; monthly: number } {
  const explicitDaily = usage?.budget_summary?.global_daily_spend_usd ?? usage?.budget_summary?.daily_spend_usd ?? usage?.budget_summary?.daily?.spent_usd ?? usage?.spend?.daily_usd ?? usage?.daily_spend_usd
  const explicitMonthly = usage?.budget_summary?.global_monthly_spend_usd ?? usage?.budget_summary?.monthly_spend_usd ?? usage?.budget_summary?.monthly?.spent_usd ?? usage?.spend?.monthly_usd ?? usage?.monthly_spend_usd
  if (typeof explicitDaily === 'number' || typeof explicitMonthly === 'number') {
    return { daily: Number(explicitDaily || 0), monthly: Number(explicitMonthly || 0) }
  }
  let daily = 0
  let monthly = 0
  for (const log of Array.isArray(usage?.logs) ? usage.logs : []) {
    const raw = log?.created_at ?? log?.timestamp
    const when = raw ? new Date(raw) : null
    if (!when || Number.isNaN(when.getTime())) continue
    const cost = Number(log?.estimated_cost_usd || 0)
    if (when.toDateString() === now.toDateString()) daily += cost
    if (when.getMonth() === now.getMonth() && when.getFullYear() === now.getFullYear()) monthly += cost
  }
  if (daily === 0 && monthly === 0) {
    const total = Number(usage?.summary?.total_estimated_cost_usd || 0)
    return { daily: total, monthly: total }
  }
  return { daily, monthly }
}

function HeadroomBar({
  label,
  limit,
  spent,
  remainingLabel,
  remainingOverride,
}: {
  label: string
  limit?: number
  spent: number
  remainingLabel: string
  remainingOverride?: number
}) {
  const hasLimit = typeof limit === 'number' && limit >= 0
  const remaining = typeof remainingOverride === 'number' ? remainingOverride : hasLimit ? Math.max(0, (limit as number) - spent) : undefined
  const pct = hasLimit ? (limit === 0 ? 100 : Math.min(100, (spent / (limit as number)) * 100)) : 0
  return (
    <div className="space-y-1 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs">
      <div className="flex items-center justify-between text-slate-300">
        <span className="font-semibold">{label}</span>
        <span className="font-mono">
          ${formatUsd(spent)}
          {hasLimit ? ` / $${formatUsd(limit)}` : ''}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full ${pct >= 90 ? 'bg-rose-500' : pct >= 70 ? 'bg-amber-400' : 'bg-emerald-400'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-slate-500">{remainingLabel}</span>
        <span className={pct >= 90 ? 'font-semibold text-rose-300' : 'font-semibold text-emerald-300'}>
          {remaining === undefined ? '—' : `$${formatUsd(remaining)}`}
        </span>
      </div>
    </div>
  )
}

export function BudgetHeadroom({
  usage,
  budget,
  fallbackDailyLimit,
}: {
  usage: any
  budget: BudgetLimits | null
  fallbackDailyLimit?: number
}) {
  const { t } = useI18n()
  const spend = computeSpend(usage)
  const summary = usage?.budget_summary
  const dailyLimit = summary?.daily_limit_usd ?? summary?.daily?.limit_usd ?? budget?.daily_limit_usd ?? (fallbackDailyLimit && fallbackDailyLimit > 0 ? fallbackDailyLimit : undefined)
  const monthlyLimit = summary?.monthly_limit_usd ?? summary?.monthly?.limit_usd ?? budget?.monthly_limit_usd
  const hasAnyLimit = typeof dailyLimit === 'number' || typeof monthlyLimit === 'number'

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <h4 className="flex items-center gap-2 text-xs font-semibold text-slate-200">
        <Wallet className="h-4 w-4 text-amber-400" />
        {t.providers.budgetTitle}
      </h4>
      {hasAnyLimit ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <HeadroomBar
            label={`${t.providers.dailyLimit} · ${t.providers.spentToday}`}
            limit={dailyLimit}
            spent={spend.daily}
            remainingLabel={t.providers.remainingHeadroom}
            remainingOverride={summary?.daily_remaining_usd ?? summary?.daily?.remaining_usd}
          />
          <HeadroomBar
            label={`${t.providers.monthlyLimit} · ${t.providers.spentThisMonth}`}
            limit={monthlyLimit}
            spent={spend.monthly}
            remainingLabel={t.providers.remainingHeadroom}
            remainingOverride={summary?.monthly_remaining_usd ?? summary?.monthly?.remaining_usd}
          />
        </div>
      ) : (
        <div className="rounded border border-amber-500/30 bg-amber-950/20 p-2 text-xs text-amber-200">
          {t.providers.budgetUnavailable}
        </div>
      )}
      {typeof budget?.require_approval_above_usd === 'number' && (
        <div className="text-[11px] text-slate-400">
          {t.providers.approvalThreshold}:{' '}
          <span className="font-mono text-slate-200">${formatUsd(budget.require_approval_above_usd)}</span>
        </div>
      )}
    </div>
  )
}

export function ProvidersView({
  providers,
  usage,
  agents,
  sessions,
  error,
  onReload,
}: {
  providers: any[]
  usage: any
  agents: any[]
  sessions: any[]
  error?: string | null
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const [testResult, setTestResult] = useState<string | null>(null)
  const [agentId, setAgentId] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [budget, setBudget] = useState<BudgetLimits | null>(null)
  const [wizardPresets, setWizardPresets] = useState<any[]>([])
  const [wizardType, setWizardType] = useState<'anthropic_claude' | 'openai_compatible' | 'arena_local_bridge' | 'arena_agent'>('anthropic_claude')
  const [wizardEndpoint, setWizardEndpoint] = useState('https://api.anthropic.com/v1')
  const [wizardCredRef, setWizardCredRef] = useState('env:ANTHROPIC_API_KEY')
  const [wizardModel, setWizardModel] = useState('claude-sonnet-4-5')
  const [wizardAssignManager, setWizardAssignManager] = useState(true)
  const [wizardAssignWorkers, setWizardAssignWorkers] = useState(true)
  const [wizardTesting, setWizardTesting] = useState(false)
  const [wizardSaving, setWizardSaving] = useState(false)
  const [wizardStatus, setWizardStatus] = useState<{ ok: boolean; text: string } | null>(null)

  // Budget limits live in the `budgets` system setting (GET /settings returns key -> {value}).
  // Best-effort: the usage card falls back to provider caps when this is unavailable.
  useEffect(() => {
    apiFetch<Record<string, { value?: any }>>('/settings')
      .then((settings) => setBudget(extractBudgetLimits(settings)))
      .catch(() => setBudget(null))
    apiFetch<{ options?: any[] }>('/providers/wizard/presets')
      .then((data) => {
        if (Array.isArray(data?.options)) setWizardPresets(data.options)
      })
      .catch(() => setWizardPresets([]))
  }, [])

  const selectWizardPreset = (ptype: 'anthropic_claude' | 'openai_compatible' | 'arena_local_bridge' | 'arena_agent') => {
    setWizardType(ptype)
    setWizardStatus(null)
    const fromServer = wizardPresets.find((opt) => opt.provider_type === ptype)
    if (fromServer) {
      setWizardEndpoint(fromServer.endpoint_url || '')
      setWizardCredRef(fromServer.credential_ref || '')
      setWizardModel(fromServer.default_model || '')
      return
    }
    if (ptype === 'anthropic_claude') {
      setWizardEndpoint('https://api.anthropic.com/v1')
      setWizardCredRef('env:ANTHROPIC_API_KEY')
      setWizardModel('claude-sonnet-4-5')
    } else if (ptype === 'openai_compatible') {
      setWizardEndpoint('https://openrouter.ai/api/v1')
      setWizardCredRef('env:OPENROUTER_API_KEY')
      setWizardModel('qwen2.5-coder:7b')
    } else if (ptype === 'arena_agent') {
      setWizardEndpoint('http://127.0.0.1:4100')
      setWizardCredRef('env:ARENA_BRIDGE_TOKEN')
      setWizardModel('arena-agent-orchestrator')
    } else {
      setWizardEndpoint('')
      setWizardCredRef('env:ARENA_BRIDGE_TOKEN')
      setWizardModel('arena-bridge-default')
    }
  }

  const testWizardConnection = async () => {
    setWizardTesting(true)
    setWizardStatus(null)
    try {
      const res = await apiFetch<any>('/providers/wizard/test-connection', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: wizardType,
          endpoint_url: wizardEndpoint || null,
          credential_ref: wizardCredRef,
          model: wizardModel,
        }),
      })
      setWizardStatus({
        ok: Boolean(res.success),
        text: `${res.detail || (res.success ? 'Connected' : res.error_message)} (${res.model}, ${formatNumber(res.latency_ms)} ms)`,
      })
    } catch (err: any) {
      setWizardStatus({ ok: false, text: err?.message || 'Connection test failed' })
    } finally {
      setWizardTesting(false)
    }
  }

  const saveWizardConfig = async (e: React.FormEvent) => {
    e.preventDefault()
    setWizardSaving(true)
    setWizardStatus(null)
    try {
      const res = await apiFetch<any>('/providers/wizard/configure', {
        method: 'POST',
        body: JSON.stringify({
          provider_type: wizardType,
          endpoint_url: wizardEndpoint || null,
          credential_ref: wizardCredRef,
          default_model: wizardModel,
          available_models: [wizardModel],
          assign_to_manager: wizardAssignManager,
          assign_to_workers: wizardAssignWorkers,
        }),
      })
      setWizardStatus({
        ok: true,
        text: `${res.provider?.name || wizardType} (${res.provider?.default_model || wizardModel}) — ${res.model_status?.status || 'CONFIGURED'}`,
      })
      onReload()
    } catch (err: any) {
      setWizardStatus({ ok: false, text: err?.message || 'Unable to save provider configuration' })
    } finally {
      setWizardSaving(false)
    }
  }

  const providerDailyCap = providers.reduce(
    (sum, p) => sum + (typeof p.daily_limit_usd === 'number' ? p.daily_limit_usd : 0),
    0
  )

  const checkAll = async () => {
    await apiFetch('/providers/health-check', { method: 'POST' })
    onReload()
  }

  const dryRun = async (id: string) => {
    const res = await apiFetch<any>(`/providers/${id}/test`, {
      method: 'POST',
      body: JSON.stringify({ prompt: 'Verify provider routing & cost control.', dry_run: true, ...(agentId && { agent_id: agentId }), ...(sessionId && { session_id: sessionId }) }),
    })
    setTestResult(`${res.provider_id} (${res.model}): ${res.content}`)
    onReload()
  }

  const activePreset = wizardPresets.find((opt) => opt.provider_type === wizardType)
  const hasUnknownSummaryCost = Boolean(usage?.summary?.has_unknown_pricing || usage?.summary?.pricing_known === false)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
          <Cpu className="h-5 w-5 text-indigo-400" />
          {t.providers.title}
        </h2>
        <Button onClick={checkAll}>
          <RefreshCw className="h-4 w-4" />
          {t.providers.healthCheckAll}
        </Button>
      </div>

      {/* Provider Setup Wizard */}
      <Card className="space-y-4 border-indigo-500/30 bg-slate-900/80">
        <div className="space-y-1">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Sparkles className="h-4 w-4 text-indigo-400" />
            {t.providers.wizardTitle}
          </h3>
          <p className="text-xs text-slate-400">{t.providers.wizardSubtitle}</p>
        </div>

        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Button
            type="button"
            variant={wizardType === 'anthropic_claude' ? 'primary' : 'secondary'}
            onClick={() => selectWizardPreset('anthropic_claude')}
          >
            {t.providers.presetAnthropic}
          </Button>
          <Button
            type="button"
            variant={wizardType === 'openai_compatible' ? 'primary' : 'secondary'}
            onClick={() => selectWizardPreset('openai_compatible')}
          >
            {t.providers.presetOpenAICompatible}
          </Button>
          <Button
            type="button"
            variant={wizardType === 'arena_local_bridge' ? 'primary' : 'secondary'}
            onClick={() => selectWizardPreset('arena_local_bridge')}
          >
            {t.providers.presetBridge}
          </Button>
          <Button
            type="button"
            variant={wizardType === 'arena_agent' ? 'primary' : 'secondary'}
            onClick={() => selectWizardPreset('arena_agent')}
          >
            {t.providers.presetArenaAgent}
          </Button>
        </div>

        {wizardType === 'arena_agent' && (
          <p data-testid="wizard-arena-agent-hint" className="rounded-lg border border-indigo-500/30 bg-indigo-500/10 p-2 text-start text-xs text-indigo-200">
            {t.providers.arenaAgentHint}
          </p>
        )}

        {wizardType === 'openai_compatible' && Array.isArray(activePreset?.sub_presets) && (
          <div className="flex flex-wrap gap-2">
            {activePreset.sub_presets.map((sub: any) => (
              <Button
                key={sub.id}
                type="button"
                size="sm"
                variant="secondary"
                onClick={() => {
                  setWizardEndpoint(sub.endpoint_url || '')
                  setWizardCredRef(sub.credential_ref || '')
                  setWizardModel(sub.default_model || '')
                }}
              >
                {sub.label}
              </Button>
            ))}
          </div>
        )}

        <form onSubmit={saveWizardConfig} className="space-y-3">
          <div className="grid gap-3 md:grid-cols-3">
            <label className="space-y-1 text-xs text-slate-300">
              <span>{t.providers.endpointUrlLabel}</span>
              <Input
                aria-label={t.providers.endpointUrlLabel}
                value={wizardEndpoint}
                onChange={(e) => setWizardEndpoint(e.target.value)}
                placeholder="https://api.anthropic.com/v1"
              />
            </label>
            <label className="space-y-1 text-xs text-slate-300">
              <span>{t.providers.credentialRefLabel}</span>
              <Input
                aria-label={t.providers.credentialRefLabel}
                value={wizardCredRef}
                onChange={(e) => setWizardCredRef(e.target.value)}
                placeholder="env:ANTHROPIC_API_KEY"
                required
              />
            </label>
            <label className="space-y-1 text-xs text-slate-300">
              <span>{t.providers.modelNameLabel}</span>
              <Input
                aria-label={t.providers.modelNameLabel}
                value={wizardModel}
                onChange={(e) => setWizardModel(e.target.value)}
                placeholder="claude-sonnet-4-5"
                required
              />
            </label>
          </div>

          <div className="flex flex-wrap items-center gap-4 text-xs text-slate-300">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={wizardAssignManager}
                onChange={(e) => setWizardAssignManager(e.target.checked)}
              />
              {t.providers.applyToManager}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={wizardAssignWorkers}
                onChange={(e) => setWizardAssignWorkers(e.target.checked)}
              />
              {t.providers.applyToWorkers}
            </label>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" disabled={wizardTesting} onClick={testWizardConnection}>
              {wizardTesting ? t.providers.testingConnection : t.providers.testConnection}
            </Button>
            <Button type="submit" disabled={wizardSaving}>
              {wizardSaving ? t.providers.savingProvider : t.providers.saveAndActivate}
            </Button>
          </div>
        </form>

        {wizardStatus && (
          <div
            role="status"
            className={`rounded-lg border px-3 py-2 text-xs ${
              wizardStatus.ok
                ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-200'
                : 'border-rose-500/40 bg-rose-950/30 text-rose-200'
            }`}
          >
            {wizardStatus.text}
          </div>
        )}
      </Card>

      {testResult && (
        <Card className="border-sky-500/40 bg-sky-950/30 text-sm text-sky-200">{testResult}</Card>
      )}

      <div className="grid gap-2 sm:grid-cols-2">
        <Select aria-label={t.enterprise.optionalAgent} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          <option value="">{t.enterprise.optionalAgent}: {t.enterprise.noSelection}</option>
          {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
        </Select>
        <Select aria-label={t.enterprise.optionalSession} value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
          <option value="">{t.enterprise.optionalSession}: {t.enterprise.noSelection}</option>
          {sessions.map((session) => <option key={session.id} value={session.id}>{session.name || session.id}</option>)}
        </Select>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {error ? (
          <EmptyState variant="error" onRetry={onReload} testId="providers-error" className="md:col-span-3" />
        ) : providers.length === 0 ? (
          <EmptyState variant="empty" testId="providers-empty" className="md:col-span-3" />
        ) : providers.map((p) => (
          <Card key={p.id} className="space-y-3">
            <div className="flex items-center justify-between">
              <div className="font-semibold text-white">{p.name}</div>
              <Badge
                variant={
                  p.health_status === 'healthy' ? 'success' : p.health_status === 'degraded' ? 'warning' : 'danger'
                }
              >
                {p.health_status}
              </Badge>
            </div>
            <div className="space-y-1 text-xs text-slate-400">
              <div>Type: {p.provider_type}</div>
              <div>Model: <span className="font-mono text-slate-200">{p.default_model}</span></div>
              <div>Credential: <span className="font-mono text-sky-400">{p.credential_status}</span></div>
              <div>Daily Cap: ${p.daily_limit_usd}</div>
            </div>
            <Button size="sm" variant="secondary" onClick={() => dryRun(p.id)}>
              {t.providers.testDryRun}
            </Button>
          </Card>
        ))}
      </div>

      {usage && (
        <Card className="space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">{t.providers.usageSummary}</h3>
          <div className="flex flex-wrap gap-6 text-sm">
            <div>
              {t.providers.totalTokens}:{' '}
              <span className="font-bold text-sky-400">
                {formatNumber(usage.summary.total_input_tokens + usage.summary.total_output_tokens)}
              </span>
            </div>
            <div>
              {t.providers.totalCost}:{' '}
              {hasUnknownSummaryCost && Number(usage.summary.total_estimated_cost_usd || 0) === 0 ? (
                <span className="font-bold text-amber-300">{t.providers.unknownCost}</span>
              ) : (
                <span className="font-bold text-emerald-400">
                  ${formatUsd(usage.summary.total_estimated_cost_usd)}
                  {hasUnknownSummaryCost ? ` · ${t.providers.pricingUnknownBadge}` : ''}
                </span>
              )}
            </div>
          </div>
          {Array.isArray(usage.logs) && usage.logs.length > 0 && (
            <div className="max-h-40 space-y-1 overflow-y-auto text-xs">
              {usage.logs.slice(0, 10).map((lg: any, idx: number) => (
                <div key={lg.id || `${lg.created_at || 'log'}-${idx}`} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/50 px-2.5 py-1.5">
                  <span className="font-mono text-slate-200">
                    {lg.provider_id} · {lg.model_name}
                  </span>
                  <span className={lg.pricing_known === false ? 'text-amber-300' : 'text-emerald-300'}>
                    {lg.pricing_known === false ? t.providers.unknownCost : `$${formatUsd(lg.estimated_cost_usd)}`}
                  </span>
                </div>
              ))}
            </div>
          )}
          <BudgetHeadroom usage={usage} budget={budget} fallbackDailyLimit={providerDailyCap} />
        </Card>
      )}
    </div>
  )
}

export function ProjectsMemoryView({
  projects,
  memories,
  activeProjectId,
  onSelectProject,
  onReload,
}: {
  projects: any[]
  memories: any[]
  activeProjectId: string | null
  onSelectProject: (id: string) => void
  onReload: (q?: string) => void
}) {
  const { t } = useI18n()
  const [projName, setProjName] = useState('')
  const [projRepoUrl, setProjRepoUrl] = useState('')
  const [formError, setFormError] = useState<{ reason: 'required' | 'invalid' | 'conflict' | 'failed'; text: string } | null>(null)
  const [creating, setCreating] = useState(false)
  const [confirmDisable, setConfirmDisable] = useState<string | null>(null)
  const [statusBusy, setStatusBusy] = useState('')
  const [actionError, setActionError] = useState<{ projectId: string; reason: 'conflict' | 'failed'; text: string } | null>(null)
  const [memQuery, setMemQuery] = useState('')
  const [memTitle, setMemTitle] = useState('')
  const [memContent, setMemContent] = useState('')
  const [memScope, setMemScope] = useState('project')

  const createProject = async (e: React.FormEvent) => {
    e.preventDefault()
    // E28/E14: a project created without `repo_url` makes every task run in AATM's own repository,
    // so the URL is required here and validated before anything is sent.
    const name = projName.trim()
    const repoUrl = projRepoUrl.trim()
    if (!name || !repoUrl) {
      setFormError({ reason: 'required', text: t.projects.repoUrlRequired })
      return
    }
    if (!isGitHubRepoUrl(repoUrl)) {
      setFormError({ reason: 'invalid', text: t.projects.repoUrlInvalid })
      return
    }
    setCreating(true); setFormError(null)
    try {
      await apiFetch('/projects', {
        method: 'POST',
        body: JSON.stringify({ name, description: 'Managed AATM project', repo_url: repoUrl }),
      })
      setProjName(''); setProjRepoUrl('')
      onReload(memQuery)
    } catch (err: any) {
      const conflict = err?.status === 409
      setFormError({ reason: conflict ? 'conflict' : 'failed', text: err?.message || t.projects.createFailed })
    } finally {
      setCreating(false)
    }
  }

  /** PATCH the project status; the server refuses some changes with 409 and that is shown, not hidden. */
  const setProjectStatus = async (project: any, status: 'active' | 'disabled') => {
    setStatusBusy(project.id); setActionError(null); setConfirmDisable(null)
    try {
      await apiFetch(`/projects/${project.id}`, { method: 'PATCH', body: JSON.stringify({ status }) })
      onReload(memQuery)
    } catch (err: any) {
      setActionError({ projectId: project.id, reason: err?.status === 409 ? 'conflict' : 'failed', text: err?.message || t.projects.disableFailed })
    } finally {
      setStatusBusy('')
    }
  }

  const addMemory = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!memTitle.trim() || !memContent.trim()) return
    await apiFetch('/memory', {
      method: 'POST',
      body: JSON.stringify({
        scope: memScope,
        project_id: activeProjectId,
        title: memTitle,
        content: memContent,
      }),
    })
    setMemTitle('')
    setMemContent('')
    onReload(memQuery)
  }

  return (
    <div className="space-y-6">
      <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
        <Brain className="h-5 w-5 text-purple-400" />
        {t.projects.title}
      </h2>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="space-y-3">
          {projects.length === 0 ? (
            <EmptyState variant="empty" testId="projects-empty" />
          ) : <ul className="space-y-2">
            {projects.map((p) => {
              const isDefault = !!(p.config || {}).active
              const disabled = p.status === 'disabled'
              const rowError = actionError?.projectId === p.id ? actionError : null
              return <li key={p.id} data-testid={`project-row-${p.id}`} className={`rounded-lg border p-2 ${disabled ? 'border-slate-800 bg-slate-950/40 opacity-80' : 'border-slate-700'}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Button
                    size="sm"
                    variant={p.id === activeProjectId ? 'primary' : 'secondary'}
                    aria-pressed={p.id === activeProjectId}
                    onClick={() => onSelectProject(p.id)}
                  >
                    {p.name}
                  </Button>
                  <span className="flex flex-wrap items-center gap-1">
                    <span data-testid={`project-status-${p.id}`}>
                      <Badge variant={disabled ? 'warning' : 'success'}>{disabled ? t.projects.disabledBadge : t.projects.activeBadge}</Badge>
                    </span>
                    {isDefault && <span data-testid={`project-default-${p.id}`}><Badge variant="info">{t.projects.defaultBadge}</Badge></span>}
                  </span>
                </div>
                <div className="mt-1 truncate font-mono text-xs text-slate-400" dir="ltr">{p.repo_url || t.projects.repoUrlRequired}</div>
                {disabled && <p className="mt-1 text-xs text-amber-200">{t.projects.disabledNotice}</p>}
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {disabled ? (
                    <Button size="sm" variant="secondary" disabled={statusBusy === p.id}
                      aria-label={`${t.projects.enable} ${p.name}`} onClick={() => setProjectStatus(p, 'active')}>
                      <Play className="me-1 h-4 w-4" />{t.projects.enable}
                    </Button>
                  ) : isDefault ? (
                    <span className="text-xs text-slate-500">{t.projects.defaultCannotDisable}</span>
                  ) : (
                    <Button size="sm" variant="danger" disabled={statusBusy === p.id}
                      aria-label={`${t.projects.disable} ${p.name}`} onClick={() => setConfirmDisable(p.id)}>
                      <Square className="me-1 h-4 w-4" />{t.projects.disable}
                    </Button>
                  )}
                </div>
                {confirmDisable === p.id && <div data-testid="project-disable-confirm" role="alertdialog" aria-label={t.projects.disableConfirmTitle}
                  className="mt-2 rounded-lg border border-rose-500/40 bg-rose-500/10 p-2 text-start text-xs text-rose-100">
                  <p className="font-semibold">{t.projects.disableConfirmTitle}</p>
                  <p className="mt-1 text-slate-300">{t.projects.disableConfirmBody}</p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    <Button size="sm" variant="danger" data-testid="project-disable-confirm-yes" disabled={statusBusy === p.id}
                      aria-label={`${t.projects.disableConfirmYes} ${p.name}`} onClick={() => setProjectStatus(p, 'disabled')}>
                      {t.projects.disableConfirmYes}
                    </Button>
                    <Button size="sm" variant="secondary" data-testid="project-disable-confirm-cancel"
                      aria-label={t.projects.disableConfirmCancel} onClick={() => setConfirmDisable(null)}>
                      {t.projects.disableConfirmCancel}
                    </Button>
                  </div>
                </div>}
                {rowError && <p data-testid="project-action-error" data-reason={rowError.reason} role="alert"
                  aria-live="assertive" className="mt-1 text-xs text-rose-300">{rowError.text}</p>}
              </li>
            })}
          </ul>}
          <form onSubmit={createProject} className="space-y-2">
            <div className="space-y-1">
              <label htmlFor="project-name" className="text-xs text-slate-400">{t.projects.nameLabel}</label>
              <Input id="project-name" aria-label={t.projects.nameLabel} value={projName}
                onChange={(e) => setProjName(e.target.value)} placeholder={t.projects.namePlaceholder} />
            </div>
            <div className="space-y-1">
              <label htmlFor="project-repo-url" className="text-xs text-slate-400">{t.projects.repoUrlLabel}</label>
              <Input id="project-repo-url" aria-label={t.projects.repoUrlLabel} value={projRepoUrl} dir="ltr" aria-required="true"
                onChange={(e) => { setProjRepoUrl(e.target.value); setFormError(null) }}
                placeholder={t.projects.repoUrlPlaceholder} />
              <p className="text-xs text-slate-500">{t.projects.repoUrlHint}</p>
            </div>
            <Button type="submit" disabled={creating}>{t.common.create}</Button>
          </form>
          {formError && <p data-testid="project-form-error" data-reason={formError.reason} role="alert" aria-live="assertive"
            className="text-xs text-rose-300">{formError.text}</p>}
        </Card>

        <Card>
          <form onSubmit={addMemory} className="space-y-2">
            <div className="flex gap-2">
              <Select value={memScope} onChange={(e) => setMemScope(e.target.value)}>
                <option value="employee">{t.projects.scopeEmployee}</option>
                <option value="team">{t.projects.scopeTeam}</option>
                <option value="project">{t.projects.scopeProject}</option>
                <option value="organization">{t.projects.scopeOrganization}</option>
                <option value="decision">{t.projects.scopeDecision}</option>
                <option value="global">{t.projects.scopeGlobal}</option>
              </Select>
              <Input value={memTitle} onChange={(e) => setMemTitle(e.target.value)} placeholder={t.tasks.taskTitle} />
            </div>
            <div className="flex gap-2">
              <Input value={memContent} onChange={(e) => setMemContent(e.target.value)} placeholder={t.projects.memoryTitle} />
              <Button type="submit">{t.projects.addMemory}</Button>
            </div>
          </form>
        </Card>
      </div>

      <Card className="space-y-3">
        <div className="flex gap-2">
          <Input
            value={memQuery}
            onChange={(e) => setMemQuery(e.target.value)}
            placeholder={t.projects.searchMemory}
          />
          <Button variant="secondary" onClick={() => onReload(memQuery)}>
            {t.common.search}
          </Button>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {memories.length === 0 ? (
            <EmptyState variant="empty" testId="memories-empty" className="md:col-span-2" />
          ) : memories.map((m) => (
            <div key={m.id} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-white">{m.title}</span>
                <Badge variant="info">{m.scope}</Badge>
              </div>
              <p className="mt-1 text-xs text-slate-300">{m.content}</p>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

export function AgentWorktrees({ agents, worktrees, onReload }: { agents: any[]; worktrees: any[]; onReload: () => void }) {
  const { t } = useI18n()
  const [agent, setAgent] = useState('')
  const [branch, setBranch] = useState('')
  const [base, setBase] = useState('HEAD')
  const [approval, setApproval] = useState('')
  const [confirm, setConfirm] = useState(false)
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const run = async (remove: boolean) => {
    setBusy(true); setFeedback('')
    try {
      await apiFetch(remove ? `/git/worktrees/${encodeURIComponent(agent)}` : '/git/worktrees', {
        method: remove ? 'DELETE' : 'POST',
        ...(remove ? {} : { body: JSON.stringify({ agent_id: agent, branch_name: branch, base_ref: base, ...(approval.trim() ? { approval_id: approval.trim() } : {}) }) }),
      })
      setConfirm(false); setFeedback(t.enterprise.worktreeDone); onReload()
    } catch (err: any) { setFeedback(err?.message || t.enterprise.operationFailed); setConfirm(false) } finally { setBusy(false) }
  }
  return <Card className="space-y-3">
    <h3 className="text-sm font-semibold text-white">{t.enterprise.worktreesTitle}</h3>
    <div className="grid gap-2 sm:grid-cols-2">
      <Select aria-label={t.enterprise.worktreeAgent} value={agent} onChange={(e) => { setAgent(e.target.value); setConfirm(false) }}>
        <option value="">{t.enterprise.noSelection}</option>
        {agents.map((a) => <option key={a.id} value={a.id}>{a.name} ({a.id})</option>)}
      </Select>
      <Input aria-label={t.enterprise.worktreeBranch} placeholder={t.enterprise.worktreeBranch} value={branch} onChange={(e) => setBranch(e.target.value)} />
      <Input aria-label={t.enterprise.worktreeBase} value={base} onChange={(e) => setBase(e.target.value)} />
      <Input aria-label={t.enterprise.worktreeApproval} placeholder={t.enterprise.worktreeApproval} value={approval} onChange={(e) => setApproval(e.target.value)} />
    </div>
    <div className="flex flex-wrap gap-2">
      <Button disabled={!agent || !branch.trim() || !base.trim() || busy} onClick={() => run(false)}>{t.enterprise.worktreeCreate}</Button>
      <Button variant="danger" disabled={!agent || busy} onClick={() => confirm ? run(true) : setConfirm(true)}>{confirm ? t.enterprise.confirmRemove : t.enterprise.worktreeRemove}</Button>
      {confirm && <Button variant="secondary" onClick={() => setConfirm(false)}>{t.common.cancel}</Button>}
    </div>
    {feedback && <div role="status" className="text-xs text-slate-300">{feedback}</div>}
    {worktrees.map((wt, i) => <div key={i} className="break-all text-xs text-slate-400">{wt.worktree || wt.path} {wt.branch || ''}</div>)}
  </Card>
}

type GitHubTarget = 'pr' | 'issue' | 'branch' | 'comment'

export function GitWorkflowsView({ gitData, approvals, agents, error, onReload }: { gitData: any; approvals: any[]; agents: any[]; error?: string | null; onReload: () => void }) {
  const { t } = useI18n()
  const [mergeFeedback, setMergeFeedback] = useState<string | null>(null)

  // Approval-gated GitHub write state
  const [target, setTarget] = useState<GitHubTarget>('pr')
  const [repo, setRepo] = useState('oldman-088/AATM')
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [headBranch, setHeadBranch] = useState('')
  const [baseBranch, setBaseBranch] = useState('main')
  const [branchName, setBranchName] = useState('')
  const [fromBranch, setFromBranch] = useState('main')
  const [prNumber, setPrNumber] = useState('')
  const [approvalId, setApprovalId] = useState('')
  const [requestedApprovalId, setRequestedApprovalId] = useState('')
  const [writeFeedback, setWriteFeedback] = useState<{ kind: 'ok' | 'error' | 'info'; message: string; url?: string } | null>(null)
  const [requesting, setRequesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  if (error) return <EmptyState variant="error" onRetry={onReload} testId="git-error" />
  if (!gitData) return <EmptyState variant="loading" testId="git-loading" />

  const { repository, workflows } = gitData
  const approvalStatus = approvals.find((a) => a.id === approvalId.trim())?.status ?? (approvalId && approvalId === requestedApprovalId ? 'pending' : 'unknown')
  const statusLabel = approvalStatus === 'approved' ? t.enterprise.statusApproved : approvalStatus === 'pending' ? t.enterprise.statusPending : approvalStatus === 'rejected' ? t.enterprise.statusRejected : t.enterprise.statusUnknown

  const attemptMerge = async (wfId: string) => {
    const res = await apiFetch<any>(`/git/workflows/${wfId}/merge`, { method: 'POST' })
    setMergeFeedback(res.reason)
    onReload()
  }

  /**
   * Requests an approval bound to the specific GitHub operation.
   */
  const requestApproval = async () => {
    if (target === 'branch' && (!repo.trim() || !branchName.trim() || !fromBranch.trim())) return
    if (target === 'comment' && (!repo.trim() || !/^[1-9][0-9]*$/.test(prNumber) || !bodyText.trim())) return
    setRequesting(true)
    try {
      const created = await apiFetch<any>('/approvals', {
        method: 'POST',
        body: JSON.stringify({
          action_type: target === 'branch' ? 'branch' : 'merge',
          title: target === 'branch' ? `${t.enterprise.targetBranch}: ${branchName}` : target === 'comment' ? `${t.enterprise.commentMode}: #${prNumber}` : title || (target === 'pr' ? 'GitHub pull request' : 'GitHub issue'),
          description:
            target === 'branch' ? `${branchName} ← ${fromBranch} (${repo})` : target === 'comment' ? `${repo} #${prNumber}` : target === 'pr'
              ? `Create PR ${headBranch || repository.current_branch} -> ${baseBranch} in ${repo}`
              : `Create issue "${title}" in ${repo}`,
          payload: target === 'branch' ? { kind: 'branch', repo, branch_name: branchName, from_branch: fromBranch } : target === 'comment' ? { kind: 'comment', repo, pr_number: Number(prNumber), event: 'COMMENT', body: bodyText } : { kind: target, repo, title, body: bodyText, head: headBranch, base: baseBranch, target_branch: target === 'pr' ? baseBranch : undefined },
        }),
      })
      setApprovalId(created?.id || '')
      setRequestedApprovalId(created?.id || '')
      setWriteFeedback({ kind: 'info', message: `${t.git.approvalCreated} ${created?.id} — ${t.git.approvalPendingHint}` })
      onReload()
    } catch (err: any) {
      setWriteFeedback({ kind: 'error', message: err?.message || t.git.writeFailed })
    } finally {
      setRequesting(false)
    }
  }

  const submitWrite = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!(target === 'branch' ? branchName.trim() && fromBranch.trim() && repo.trim() : target === 'comment' ? repo.trim() && /^[1-9][0-9]*$/.test(prNumber) && bodyText.trim() : title.trim()) || !approvalId.trim() || approvalStatus === 'pending' || approvalStatus === 'rejected') {
      setWriteFeedback({ kind: 'error', message: t.git.approvalRequired })
      return
    }
    setSubmitting(true)
    try {
      const payload =
        target === 'comment'
          ? { approval_id: approvalId.trim(), repo, event: 'COMMENT', body: bodyText }
          : target === 'branch'
          ? { approval_id: approvalId.trim(), repo, branch_name: branchName, from_branch: fromBranch }
          : target === 'pr'
          ? {
              approval_id: approvalId.trim(),
              repo,
              title,
              body: bodyText,
              head: headBranch || repository.current_branch,
              base: baseBranch,
            }
          : { approval_id: approvalId.trim(), repo, title, body: bodyText }
      const res = await apiFetch<any>(target === 'pr' ? '/github/prs' : target === 'issue' ? '/github/issues' : target === 'branch' ? '/github/branches' : `/github/prs/${encodeURIComponent(prNumber)}/comments`, {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      const number = res?.pr_number ?? res?.issue_number ?? res?.number
      const ref = res?.url || res?.html_url || (number ? `#${number}` : '') || res?.branch_name || res?.id || ''
      const url = typeof res?.url === 'string' ? res.url : res?.html_url
      setWriteFeedback({ kind: 'ok', message: `${t.git.writeSuccess} ${ref}`.trim(), url: typeof url === 'string' && /^https:\/\//i.test(url) ? url : undefined })
      setTitle('')
      setBodyText('')
      setBranchName('')
      setApprovalId('')
      setRequestedApprovalId('')
      onReload()
    } catch (err: any) {
      setWriteFeedback({ kind: 'error', message: err?.message || t.git.writeFailed })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
        <FolderGit2 className="h-5 w-5 text-orange-400" />
        {t.git.title}
      </h2>

      {mergeFeedback && (
        <Card className="border-amber-500/40 bg-amber-950/30 text-sm text-amber-200">{mergeFeedback}</Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="space-y-2">
          <div className="text-sm font-semibold text-slate-200">{t.git.currentBranch}</div>
          <div className="font-mono text-lg text-sky-400">{repository.current_branch}</div>
          <div className="text-xs text-slate-400">
            {t.git.recentCommits}:
            <ul className="mt-2 space-y-1 font-mono text-[11px] text-slate-300">
              {(repository.recent_commits || []).map((c: string, i: number) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        </Card>

        <Card className="space-y-3">
          <div className="text-sm font-semibold text-slate-200">{t.git.protectedPolicy}</div>
          {workflows.map((wf: any) => (
            <div key={wf.id} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-mono text-sky-400">
                  {wf.branch_name} → {wf.base_branch}
                </span>
                <Badge variant={wf.is_protected_target ? 'warning' : 'success'}>
                  {wf.pr_status}
                </Badge>
              </div>
              <Button size="sm" variant="secondary" className="mt-2" onClick={() => attemptMerge(wf.id)}>
                {t.git.requestMerge}
              </Button>
            </div>
          ))}
        </Card>
      </div>

      <AgentWorktrees agents={agents} worktrees={Array.isArray(repository.worktrees) ? repository.worktrees : []} onReload={onReload} />
      {/* Approval-gated GitHub writes */}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Sparkles className="h-4 w-4 text-amber-400" />
            {t.git.writeTitle}
          </h3>
          <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 p-1">
            {(['pr', 'issue', 'branch', 'comment'] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => { setTarget(kind); setApprovalId(''); setRequestedApprovalId(''); setWriteFeedback(null) }}
                className={`rounded px-2.5 py-1 text-xs font-medium ${
                  target === kind ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-white'
                }`}
              >
                {kind === 'pr' ? t.git.targetPr : kind === 'issue' ? t.git.targetIssue : kind === 'branch' ? t.enterprise.targetBranch : t.enterprise.commentMode}
              </button>
            ))}
          </div>
        </div>
        <p className="text-xs text-slate-400">{t.git.writeHint}</p>

        {writeFeedback && (
          <div
            className={`rounded border p-2 text-xs ${
              writeFeedback.kind === 'ok'
                ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-200'
                : writeFeedback.kind === 'info'
                  ? 'border-sky-500/40 bg-sky-950/30 text-sky-200'
                  : 'border-rose-500/40 bg-rose-950/30 text-rose-200'
            }`}
          >
            {writeFeedback.url ? <a href={writeFeedback.url} target="_blank" rel="noopener noreferrer" className="underline">{writeFeedback.message}</a> : writeFeedback.message}
          </div>
        )}

        <form onSubmit={submitWrite} className="space-y-2">
          <div className="grid gap-2 md:grid-cols-2">
            <Input
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder={t.git.repositoryPlaceholder}
              aria-label={t.git.repository}
            />
            <Input
              value={approvalId}
              onChange={(e) => setApprovalId(e.target.value)}
              placeholder={t.git.approvalIdPlaceholder}
              aria-label={t.git.approvalId}
            />
          </div>
          {approvalId.trim() && <div role="status" className="text-xs text-slate-300">{t.enterprise.approvalStatus} {statusLabel}</div>}
          {target === 'comment' ? <Input type="number" min="1" aria-label={t.enterprise.prNumber} placeholder={t.enterprise.prNumber} value={prNumber} onChange={(e) => setPrNumber(e.target.value)} /> : target === 'branch' ? (
            <div className="grid gap-2 md:grid-cols-2">
              <Input value={branchName} onChange={(e) => setBranchName(e.target.value)} placeholder={t.enterprise.branchName} aria-label={t.enterprise.branchName} />
              <Input value={fromBranch} onChange={(e) => setFromBranch(e.target.value)} placeholder={t.enterprise.fromBranch} aria-label={t.enterprise.fromBranch} />
            </div>
          ) : (
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={target === 'pr' ? t.git.prTitlePlaceholder : t.git.issueTitlePlaceholder} aria-label={target === 'pr' ? t.git.prTitlePlaceholder : t.git.issueTitlePlaceholder} />
          )}
          {target === 'pr' && (
            <div className="grid gap-2 md:grid-cols-2">
              <Input
                value={headBranch}
                onChange={(e) => setHeadBranch(e.target.value)}
                placeholder={`${t.git.headBranch}: ${repository.current_branch}`}
                aria-label={t.git.headBranch}
              />
              <Input
                value={baseBranch}
                onChange={(e) => setBaseBranch(e.target.value)}
                placeholder={`${t.git.baseBranch}: main`}
                aria-label={t.git.baseBranch}
              />
            </div>
          )}
          {target !== 'branch' && <textarea
            value={bodyText}
            onChange={(e) => setBodyText(e.target.value)}
            rows={3}
            placeholder={target === 'comment' ? t.enterprise.commentBody : t.git.bodyPlaceholder}
            aria-label={target === 'comment' ? t.enterprise.commentBody : t.git.bodyPlaceholder}
            className="w-full rounded-lg border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-100 focus:border-sky-500 focus:outline-none"
          />}
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" onClick={requestApproval} disabled={requesting || (target === 'branch' && (!repo.trim() || !branchName.trim() || !fromBranch.trim())) || (target === 'comment' && (!repo.trim() || !/^[1-9][0-9]*$/.test(prNumber) || !bodyText.trim()))}>
              {t.git.requestApproval}
            </Button>
            <Button type="submit" disabled={submitting || !approvalId.trim() || approvalStatus === 'pending' || approvalStatus === 'rejected' || (target === 'branch' && (!repo.trim() || !branchName.trim() || !fromBranch.trim())) || (target === 'comment' && (!repo.trim() || !/^[1-9][0-9]*$/.test(prNumber) || !bodyText.trim()))}>
              {target === 'pr' ? t.git.submitPr : target === 'issue' ? t.git.submitIssue : target === 'branch' ? t.enterprise.submitBranch : t.enterprise.submitComment}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}
