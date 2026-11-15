import React, { useEffect, useState } from 'react'
import { Brain, Cpu, FolderGit2, Globe, Monitor, Play, RefreshCw, ShieldAlert, Square, Sparkles, Wallet } from 'lucide-react'
import { Badge, Button, Card, Input, Select } from '../components/ui'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

export function BrowserWallView({
  browsers,
  capabilities,
  onReload,
}: {
  browsers: any[]
  capabilities: any
  onReload: () => void
}) {
  const { t } = useI18n()
  const [navUrl, setNavUrl] = useState<Record<string, string>>({})

  const action = async (id: string, endpoint: string, body?: any) => {
    await apiFetch(`/browsers/${id}/${endpoint}`, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    })
    onReload()
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

            {bp.last_snapshot_svg ? (
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
  onReload,
}: {
  providers: any[]
  usage: any
  agents: any[]
  sessions: any[]
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const [testResult, setTestResult] = useState<string | null>(null)
  const [agentId, setAgentId] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [budget, setBudget] = useState<BudgetLimits | null>(null)

  // Budget limits live in the `budgets` system setting (GET /settings returns key -> {value}).
  // Best-effort: the usage card falls back to provider caps when this is unavailable.
  useEffect(() => {
    apiFetch<Record<string, { value?: any }>>('/settings')
      .then((settings) => setBudget(extractBudgetLimits(settings)))
      .catch(() => setBudget(null))
  }, [])

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
        {providers.map((p) => (
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
              <span className="font-bold text-emerald-400">${formatUsd(usage.summary.total_estimated_cost_usd)}</span>
            </div>
          </div>
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
  const [memQuery, setMemQuery] = useState('')
  const [memTitle, setMemTitle] = useState('')
  const [memContent, setMemContent] = useState('')
  const [memScope, setMemScope] = useState('project')

  const createProject = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projName.trim()) return
    await apiFetch('/projects', {
      method: 'POST',
      body: JSON.stringify({ name: projName, description: 'Managed AATM project' }),
    })
    setProjName('')
    onReload(memQuery)
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
          <div className="flex flex-wrap gap-2">
            {projects.map((p) => (
              <Button
                key={p.id}
                size="sm"
                variant={p.id === activeProjectId ? 'primary' : 'secondary'}
                onClick={() => onSelectProject(p.id)}
              >
                {p.name}
              </Button>
            ))}
          </div>
          <form onSubmit={createProject} className="flex gap-2">
            <Input value={projName} onChange={(e) => setProjName(e.target.value)} placeholder={t.projects.newProject} />
            <Button type="submit">{t.common.create}</Button>
          </form>
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
          {memories.map((m) => (
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

type GitHubTarget = 'pr' | 'issue'

export function GitWorkflowsView({ gitData, approvals, onReload }: { gitData: any; approvals: any[]; onReload: () => void }) {
  const { t } = useI18n()
  const [mergeFeedback, setMergeFeedback] = useState<string | null>(null)

  // Approval-gated GitHub write state
  const [target, setTarget] = useState<GitHubTarget>('pr')
  const [repo, setRepo] = useState('oldman-088/AATM')
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [headBranch, setHeadBranch] = useState('')
  const [baseBranch, setBaseBranch] = useState('main')
  const [approvalId, setApprovalId] = useState('')
  const [requestedApprovalId, setRequestedApprovalId] = useState('')
  const [writeFeedback, setWriteFeedback] = useState<{ kind: 'ok' | 'error' | 'info'; message: string; url?: string } | null>(null)
  const [requesting, setRequesting] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  if (!gitData) return null

  const { repository, workflows } = gitData
  const approvalStatus = approvals.find((a) => a.id === approvalId.trim())?.status ?? (approvalId && approvalId === requestedApprovalId ? 'pending' : 'unknown')
  const statusLabel = approvalStatus === 'approved' ? t.enterprise.statusApproved : approvalStatus === 'pending' ? t.enterprise.statusPending : approvalStatus === 'rejected' ? t.enterprise.statusRejected : t.enterprise.statusUnknown

  const attemptMerge = async (wfId: string) => {
    const res = await apiFetch<any>(`/git/workflows/${wfId}/merge`, { method: 'POST' })
    setMergeFeedback(res.reason)
    onReload()
  }

  /**
   * Requests a pending merge-class approval for either GitHub write.
   */
  const requestApproval = async () => {
    setRequesting(true)
    try {
      const created = await apiFetch<any>('/approvals', {
        method: 'POST',
        body: JSON.stringify({
          action_type: 'merge',
          title: title || (target === 'pr' ? 'GitHub pull request' : 'GitHub issue'),
          description:
            target === 'pr'
              ? `Create PR ${headBranch || repository.current_branch} -> ${baseBranch} in ${repo}`
              : `Create issue "${title}" in ${repo}`,
          payload: { kind: target, repo, title, body: bodyText, head: headBranch, base: baseBranch, target_branch: target === 'pr' ? baseBranch : undefined },
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
    if (!title.trim() || !approvalId.trim() || approvalStatus === 'pending' || approvalStatus === 'rejected') {
      setWriteFeedback({ kind: 'error', message: t.git.approvalRequired })
      return
    }
    setSubmitting(true)
    try {
      const payload =
        target === 'pr'
          ? {
              approval_id: approvalId.trim(),
              repo,
              title,
              body: bodyText,
              head: headBranch || repository.current_branch,
              base: baseBranch,
            }
          : { approval_id: approvalId.trim(), repo, title, body: bodyText }
      const res = await apiFetch<any>(target === 'pr' ? '/github/prs' : '/github/issues', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      const number = res?.pr_number ?? res?.issue_number ?? res?.number
      const ref = res?.url || res?.html_url || (number ? `#${number}` : '') || res?.id || ''
      const url = typeof res?.url === 'string' ? res.url : res?.html_url
      setWriteFeedback({ kind: 'ok', message: `${t.git.writeSuccess} ${ref}`.trim(), url: typeof url === 'string' && /^https:\/\//i.test(url) ? url : undefined })
      setTitle('')
      setBodyText('')
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

      {/* Approval-gated GitHub writes (PR / Issue) */}
      <Card className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Sparkles className="h-4 w-4 text-amber-400" />
            {t.git.writeTitle}
          </h3>
          <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 p-1">
            {(['pr', 'issue'] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => setTarget(kind)}
                className={`rounded px-2.5 py-1 text-xs font-medium ${
                  target === kind ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-white'
                }`}
              >
                {kind === 'pr' ? t.git.targetPr : t.git.targetIssue}
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
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={target === 'pr' ? t.git.prTitlePlaceholder : t.git.issueTitlePlaceholder}
            aria-label={target === 'pr' ? t.git.prTitlePlaceholder : t.git.issueTitlePlaceholder}
          />
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
          <textarea
            value={bodyText}
            onChange={(e) => setBodyText(e.target.value)}
            rows={3}
            placeholder={t.git.bodyPlaceholder}
            aria-label={t.git.bodyPlaceholder}
            className="w-full rounded-lg border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-100 focus:border-sky-500 focus:outline-none"
          />
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" onClick={requestApproval} disabled={requesting}>
              {t.git.requestApproval}
            </Button>
            <Button type="submit" disabled={submitting || !approvalId.trim() || approvalStatus === 'pending' || approvalStatus === 'rejected'}>
              {target === 'pr' ? t.git.submitPr : t.git.submitIssue}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}
