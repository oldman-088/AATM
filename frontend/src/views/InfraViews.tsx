import React, { useState } from 'react'
import { Brain, Cpu, FolderGit2, Globe, Monitor, Play, RefreshCw, ShieldAlert, Square } from 'lucide-react'
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

export function ProvidersView({
  providers,
  usage,
  onReload,
}: {
  providers: any[]
  usage: any
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const [testResult, setTestResult] = useState<string | null>(null)

  const checkAll = async () => {
    await apiFetch('/providers/health-check', { method: 'POST' })
    onReload()
  }

  const dryRun = async (id: string) => {
    const res = await apiFetch<any>(`/providers/${id}/test`, {
      method: 'POST',
      body: JSON.stringify({ prompt: 'Verify provider routing & cost control.', dry_run: true }),
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
        <Card className="space-y-2">
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
              <span className="font-bold text-emerald-400">${usage.summary.total_estimated_cost_usd}</span>
            </div>
          </div>
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

export function GitWorkflowsView({ gitData, onReload }: { gitData: any; onReload: () => void }) {
  const { t } = useI18n()
  const [mergeFeedback, setMergeFeedback] = useState<string | null>(null)
  if (!gitData) return null

  const { repository, workflows } = gitData

  const attemptMerge = async (wfId: string) => {
    const res = await apiFetch<any>(`/git/workflows/${wfId}/merge`, { method: 'POST' })
    setMergeFeedback(res.reason)
    onReload()
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
    </div>
  )
}
