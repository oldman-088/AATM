import React, { useEffect, useState } from 'react'
import { FolderCode, Laptop, Mail, Play, Plus, RefreshCw, ShieldAlert, Terminal, UserPlus, Wrench } from 'lucide-react'
import { Badge, Button, Card, Input, Select } from '../components/ui'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

export function EmployeeCreatorAndOrgPanel({
  agents,
  templates,
  onReload,
}: {
  agents: any[]
  templates: any[]
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const [name, setName] = useState('')
  const [templateId, setTemplateId] = useState('backend_developer')
  const [allocationMode, setAllocationMode] = useState('hybrid')
  const [autonomyMode, setAutonomyMode] = useState('semi_autonomous')
  const [feedback, setFeedback] = useState<string | null>(null)
  const [traces, setTraces] = useState<any[] | null>(null)
  const [traceUnavailable, setTraceUnavailable] = useState(false)
  useEffect(() => {
    let active = true
    apiFetch<any>('/traces').then((result) => {
      if (active) setTraces(Array.isArray(result) ? result : Array.isArray(result?.traces) ? result.traces : [])
    }).catch(() => { if (active) setTraceUnavailable(true) })
    return () => { active = false }
  }, [])
  const managers = agents.filter((agent) => agent.id === 'manager-0' || agent.role === 'manager')
  const groups = new Map<string, any[]>()
  for (const agent of agents.filter((item) => !managers.includes(item))) {
    const divisions = Array.isArray(agent.divisions) && agent.divisions.length ? agent.divisions : [t.enterprise.unassignedDivision]
    for (const division of divisions) groups.set(division, [...(groups.get(division) || []), agent])
  }


  const createEmployee = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    const emp = await apiFetch<any>('/employees', {
      method: 'POST',
      body: JSON.stringify({
        name,
        role_template_id: templateId,
        allocation_mode: allocationMode,
        autonomy_mode: autonomyMode,
        provider_id: 'arena-agent',
      }),
    })
    setFeedback(`${t.enterprise.employeeCreated} ${emp.name} (${emp.id}) — ${t.enterprise.isolatedWorkspaceSession}`)
    setName('')
    onReload()
  }

  const replaceEmployee = async (agentId: string) => {
    try {
      const res = await apiFetch<any>(`/employees/${agentId}/replace`, { method: 'POST' })
      setFeedback(
        `${t.enterprise.replaced} ${res.replaced_employee} -> ${res.replacement_employee} (${formatNumber(res.transferred_tasks.length)} ${t.enterprise.tasksTransferred}).`
      )
      onReload()
    } catch (err: any) {
      setFeedback(err?.message || t.enterprise.replacementFailed)
    }
  }

  return (
    <div className="space-y-4">
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <UserPlus className="h-4 w-4 text-sky-400" />
            {t.enterprise.employeeCreationTitle}
          </h3>
          <Badge variant="info">
            {formatNumber(agents.length)} {t.enterprise.employeesRegistered}
          </Badge>
        </div>

        {feedback && <div className="rounded border border-sky-500/30 bg-sky-950/30 p-2 text-xs text-sky-200">{feedback}</div>}

        <form onSubmit={createEmployee} className="grid gap-2 md:grid-cols-5">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t.enterprise.employeeNamePlaceholder} />
          <Select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
            {templates.map((tpl) => (
              <option key={tpl.id} value={tpl.id}>
                {tpl.title}
              </option>
            ))}
          </Select>
          <Select value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
            <option value="hybrid">{t.enterprise.allocationHybrid}</option>
            <option value="one_per_employee">{t.enterprise.allocationOnePerEmployee}</option>
            <option value="shared">{t.enterprise.allocationShared}</option>
          </Select>
          <Select value={autonomyMode} onChange={(e) => setAutonomyMode(e.target.value)}>
            <option value="supervised">{t.enterprise.autonomySupervised}</option>
            <option value="semi_autonomous">{t.enterprise.autonomySemiAutonomous}</option>
            <option value="highly_autonomous">{t.enterprise.autonomyHighlyAutonomous}</option>
          </Select>
          <Button type="submit">
            <Plus className="h-4 w-4" /> {t.enterprise.createEmployee}
          </Button>
        </form>
      </Card>

      {/* Organization Structure + Smart Replacement trigger */}
      <Card className="space-y-2">
        <h3 className="text-sm font-semibold text-slate-200">{t.enterprise.orgStructureTitle}</h3>
        <p className="text-xs text-slate-400">{t.enterprise.divisionGroups}</p>
        {managers.map((ag) => <div key={ag.id} className="rounded border border-sky-800 p-2 text-xs text-white">{ag.name} • {ag.role}</div>)}
        {[...groups.entries()].map(([division, members]) => (
          <section key={division} className="space-y-2 border-s border-slate-700 ps-3">
            <h4 className="text-xs font-semibold text-sky-300">{division}</h4>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {members.map((ag) => (
                <div key={ag.id} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 text-xs">
                  <div className="min-w-0"><div className="truncate font-semibold text-white">{ag.name}</div>
                    <div className="truncate text-slate-400">{ag.role_template || ag.role} • {ag.autonomy_mode} • {t.enterprise.score} {ag.performance_score}</div>
                  </div>
                  <Button size="sm" variant="secondary" onClick={() => replaceEmployee(ag.id)}>{t.enterprise.replace}</Button>
                </div>
              ))}
            </div>
          </section>
        ))}
      </Card>
      <Card className="space-y-2">
        <h3 className="text-sm font-semibold text-slate-200">{t.enterprise.executionTraces}</h3>
        {traceUnavailable ? <p className="text-xs text-slate-400">{t.enterprise.tracesUnavailable}</p> : traces && traces.length === 0 ? <p className="text-xs text-slate-400">{t.enterprise.noTraces}</p> : traces?.map((trace, i) => <div key={trace.id || i} className="break-all rounded border border-slate-800 p-2 text-xs text-slate-300">{[trace.name || trace.action || trace.id, trace.status, trace.agent_id, trace.created_at].filter(Boolean).join(' • ')}</div>)}
      </Card>
    </div>
  )
}

export function AgentMessagingPanel({
  agents,
  messages,
  projectId,
  onReload,
}: {
  agents: any[]
  messages: any[]
  projectId: string | null
  onReload: () => void
}) {
  const { t } = useI18n()
  const [recipientId, setRecipientId] = useState('worker-1')
  const [instructions, setInstructions] = useState('')
  const [deliverable, setDeliverable] = useState('task_output.md')

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!instructions.trim()) return
    await apiFetch('/messages', {
      method: 'POST',
      body: JSON.stringify({
        sender_id: 'manager-0',
        recipient_id: recipientId,
        project_id: projectId,
        instructions,
        expected_deliverables: [deliverable || 'task_output.md'],
        auto_execute: true,
      }),
    })
    setInstructions('')
    onReload()
  }

  return (
    <Card className="space-y-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
        <Mail className="h-4 w-4 text-emerald-400" />
        {t.enterprise.messagingTitle}
      </h3>
      <form onSubmit={sendMessage} className="grid gap-2 md:grid-cols-4">
        <Select value={recipientId} onChange={(e) => setRecipientId(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.id})
            </option>
          ))}
        </Select>
        <Input
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder={t.enterprise.messagingPlaceholder}
          className="md:col-span-2"
        />
        <div className="flex gap-2">
          <Input value={deliverable} onChange={(e) => setDeliverable(e.target.value)} placeholder={t.enterprise.deliverablePlaceholder} />
          <Button type="submit">{t.enterprise.dispatch}</Button>
        </div>
      </form>
      <div className="max-h-48 space-y-2 overflow-y-auto text-xs">
        {messages.map((m) => (
          <div key={m.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
            <div>
              <span className="font-mono text-sky-400">
                {m.sender_id} → {m.recipient_id}
              </span>
              <div className="text-slate-300">{m.instructions}</div>
            </div>
            <Badge variant={m.execution_status === 'completed' ? 'success' : 'info'}>{m.execution_status}</Badge>
          </div>
        ))}
      </div>
    </Card>
  )
}

export function WorkspacesView({ agents }: { agents: any[] }) {
  const { t } = useI18n()
  const [selectedAgent, setSelectedAgent] = useState(agents[0]?.id || 'worker-1')
  const [wsData, setWsData] = useState<any>(null)
  const [filePath, setFilePath] = useState('hello_aatm.py')
  const [fileContent, setFileContent] = useState('print("AATM isolated workspace verified")\n')
  const [execOut, setExecOut] = useState<string | null>(null)

  const loadWorkspace = async (aid: string) => {
    const res = await apiFetch<any>(`/workspaces/${aid}/files`)
    setWsData(res)
  }

  useEffect(() => {
    if (selectedAgent) loadWorkspace(selectedAgent).catch(() => {})
  }, [selectedAgent])

  const writeFile = async (e: React.FormEvent) => {
    e.preventDefault()
    await apiFetch(`/workspaces/${selectedAgent}/files`, {
      method: 'POST',
      body: JSON.stringify({ relative_path: filePath, content: fileContent }),
    })
    loadWorkspace(selectedAgent)
  }

  const runPython = async () => {
    const res = await apiFetch<any>(`/workspaces/${selectedAgent}/exec`, {
      method: 'POST',
      body: JSON.stringify({ command: ['python', filePath] }),
    })
    setExecOut(`exit=${res.exit_code} | ${res.stdout || res.stderr}`)
    loadWorkspace(selectedAgent)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
          <FolderCode className="h-5 w-5 text-amber-400" />
          {t.enterprise.workspacesTitle}
        </h2>
        <Select value={selectedAgent} onChange={(e) => setSelectedAgent(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} ({a.id})
            </option>
          ))}
        </Select>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">{t.enterprise.workspaceFileTitle}</h3>
          <form onSubmit={writeFile} className="space-y-2">
            <Input value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder={t.enterprise.filePathPlaceholder} />
            <textarea
              value={fileContent}
              onChange={(e) => setFileContent(e.target.value)}
              rows={5}
              className="w-full rounded-lg border border-slate-700 bg-slate-950/70 p-3 font-mono text-xs text-slate-100 focus:border-sky-500 focus:outline-none"
            />
            <div className="flex gap-2">
              <Button type="submit">{t.enterprise.saveFile}</Button>
              <Button type="button" variant="secondary" onClick={runPython}>
                <Terminal className="h-4 w-4" /> {t.enterprise.runWithPython}
              </Button>
            </div>
          </form>
          {execOut && <div className="rounded border border-emerald-500/30 bg-emerald-950/30 p-2 font-mono text-xs text-emerald-200">{execOut}</div>}
        </Card>

        <Card className="space-y-2">
          <h3 className="text-sm font-semibold text-slate-200">
            {t.enterprise.filesArtifacts} ({selectedAgent})
          </h3>
          <div className="max-h-64 space-y-1.5 overflow-y-auto text-xs">
            {(wsData?.files || []).map((f: any) => (
              <div key={f.path} className="flex justify-between rounded border border-slate-800 bg-slate-950/60 p-2 font-mono">
                <span className="text-sky-400">{f.path}</span>
                <span className="text-slate-500">{f.size_bytes} B</span>
              </div>
            ))}
            {(wsData?.files || []).length === 0 && <div className="text-slate-500">{t.enterprise.workspaceEmpty}</div>}
          </div>
        </Card>
      </div>
    </div>
  )
}

export function ArenaAccountsSessionsPanel({
  accounts,
  sessions,
  onReload,
}: {
  accounts: any[]
  sessions: any[]
  onReload: () => void
}) {
  const { t } = useI18n()
  const [label, setLabel] = useState('')
  const [identifier, setIdentifier] = useState('')
  const [mode, setMode] = useState('hybrid')

  const createAccount = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!label.trim() || !identifier.trim()) return
    await apiFetch('/accounts', {
      method: 'POST',
      body: JSON.stringify({ provider_id: 'arena-agent', label, account_identifier: identifier, allocation_mode: mode }),
    })
    setLabel('')
    setIdentifier('')
    onReload()
  }

  const recoverSession = async (id: string) => {
    await apiFetch(`/sessions/${id}/recover`, { method: 'POST' })
    onReload()
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="space-y-3">
        <h3 className="text-sm font-semibold text-white">{t.enterprise.accountsTitle}</h3>
        <form onSubmit={createAccount} className="grid gap-2 sm:grid-cols-4">
          <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t.enterprise.accountLabelPlaceholder} />
          <Input value={identifier} onChange={(e) => setIdentifier(e.target.value)} placeholder={t.enterprise.accountIdentifierPlaceholder} />
          <Select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="one_per_employee">{t.enterprise.allocationOnePerEmployee}</option>
            <option value="shared">{t.enterprise.allocationShared}</option>
            <option value="hybrid">{t.enterprise.allocationHybrid}</option>
          </Select>
          <Button type="submit">{t.enterprise.addAccount}</Button>
        </form>
        <div className="max-h-40 space-y-1.5 overflow-y-auto text-xs">
          {accounts.map((a) => (
            <div key={a.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <div className="font-semibold text-white">{a.label}</div>
                <div className="text-slate-400">
                  {a.account_identifier} • {a.allocation_mode} ({t.enterprise.max} {a.max_employees})
                </div>
              </div>
              <Badge variant="success">{a.status}</Badge>
            </div>
          ))}
        </div>
      </Card>

      <Card className="space-y-3">
        <h3 className="text-sm font-semibold text-white">{t.enterprise.sessionsTitle}</h3>
        <div className="max-h-52 space-y-1.5 overflow-y-auto text-xs">
          {sessions.map((s) => (
            <div key={s.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <div className="font-mono text-sky-400">{s.agent_id} → {s.conversation_id}</div>
                <div className="text-slate-400">
                  {t.enterprise.account}: {s.account_id} • {t.enterprise.health}: {s.health_score}%
                </div>
              </div>
              <Button size="sm" variant="secondary" onClick={() => recoverSession(s.id)}>
                <RefreshCw className="h-3 w-3" /> {t.enterprise.recover}
              </Button>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

export function UserscriptsEnginesPanel({
  engines,
  userscripts,
  onReload,
}: {
  engines: any
  userscripts: any[]
  onReload: () => void
}) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [pattern, setPattern] = useState('https://example.com/*')
  const [code, setCode] = useState("console.info('AATM userscript active');")
  const [err, setErr] = useState<string | null>(null)

  const createScript = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    try {
      await apiFetch('/userscripts', {
        method: 'POST',
        body: JSON.stringify({ name, match_patterns: [pattern], code }),
      })
      setName('')
      setErr(null)
      onReload()
    } catch (e: any) {
      setErr(e?.message || t.enterprise.userscriptPolicyError)
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="space-y-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
          <Wrench className="h-4 w-4 text-cyan-400" />
          {t.enterprise.enginesTitle}
        </h3>
        <div className="grid gap-2 sm:grid-cols-2">
          {engines &&
            Object.entries(engines).map(([key, val]: [string, any]) => (
              <div key={key} className="rounded border border-slate-800 bg-slate-950/60 p-2.5 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-mono font-bold text-sky-400">{key}</span>
                  <Badge variant="info">{val.status}</Badge>
                </div>
              </div>
            ))}
        </div>
      </Card>

      <Card className="space-y-3">
        <h3 className="text-sm font-semibold text-white">{t.enterprise.userscriptsTitle}</h3>
        {err && <div className="rounded border border-rose-500/40 bg-rose-950/30 p-2 text-xs text-rose-200">{err}</div>}
        <form onSubmit={createScript} className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t.enterprise.userscriptNamePlaceholder} />
            <Input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder={t.enterprise.matchPatternPlaceholder} />
          </div>
          <div className="flex gap-2">
            <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder={t.enterprise.userscriptCodePlaceholder} />
            <Button type="submit">{t.enterprise.register}</Button>
          </div>
        </form>
        <div className="space-y-1.5 text-xs">
          {userscripts.map((u) => (
            <div key={u.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <div className="font-semibold text-white">{u.name}</div>
                <div className="font-mono text-slate-400">{(u.match_patterns || []).join(', ')}</div>
              </div>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => apiFetch(`/userscripts/${u.id}/toggle`, { method: 'PATCH' }).then(onReload)}
              >
                {u.is_enabled ? t.enterprise.enabled : t.enterprise.disabled}
              </Button>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

export function SDLCIdeaExecutorPanel({ onReload }: { onReload: () => void }) {
  const { t } = useI18n()
  const [projectName, setProjectName] = useState('')
  const [idea, setIdea] = useState('')
  const [result, setResult] = useState<any>(null)
  const [running, setRunning] = useState(false)

  const runSdlc = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projectName.trim() || !idea.trim()) return
    setRunning(true)
    try {
      const res = await apiFetch<any>('/sdlc/execute', {
        method: 'POST',
        body: JSON.stringify({ project_name: projectName, idea }),
      })
      setResult(res)
      setProjectName('')
      setIdea('')
      onReload()
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card className="space-y-3 border-sky-500/30">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
        <Play className="h-4 w-4 text-emerald-400" />
        {t.enterprise.sdlcTitle}
      </h3>
      <form onSubmit={runSdlc} className="grid gap-2 md:grid-cols-4">
        <Input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder={t.enterprise.sdlcProjectPlaceholder} />
        <Input
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          placeholder={t.enterprise.sdlcIdeaPlaceholder}
          className="md:col-span-2"
        />
        <Button type="submit" disabled={running}>
          {running ? t.enterprise.sdlcExecuting : t.enterprise.sdlcLaunch}
        </Button>
      </form>
      {result && (
        <div className="rounded border border-emerald-500/30 bg-emerald-950/30 p-2.5 text-xs text-emerald-200">
          {t.enterprise.delivered} <strong>{result.project_name}</strong> • {t.enterprise.team}: {result.team_created.join(', ')} •{' '}
          {t.enterprise.pytestExitCode}: {result.pytest_result.exit_code}
        </div>
      )}
    </Card>
  )
}

export function MultiDeviceNodesPanel({ devices, onReload }: { devices: any[]; onReload: () => void }) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [host, setHost] = useState('10.0.0.20')
  const [osName, setOsName] = useState('Ubuntu 24.04')
  const [pinged, setPinged] = useState<string | null>(null)
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null)
  const [rotated, setRotated] = useState<{ deviceId: string; deviceName: string; token: string; registration?: boolean } | null>(null)
  const [revokeError, setRevokeError] = useState<string | null>(null)

  const registerDevice = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    const result = await apiFetch<any>('/devices', {
      method: 'POST',
      body: JSON.stringify({
        id: `node-${Date.now().toString(36)}`,
        name,
        device_type: 'remote_server',
        host_address: host,
        os_name: osName,
      }),
    })
    if (result?.device_token) setRotated({ deviceId: result.device?.id || result.id, deviceName: name, token: result.device_token, registration: true })
    setName('')
    onReload()
  }

  // Authenticated heartbeat (Worker 4 hardening: endpoint now requires a JWT, so it must
  // travel through apiFetch rather than a raw fetch call).
  const sendHeartbeat = async (deviceId: string) => {
    await apiFetch(`/devices/${deviceId}/heartbeat`, {
      method: 'POST',
      body: JSON.stringify({ cpu_percent: 21, ram_percent: 34 }),
    })
    setPinged(deviceId)
    onReload()
  }

  /**
   * Revokes the device credential and surfaces the rotated token exactly once
   * (the backend never returns it again, so it is kept only in transient state).
   */
  const revokeToken = async (device: any) => {
    setConfirmRevoke(null)
    setRevokeError(null)
    try {
      const res = await apiFetch<any>(`/devices/${device.id}/revoke`, { method: 'POST' })
      const token = res?.new_token || res?.token || res?.device_token || res?.rotated_token
      setRotated({ deviceId: device.id, deviceName: device.name, token: token || '' })
      onReload()
    } catch (err: any) {
      setRevokeError(err?.message || t.enterprise.revokeFailed)
    }
  }

  return (
    <Card className="space-y-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
        <Laptop className="h-4 w-4 text-sky-400" />
        {t.enterprise.devicesTitle}
      </h3>
      {revokeError && (
        <div className="rounded border border-rose-500/40 bg-rose-950/30 p-2 text-xs text-rose-200">{revokeError}</div>
      )}

      {rotated && (
        <div className="space-y-1 rounded-lg border border-amber-500/40 bg-amber-950/30 p-2.5 text-xs text-amber-100">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold">
              {rotated.registration ? t.enterprise.deviceRegistered : t.enterprise.revokeSuccess} {rotated.deviceName}
            </span>
            <Button size="sm" variant="secondary" onClick={() => setRotated(null)}>
              {t.common.cancel}
            </Button>
          </div>
          {rotated.token ? (
            <>
              <div className="font-semibold">{rotated.registration ? t.enterprise.deviceToken : t.enterprise.rotatedToken}</div>
              <code
                data-testid="rotated-device-token"
                className="block break-all rounded bg-slate-950/70 p-2 font-mono text-[11px] text-emerald-200"
              >
                {rotated.token}
              </code>
              <div className="text-[11px] text-amber-200/80">{t.enterprise.rotatedTokenHint}</div>
            </>
          ) : null}
        </div>
      )}

      <form onSubmit={registerDevice} className="grid gap-2 sm:grid-cols-4">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t.enterprise.nodeNamePlaceholder} />
        <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder={t.enterprise.hostPlaceholder} />
        <Input value={osName} onChange={(e) => setOsName(e.target.value)} placeholder={t.enterprise.osPlaceholder} />
        <Button type="submit">{t.enterprise.registerNode}</Button>
      </form>
      <div className="grid gap-2 sm:grid-cols-2">
        {devices.map((d) => (
          <div key={d.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2.5 text-xs">
            <div>
              <div className="font-semibold text-white">{d.name}</div>
              <div className="font-mono text-slate-400">
                {d.host_address} • {d.os_name} • {d.cpu_cores} vCPU
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {pinged === d.id && <Badge variant="info">{t.enterprise.heartbeatSent}</Badge>}
              <Badge variant={d.status === 'online' ? 'success' : 'warning'}>{d.status}</Badge>
              <Button size="sm" variant="secondary" onClick={() => sendHeartbeat(d.id)}>
                {t.enterprise.heartbeat}
              </Button>
              {confirmRevoke === d.id ? (
                <>
                  <Button size="sm" variant="danger" onClick={() => revokeToken(d)}>
                    {t.enterprise.revokeConfirm}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmRevoke(null)}>
                    {t.common.cancel}
                  </Button>
                </>
              ) : (
                <Button size="sm" variant="danger" onClick={() => setConfirmRevoke(d.id)}>
                  {t.enterprise.revokeToken}
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

export function PromptInjectionGuardPanel() {
  const { t } = useI18n()
  const [text, setText] = useState('')
  const [res, setRes] = useState<any>(null)

  const testSanitize = async (e: React.FormEvent) => {
    e.preventDefault()
    const out = await apiFetch<any>('/security/sanitize', {
      method: 'POST',
      body: JSON.stringify({ text, source: 'webpage' }),
    })
    setRes(out)
  }

  return (
    <Card className="space-y-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
        <ShieldAlert className="h-4 w-4 text-amber-400" />
        {t.enterprise.guardTitle}
      </h3>
      <form onSubmit={testSanitize} className="flex gap-2">
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t.enterprise.guardPlaceholder}
        />
        <Button type="submit" variant="secondary">
          {t.enterprise.sanitize}
        </Button>
      </form>
      {res && (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-xs">
          <div>
            {t.enterprise.injectionDetected}{' '}
            <Badge variant={res.injection_detected ? 'danger' : 'success'}>
              {res.injection_detected ? t.enterprise.quarantined : t.enterprise.clean}
            </Badge>
          </div>
          <pre className="mt-1 overflow-x-auto font-mono text-[11px] text-slate-300">{res.quarantined_text}</pre>
        </div>
      )}
    </Card>
  )
}
