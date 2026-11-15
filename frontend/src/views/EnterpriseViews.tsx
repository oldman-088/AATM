import React, { useEffect, useState } from 'react'
import { FolderCode, Laptop, Mail, Play, Plus, RefreshCw, ShieldAlert, Terminal, UserPlus, Wrench } from 'lucide-react'
import { Badge, Button, Card, Input, Select } from '../components/ui'
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
  const [name, setName] = useState('')
  const [templateId, setTemplateId] = useState('backend_developer')
  const [allocationMode, setAllocationMode] = useState('hybrid')
  const [autonomyMode, setAutonomyMode] = useState('semi_autonomous')
  const [feedback, setFeedback] = useState<string | null>(null)

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
    setFeedback(`Created employee ${emp.name} (${emp.id}) with isolated workspace & session.`)
    setName('')
    onReload()
  }

  const replaceEmployee = async (agentId: string) => {
    try {
      const res = await apiFetch<any>(`/employees/${agentId}/replace`, { method: 'POST' })
      setFeedback(`Replaced ${res.replaced_employee} -> ${res.replacement_employee} (${res.transferred_tasks.length} task(s) transferred).`)
      onReload()
    } catch (err: any) {
      setFeedback(err?.message || 'Replacement failed')
    }
  }

  return (
    <div className="space-y-4">
      <Card className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <UserPlus className="h-4 w-4 text-sky-400" />
            Dynamic AI Employee Creation (16+ Role Templates, 20+ Concurrent Scale)
          </h3>
          <Badge variant="info">{agents.length} Employees Registered</Badge>
        </div>

        {feedback && <div className="rounded border border-sky-500/30 bg-sky-950/30 p-2 text-xs text-sky-200">{feedback}</div>}

        <form onSubmit={createEmployee} className="grid gap-2 md:grid-cols-5">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Employee Name (e.g., API Specialist)" />
          <Select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
            {templates.map((tpl) => (
              <option key={tpl.id} value={tpl.id}>
                {tpl.title}
              </option>
            ))}
          </Select>
          <Select value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
            <option value="hybrid">Hybrid Account</option>
            <option value="one_per_employee">1 Account / Employee</option>
            <option value="shared">Shared Account</option>
          </Select>
          <Select value={autonomyMode} onChange={(e) => setAutonomyMode(e.target.value)}>
            <option value="supervised">Supervised</option>
            <option value="semi_autonomous">Semi-Autonomous</option>
            <option value="highly_autonomous">Highly Autonomous</option>
          </Select>
          <Button type="submit">
            <Plus className="h-4 w-4" /> Create Employee
          </Button>
        </form>
      </Card>

      {/* Organization Structure + Smart Replacement trigger */}
      <Card className="space-y-2">
        <h3 className="text-sm font-semibold text-slate-200">Organization Structure & Smart Replacement</h3>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((ag) => (
            <div key={ag.id} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/60 p-2.5 text-xs">
              <div className="min-w-0">
                <div className="truncate font-semibold text-white">{ag.name}</div>
                <div className="truncate text-slate-400">
                  {ag.role_template || ag.role} • {ag.autonomy_mode} • Score {ag.performance_score}
                </div>
              </div>
              {ag.role !== 'manager' && (
                <Button size="sm" variant="secondary" onClick={() => replaceEmployee(ag.id)}>
                  Replace
                </Button>
              )}
            </div>
          ))}
        </div>
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
        AATM Agent Messaging System (Direct Delivery & Auto-Execution)
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
          placeholder="Task instructions to deliver & execute..."
          className="md:col-span-2"
        />
        <div className="flex gap-2">
          <Input value={deliverable} onChange={(e) => setDeliverable(e.target.value)} placeholder="deliverable.md" />
          <Button type="submit">Dispatch</Button>
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
          Isolated Employee Workspaces & Real Tool Execution
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
          <h3 className="text-sm font-semibold text-slate-200">Create / Update Workspace File</h3>
          <form onSubmit={writeFile} className="space-y-2">
            <Input value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder="Relative path (e.g., module.py)" />
            <textarea
              value={fileContent}
              onChange={(e) => setFileContent(e.target.value)}
              rows={5}
              className="w-full rounded-lg border border-slate-700 bg-slate-950/70 p-3 font-mono text-xs text-slate-100 focus:border-sky-500 focus:outline-none"
            />
            <div className="flex gap-2">
              <Button type="submit">Save File</Button>
              <Button type="button" variant="secondary" onClick={runPython}>
                <Terminal className="h-4 w-4" /> Run with Python
              </Button>
            </div>
          </form>
          {execOut && <div className="rounded border border-emerald-500/30 bg-emerald-950/30 p-2 font-mono text-xs text-emerald-200">{execOut}</div>}
        </Card>

        <Card className="space-y-2">
          <h3 className="text-sm font-semibold text-slate-200">Files & Execution Artifacts ({selectedAgent})</h3>
          <div className="max-h-64 space-y-1.5 overflow-y-auto text-xs">
            {(wsData?.files || []).map((f: any) => (
              <div key={f.path} className="flex justify-between rounded border border-slate-800 bg-slate-950/60 p-2 font-mono">
                <span className="text-sky-400">{f.path}</span>
                <span className="text-slate-500">{f.size_bytes} B</span>
              </div>
            ))}
            {(wsData?.files || []).length === 0 && <div className="text-slate-500">Workspace empty.</div>}
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
        <h3 className="text-sm font-semibold text-white">Arena Accounts & Allocation Modes</h3>
        <form onSubmit={createAccount} className="grid gap-2 sm:grid-cols-4">
          <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Account Label" />
          <Input value={identifier} onChange={(e) => setIdentifier(e.target.value)} placeholder="account@arena.ai" />
          <Select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="one_per_employee">1 / Employee</option>
            <option value="shared">Shared</option>
            <option value="hybrid">Hybrid</option>
          </Select>
          <Button type="submit">Add Account</Button>
        </form>
        <div className="max-h-40 space-y-1.5 overflow-y-auto text-xs">
          {accounts.map((a) => (
            <div key={a.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <div className="font-semibold text-white">{a.label}</div>
                <div className="text-slate-400">
                  {a.account_identifier} • {a.allocation_mode} (max {a.max_employees})
                </div>
              </div>
              <Badge variant="success">{a.status}</Badge>
            </div>
          ))}
        </div>
      </Card>

      <Card className="space-y-3">
        <h3 className="text-sm font-semibold text-white">Independent Agent Provider Sessions</h3>
        <div className="max-h-52 space-y-1.5 overflow-y-auto text-xs">
          {sessions.map((s) => (
            <div key={s.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <div className="font-mono text-sky-400">{s.agent_id} → {s.conversation_id}</div>
                <div className="text-slate-400">Account: {s.account_id} • Health: {s.health_score}%</div>
              </div>
              <Button size="sm" variant="secondary" onClick={() => recoverSession(s.id)}>
                <RefreshCw className="h-3 w-3" /> Recover
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
  const [name, setName] = useState('')
  const [pattern, setPattern] = useState('http://localhost:5173/*')
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
      setErr(e?.message || 'Userscript policy error')
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="space-y-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
          <Wrench className="h-4 w-4 text-cyan-400" />
          Modular Browser Automation Engines
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
        <h3 className="text-sm font-semibold text-white">Tampermonkey Userscripts (Policy Audited)</h3>
        {err && <div className="rounded border border-rose-500/40 bg-rose-950/30 p-2 text-xs text-rose-200">{err}</div>}
        <form onSubmit={createScript} className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Userscript Name" />
            <Input value={pattern} onChange={(e) => setPattern(e.target.value)} placeholder="@match pattern" />
          </div>
          <div className="flex gap-2">
            <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="// JavaScript userscript..." />
            <Button type="submit">Register</Button>
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
                {u.is_enabled ? 'Enabled' : 'Disabled'}
              </Button>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

export function SDLCIdeaExecutorPanel({ onReload }: { onReload: () => void }) {
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
        Virtual AI Software Company — Idea-to-Delivery SDLC Pipeline
      </h3>
      <form onSubmit={runSdlc} className="grid gap-2 md:grid-cols-4">
        <Input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="Project Name" />
        <Input
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          placeholder="Describe your software idea (creates team, code & runs pytest)..."
          className="md:col-span-2"
        />
        <Button type="submit" disabled={running}>
          {running ? 'Executing SDLC...' : 'Launch SDLC Build'}
        </Button>
      </form>
      {result && (
        <div className="rounded border border-emerald-500/30 bg-emerald-950/30 p-2.5 text-xs text-emerald-200">
          Delivered <strong>{result.project_name}</strong> • Team: {result.team_created.join(', ')} • Pytest exit code:{' '}
          {result.pytest_result.exit_code}
        </div>
      )}
    </Card>
  )
}

export function MultiDeviceNodesPanel({ devices, onReload }: { devices: any[]; onReload: () => void }) {
  const [name, setName] = useState('')
  const [host, setHost] = useState('10.0.0.20')
  const [osName, setOsName] = useState('Ubuntu 24.04')

  const registerDevice = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    await apiFetch('/devices', {
      method: 'POST',
      body: JSON.stringify({
        id: `node-${Date.now().toString(36)}`,
        name,
        device_type: 'remote_server',
        host_address: host,
        os_name: osName,
      }),
    })
    setName('')
    onReload()
  }

  return (
    <Card className="space-y-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
        <Laptop className="h-4 w-4 text-sky-400" />
        Central Server & Multi-Device Execution Nodes
      </h3>
      <form onSubmit={registerDevice} className="grid gap-2 sm:grid-cols-4">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Node Name" />
        <Input value={host} onChange={(e) => setHost(e.target.value)} placeholder="Host IP / DNS" />
        <Input value={osName} onChange={(e) => setOsName(e.target.value)} placeholder="OS" />
        <Button type="submit">Register Node</Button>
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
            <Badge variant={d.status === 'online' ? 'success' : 'warning'}>{d.status}</Badge>
          </div>
        ))}
      </div>
    </Card>
  )
}

export function PromptInjectionGuardPanel() {
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
        Prompt-Injection Defense & Untrusted Content Quarantine
      </h3>
      <form onSubmit={testSanitize} className="flex gap-2">
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Test untrusted external text (e.g., 'Ignore all previous instructions...')..."
        />
        <Button type="submit" variant="secondary">
          Sanitize
        </Button>
      </form>
      {res && (
        <div className="rounded border border-slate-800 bg-slate-950/70 p-2 text-xs">
          <div>
            Injection Detected:{' '}
            <Badge variant={res.injection_detected ? 'danger' : 'success'}>
              {res.injection_detected ? 'QUARANTINED' : 'CLEAN'}
            </Badge>
          </div>
          <pre className="mt-1 overflow-x-auto font-mono text-[11px] text-slate-300">{res.quarantined_text}</pre>
        </div>
      )}
    </Card>
  )
}
