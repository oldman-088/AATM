import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Bell,
  Bot,
  Brain,
  CheckSquare,
  Cpu,
  FolderCode,
  FolderGit2,
  Languages,
  LayoutDashboard,
  ListTodo,
  LogIn,
  LogOut,
  MessageSquare,
  Monitor,
  OctagonAlert,
  ShieldCheck,
  SunMoon,
} from 'lucide-react'
import { Badge, Button, Input } from './components/ui'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { I18nProvider, useI18n } from './i18n/I18nContext'
import type { Language } from './i18n/types'
import { apiFetch, getToken } from './lib/api'
import { AgentsView, DashboardView, ManagerChatView, TasksView } from './views/CoreViews'
import {
  AgentMessagingPanel,
  ArenaAccountsSessionsPanel,
  EmployeeCreatorAndOrgPanel,
  MultiDeviceNodesPanel,
  PromptInjectionGuardPanel,
  SDLCIdeaExecutorPanel,
  UserscriptsEnginesPanel,
  WorkspacesView,
} from './views/EnterpriseViews'
import { ApprovalsAuditView, QAView, SettingsNotificationsView } from './views/GovernanceViews'
import { BrowserWallView, GitWorkflowsView, ProjectsMemoryView, ProvidersView } from './views/InfraViews'

type NavTab =
  | 'dashboard'
  | 'chat'
  | 'agents'
  | 'tasks'
  | 'workspaces'
  | 'browsers'
  | 'providers'
  | 'projects'
  | 'git'
  | 'qa'
  | 'approvals'
  | 'settings'

function AATMWorkspace() {
  const { t, language, setLanguage, formatNumber } = useI18n()
  const { user, loading, login, logout, changePassword, syncPreferences } = useAuth()

  const [tab, setTab] = useState<NavTab>('dashboard')
  const [theme, setTheme] = useState<'dark' | 'light'>(() => (localStorage.getItem('aatm_theme') === 'light' ? 'light' : 'dark'))
  const [errorBanner, setErrorBanner] = useState<string | null>(null)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [authError, setAuthError] = useState<string | null>(null)

  const [overview, setOverview] = useState<any>(null)
  const [agents, setAgents] = useState<any[]>([])
  const [templates, setTemplates] = useState<any[]>([])
  const [tasks, setTasks] = useState<any[]>([])
  const [agentMessages, setAgentMessages] = useState<any[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const [memories, setMemories] = useState<any[]>([])
  const [chatMessages, setChatMessages] = useState<any[]>([])
  const [browsers, setBrowsers] = useState<any[]>([])
  const [browserCaps, setBrowserCaps] = useState<any>(null)
  const [browserEngines, setBrowserEngines] = useState<any>(null)
  const [userscripts, setUserscripts] = useState<any[]>([])
  const [providers, setProviders] = useState<any[]>([])
  const [providerUsage, setProviderUsage] = useState<any>(null)
  const [accounts, setAccounts] = useState<any[]>([])
  const [sessions, setSessions] = useState<any[]>([])
  const [devices, setDevices] = useState<any[]>([])
  const [gitData, setGitData] = useState<any>(null)
  const [qaData, setQaData] = useState<any>(null)
  const [approvals, setApprovals] = useState<any[]>([])
  const [auditLogs, setAuditLogs] = useState<any[]>([])
  const [notifications, setNotifications] = useState<any[]>([])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    document.documentElement.classList.toggle('light', theme === 'light')
    localStorage.setItem('aatm_theme', theme)
  }, [theme])

  const loadAll = useCallback(async (memQuery = '') => {
    try {
      const [ov, ag, tpl, tk, am, pr, ch, br, bc, be, us, pv, pu, ac, ss, dv, gt, qa, ap, au, nf, mm] = await Promise.all([
        apiFetch<any>('/dashboard/overview'),
        apiFetch<any[]>('/agents'),
        apiFetch<any[]>('/employees/templates'),
        apiFetch<any[]>('/tasks'),
        apiFetch<any[]>('/messages'),
        apiFetch<any[]>('/projects'),
        apiFetch<any[]>('/chat'),
        apiFetch<any[]>('/browsers'),
        apiFetch<any>('/browsers/capabilities'),
        apiFetch<any>('/browsers/engines'),
        apiFetch<any[]>('/userscripts'),
        apiFetch<any[]>('/providers'),
        apiFetch<any>('/providers/usage'),
        apiFetch<any[]>('/accounts'),
        apiFetch<any[]>('/sessions'),
        apiFetch<any[]>('/devices'),
        apiFetch<any>('/git/status'),
        apiFetch<any>('/qa/summary'),
        apiFetch<any[]>('/approvals'),
        apiFetch<any[]>('/audit'),
        apiFetch<any[]>('/notifications'),
        apiFetch<any[]>(`/memory?q=${encodeURIComponent(memQuery)}`),
      ])
      setOverview(ov)
      setAgents(ag)
      setTemplates(tpl)
      setTasks(tk)
      setAgentMessages(am)
      setProjects(pr)
      if (!activeProjectId && pr.length > 0) setActiveProjectId(pr[0].id)
      setChatMessages(ch)
      setBrowsers(br)
      setBrowserCaps(bc)
      setBrowserEngines(be)
      setUserscripts(us)
      setProviders(pv)
      setProviderUsage(pu)
      setAccounts(ac)
      setSessions(ss)
      setDevices(dv)
      setGitData(gt)
      setQaData(qa)
      setApprovals(ap)
      setAuditLogs(au)
      setNotifications(nf)
      setMemories(mm)
      setErrorBanner(null)
    } catch (err: any) {
      setErrorBanner(err?.message || t.common.loadFailed)
    }
  }, [activeProjectId])

  const loadAllRef = useRef(loadAll)
  loadAllRef.current = loadAll

  useEffect(() => {
    if (!user || user.must_change_password) return
    loadAll()
  }, [loadAll, user])

  useEffect(() => {
    if (!user || user.must_change_password) return
    const token = getToken()
    if (!token) return
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`)
    ws.onopen = () => {
      ws.send(JSON.stringify({ token }))
    }
    ws.onmessage = () => {
      loadAllRef.current()
    }
    return () => ws.close()
  }, [user])

  const handleLanguageChange = (next: Language) => {
    setLanguage(next)
    syncPreferences({ language: next })
  }

  const handleThemeToggle = (next: 'dark' | 'light') => {
    setTheme(next)
    syncPreferences({ theme: next })
  }

  const handleSignIn = async (e: React.FormEvent) => {
    e.preventDefault()
    setAuthError(null)
    try {
      await login(email.trim(), password)
      setPassword('')
      setErrorBanner(null)
    } catch (err: any) {
      setAuthError(err?.message || t.common.loginFailed)
    }
  }

  const handleForcedPasswordChange = async (e: React.FormEvent) => {
    e.preventDefault()
    setAuthError(null)
    if (newPassword.length < 12) {
      setAuthError(t.auth.passwordTooShort)
      return
    }
    if (newPassword !== confirmPassword) {
      setAuthError(t.auth.passwordMismatch)
      return
    }
    try {
      await changePassword(currentPassword, newPassword)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setAuthError(null)
    } catch (err: any) {
      setAuthError(err?.message || t.auth.passwordChangeFailed)
    }
  }

  const anyEmergencyStopped = agents.some((a) => a.is_emergency_stopped)

  const toggleEmergencyStop = async () => {
    try {
      if (anyEmergencyStopped) {
        await apiFetch('/emergency-stop/clear', { method: 'POST' })
      } else {
        await apiFetch('/emergency-stop', {
          method: 'POST',
          body: JSON.stringify({ scope: 'global', reason: t.common.emergencyStopReason }),
        })
      }
      loadAll()
    } catch (err: any) {
      setErrorBanner(err?.message || t.common.emergencyControlFailed)
    }
  }

  const navItems: Array<{ id: NavTab; label: string; icon: React.ReactNode }> = [
    { id: 'dashboard', label: t.nav.dashboard, icon: <LayoutDashboard className="h-4 w-4" /> },
    { id: 'chat', label: t.nav.chat, icon: <MessageSquare className="h-4 w-4" /> },
    { id: 'agents', label: t.nav.agents, icon: <Bot className="h-4 w-4" /> },
    { id: 'tasks', label: t.nav.tasks, icon: <ListTodo className="h-4 w-4" /> },
    { id: 'workspaces', label: t.nav.workspaces, icon: <FolderCode className="h-4 w-4" /> },
    { id: 'browsers', label: t.nav.browsers, icon: <Monitor className="h-4 w-4" /> },
    { id: 'providers', label: t.nav.providers, icon: <Cpu className="h-4 w-4" /> },
    { id: 'projects', label: t.nav.projects, icon: <Brain className="h-4 w-4" /> },
    { id: 'git', label: t.nav.git, icon: <FolderGit2 className="h-4 w-4" /> },
    { id: 'qa', label: t.nav.qa, icon: <ShieldCheck className="h-4 w-4" /> },
    { id: 'approvals', label: t.nav.approvals, icon: <CheckSquare className="h-4 w-4" /> },
    { id: 'settings', label: t.nav.settings, icon: <Bell className="h-4 w-4" /> },
  ]

  const unreadNotifs = notifications.filter((n) => !n.is_read).length

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950 text-slate-300">
        {t.common.loading}
      </div>
    )
  }

  if (!user) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-slate-950 p-4 text-slate-100">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-sky-600 font-bold text-white">AA</div>
          <div>
            <div className="text-lg font-bold tracking-tight text-white">{t.appName}</div>
            <div className="text-xs text-slate-400">{t.appSubtitle}</div>
          </div>
        </div>

        <form onSubmit={handleSignIn} className="w-full max-w-sm space-y-4 rounded-xl border border-slate-800 bg-slate-900 p-6 shadow-xl">
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-semibold text-white">{t.auth.signIn}</h1>
            <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 p-1">
              {(['en', 'ar', 'fa'] as const).map((code) => (
                <button
                  key={code}
                  type="button"
                  onClick={() => handleLanguageChange(code)}
                  className={`rounded px-2 py-0.5 text-xs font-medium ${
                    language === code ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-white'
                  }`}
                >
                  {code.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          <p className="text-xs text-slate-400">{t.auth.authRequiredHint}</p>

          {authError && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
              {authError}
            </div>
          )}

          <Input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t.auth.email}
            aria-label={t.auth.email}
          />
          <Input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t.auth.password}
            aria-label={t.auth.password}
          />
          <Button type="submit" className="w-full">
            <LogIn className="h-4 w-4" />
            {t.auth.signIn}
          </Button>
        </form>
      </div>
    )
  }

  if (user.must_change_password) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-slate-950 p-4 text-slate-100">
        <form
          onSubmit={handleForcedPasswordChange}
          className="w-full max-w-md space-y-4 rounded-xl border border-amber-500/40 bg-slate-900 p-6 shadow-xl"
        >
          <div className="flex items-center justify-between">
            <h1 className="text-lg font-semibold text-white">{t.auth.changePasswordTitle}</h1>
            <Button type="button" size="sm" variant="secondary" onClick={logout}>
              <LogOut className="h-3.5 w-3.5" />
              {t.auth.signOut}
            </Button>
          </div>
          <p className="text-xs text-slate-300">{t.auth.changePasswordSubtitle}</p>

          {authError && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
              {authError}
            </div>
          )}

          <Input
            type="password"
            autoComplete="current-password"
            required
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            placeholder={t.auth.currentPassword}
            aria-label={t.auth.currentPassword}
          />
          <Input
            type="password"
            autoComplete="new-password"
            required
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder={t.auth.newPassword}
            aria-label={t.auth.newPassword}
          />
          <Input
            type="password"
            autoComplete="new-password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            placeholder={t.auth.confirmPassword}
            aria-label={t.auth.confirmPassword}
          />
          <Button type="submit" className="w-full">
            {t.auth.updatePassword}
          </Button>
        </form>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-100">
      <aside className="hidden w-64 shrink-0 flex-col border-e border-slate-800 bg-slate-900/70 p-4 md:flex">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-600 font-bold text-white">AA</div>
          <div>
            <div className="text-base font-bold tracking-tight text-white">{t.appName}</div>
            <div className="text-[11px] text-slate-400">{t.appSubtitle}</div>
          </div>
        </div>

        <nav className="flex-1 space-y-1">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-start text-sm transition ${
                tab === item.id
                  ? 'bg-sky-600/20 font-medium text-sky-400'
                  : 'text-slate-300 hover:bg-slate-800/60 hover:text-white'
              }`}
            >
              <span className="flex items-center gap-2.5">
                {item.icon}
                {item.label}
              </span>
              {item.id === 'settings' && unreadNotifs > 0 && <Badge variant="info">{unreadNotifs}</Badge>}
            </button>
          ))}
        </nav>

        <div className="mt-4 border-t border-slate-800 pt-3 text-xs">
          <div className="space-y-2">
            <div className="truncate text-slate-400">
              {t.auth.signedInAs}: <span className="text-slate-200">{user.email}</span>
            </div>
            <Button size="sm" variant="secondary" className="w-full" onClick={logout}>
              <LogOut className="h-3.5 w-3.5" />
              {t.auth.signOut}
            </Button>
          </div>
        </div>
      </aside>

      <div className="flex flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-slate-900/50 px-5 py-3">
          <div className="flex flex-wrap items-center gap-2 md:hidden">
            {navItems.slice(0, 6).map((item) => (
              <Button
                key={item.id}
                size="sm"
                variant={tab === item.id ? 'primary' : 'secondary'}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </Button>
            ))}
          </div>

          <div className="hidden items-center gap-2 text-xs text-slate-400 md:flex">
            <Badge variant="success">
              {formatNumber(agents.length)} {t.common.employees}
            </Badge>
            <Badge variant="info">
              {formatNumber(devices.length)} {t.common.deviceNodes}
            </Badge>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant={anyEmergencyStopped ? 'secondary' : 'danger'} onClick={toggleEmergencyStop}>
              <OctagonAlert className="h-4 w-4" />
              {anyEmergencyStopped ? t.common.resumeSystem : t.common.emergencyStop}
            </Button>

            <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-950 p-1">
              <Languages className="mx-1 h-3.5 w-3.5 text-slate-400" />
              {(['en', 'ar', 'fa'] as const).map((code) => (
                <button
                  key={code}
                  onClick={() => handleLanguageChange(code)}
                  className={`rounded px-2 py-0.5 text-xs font-medium ${
                    language === code ? 'bg-sky-600 text-white' : 'text-slate-400 hover:text-white'
                  }`}
                >
                  {code.toUpperCase()}
                </button>
              ))}
            </div>

            <Button
              size="sm"
              variant="secondary"
              onClick={() => handleThemeToggle(theme === 'dark' ? 'light' : 'dark')}
              aria-label={t.settings.theme}
            >
              <SunMoon className="h-4 w-4" />
            </Button>
          </div>
        </header>

        <main className="flex-1 space-y-6 p-5">
          {errorBanner && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-950/40 px-4 py-2 text-sm text-rose-200">
              {errorBanner}
            </div>
          )}

          {tab === 'dashboard' && (
            <DashboardView
              overview={overview}
              onRefresh={() => loadAll()}
              onRunCycle={() => apiFetch('/orchestration/cycle', { method: 'POST' }).then(() => loadAll()).catch((e) => setErrorBanner(e.message))}
              onSelfHeal={() => apiFetch('/qa/self-heal', { method: 'POST' }).then(() => loadAll()).catch((e) => setErrorBanner(e.message))}
            />
          )}
          {tab === 'chat' && (
            <ManagerChatView messages={chatMessages} projectId={activeProjectId} onReload={() => loadAll()} />
          )}
          {tab === 'agents' && (
            <div className="space-y-6">
              <EmployeeCreatorAndOrgPanel agents={agents} templates={templates} onReload={() => loadAll()} />
              <AgentsView agents={agents} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'tasks' && (
            <div className="space-y-6">
              <AgentMessagingPanel agents={agents} messages={agentMessages} projectId={activeProjectId} onReload={() => loadAll()} />
              <TasksView tasks={tasks} projectId={activeProjectId} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'workspaces' && <WorkspacesView agents={agents} />}
          {tab === 'browsers' && (
            <div className="space-y-6">
              <UserscriptsEnginesPanel engines={browserEngines} userscripts={userscripts} onReload={() => loadAll()} />
              <BrowserWallView browsers={browsers} capabilities={browserCaps} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'providers' && (
            <div className="space-y-6">
              <ArenaAccountsSessionsPanel accounts={accounts} sessions={sessions} onReload={() => loadAll()} />
              <ProvidersView providers={providers} usage={providerUsage} agents={agents} sessions={sessions} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'projects' && (
            <div className="space-y-6">
              <SDLCIdeaExecutorPanel onReload={() => loadAll()} />
              <ProjectsMemoryView
                projects={projects}
                memories={memories}
                activeProjectId={activeProjectId}
                onSelectProject={setActiveProjectId}
                onReload={(q) => loadAll(q)}
              />
            </div>
          )}
          {tab === 'git' && <GitWorkflowsView gitData={gitData} approvals={approvals} agents={agents} onReload={() => loadAll()} />}
          {tab === 'qa' && (
            <div className="space-y-6">
              <QAView qaData={qaData} onReload={() => loadAll()} />
              <MultiDeviceNodesPanel devices={devices} onReload={() => loadAll()} />
              <PromptInjectionGuardPanel />
            </div>
          )}
          {tab === 'approvals' && (
            <ApprovalsAuditView approvals={approvals} auditLogs={auditLogs} onReload={() => loadAll()} />
          )}
          {tab === 'settings' && (
            <SettingsNotificationsView
              notifications={notifications}
              theme={theme}
              onToggleTheme={handleThemeToggle}
              onChangeLanguage={handleLanguageChange}
              onReload={() => loadAll()}
            />
          )}
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <I18nProvider>
      <AuthProvider>
        <AATMWorkspace />
      </AuthProvider>
    </I18nProvider>
  )
}
