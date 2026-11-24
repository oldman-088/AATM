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
import { createPortal } from 'react-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { I18nProvider, useI18n } from './i18n/I18nContext'
import type { Language } from './i18n/types'
import { apiFetch, getToken } from './lib/api'
import { AgentsView, DashboardView, ManagerChatView, TasksView } from './views/CoreViews'
import { ArenaAgentDashboard, ArenaConnectorPanel } from './views/ArenaViews'
import { LIVE_EVENT, MissionControl, NeedsYouBadge, PullRequestsView } from './views/DashboardViews'
import {
  AgentMessagingPanel,
  ArenaAccountsSessionsPanel,
  EmployeeCreatorAndOrgPanel,
  TeamScalingPanel,
  MultiDeviceNodesPanel,
  PromptInjectionGuardPanel,
  SDLCIdeaExecutorPanel,
  UserscriptsEnginesPanel,
  WorkspacesView,
} from './views/EnterpriseViews'
import { ApprovalsAuditView, QAView, SettingsNotificationsView } from './views/GovernanceViews'
import { BrowserWallView, GitWorkflowsView, ProjectsMemoryView, ProvidersView } from './views/InfraViews'
import CommandBridge from './bridge/Bridge'

type NavTab =
  | 'bridge'
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

  const [tab, setTab] = useState<NavTab>('bridge')
  const [theme, setTheme] = useState<'dark' | 'light'>(() => (localStorage.getItem('aatm_theme') === 'light' ? 'light' : 'dark'))
  const [errorBanner, setErrorBanner] = useState<string | null>(null)

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [authError, setAuthError] = useState<string | null>(null)

  const [overview, setOverview] = useState<any>(null)
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  const [agents, setAgents] = useState<any[]>([])
  const [templates, setTemplates] = useState<any[]>([])
  const [tasks, setTasks] = useState<any[]>([])
  const [agentMessages, setAgentMessages] = useState<any[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const activeProject = projects.find((p: any) => p.id === activeProjectId) || null
  // Follow the project the owner marked active (the API lists it first) until the owner picks one by hand.
  // Otherwise a page that stays open keeps the project it chose at load, and orders go to the wrong project.
  const projectPickedByUser = useRef(false)
  const selectProject = useCallback((id: string | null) => { projectPickedByUser.current = true; setActiveProjectId(id) }, [])
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
  const [securityPosture, setSecurityPosture] = useState<any>(null)
  const [wsStatus, setWsStatus] = useState<'connecting' | 'live' | 'offline'>('offline')
  const [approvals, setApprovals] = useState<any[]>([])
  const [auditLogs, setAuditLogs] = useState<any[]>([])
  const [notifications, setNotifications] = useState<any[]>([])

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    document.documentElement.classList.toggle('light', theme === 'light')
    localStorage.setItem('aatm_theme', theme)
  }, [theme])

  const loadAll = useCallback(async (memQuery = '') => {
    // Load each dataset independently so one failing endpoint never blanks the
    // others. Per-dataset errors are tracked so each page can render a clear
    // Arabic error state instead of a silent loading placeholder.
    const nextErrors: Record<string, string | null> = {}
    const track = async <T,>(key: string, run: () => Promise<T>, fallback: T): Promise<T> => {
      try {
        const value = await run()
        nextErrors[key] = null
        return value
      } catch (err: any) {
        nextErrors[key] = err?.message || t.common.loadFailed
        return fallback
      }
    }

    const [ov, ag, tpl, tk, am, pr, ch, br, bc, be, us, pv, pu, ac, ss, dv, gt, qa, sp, ap, au, nf, mm] = await Promise.all([
      track('overview', () => apiFetch<any>('/dashboard/overview'), null),
      track('agents', () => apiFetch<any[]>('/agents'), []),
      track('templates', () => apiFetch<any[]>('/employees/templates'), []),
      track('tasks', () => apiFetch<any[]>('/tasks'), []),
      track('messages', () => apiFetch<any[]>('/messages'), []),
      track('projects', () => apiFetch<any[]>('/projects'), []),
      track('chat', () => apiFetch<any[]>('/chat'), []),
      track('browsers', () => apiFetch<any[]>('/browsers'), []),
      track('browserCaps', () => apiFetch<any>('/browsers/capabilities'), null),
      track('browserEngines', () => apiFetch<any[]>('/browsers/engines'), []),
      track('userscripts', () => apiFetch<any[]>('/userscripts'), []),
      track('providers', () => apiFetch<any[]>('/providers'), []),
      track('providerUsage', () => apiFetch<any>('/providers/usage'), null),
      track('accounts', () => apiFetch<any[]>('/accounts'), []),
      track('sessions', () => apiFetch<any[]>('/sessions'), []),
      track('devices', () => apiFetch<any[]>('/devices'), []),
      track('git', () => apiFetch<any>('/git/status'), null),
      track('qa', () => apiFetch<any>('/qa/summary'), null),
      track('securityPosture', () => apiFetch<any>('/governance/security-posture'), null),
      track('approvals', () => apiFetch<any[]>('/approvals'), []),
      track('audit', () => apiFetch<any[]>('/audit'), []),
      track('notifications', () => apiFetch<any[]>('/notifications'), []),
      track('memory', () => apiFetch<any[]>(`/memory?q=${encodeURIComponent(memQuery)}`), []),
    ])
    setErrors(nextErrors)
    setOverview(ov)
    setAgents(ag)
    setTemplates(tpl)
    setTasks(tk)
    setAgentMessages(am)
    setProjects(pr)
    if (pr.length > 0) {
      const stillThere = activeProjectId ? pr.some((p: any) => p.id === activeProjectId) : false
      if (!stillThere || !projectPickedByUser.current) setActiveProjectId(pr[0].id)
    }
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
    setSecurityPosture(sp)
    setApprovals(ap)
    setAuditLogs(au)
    setNotifications(nf)
    setMemories(mm)
    const failed = Object.entries(nextErrors).filter(([, v]) => v).map(([k]) => k)
    setErrorBanner(failed.length ? t.common.loadFailed : null)
  }, [activeProjectId, t.common.loadFailed])

  const loadAllRef = useRef(loadAll)
  loadAllRef.current = loadAll

  useEffect(() => {
    if (!user || user.must_change_password) return
    loadAll()
  }, [loadAll, user])

  useEffect(() => {
    if (!user || user.must_change_password) {
      setWsStatus('offline')
      return
    }
    const token = getToken()
    if (!token) {
      setWsStatus('offline')
      return
    }
    let cancelled = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | null = null

    const connect = () => {
      if (cancelled) return
      setWsStatus('connecting')
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${window.location.host}/ws/events`)
      ws.onopen = () => {
        ws?.send(JSON.stringify({ token }))
      }
      ws.onmessage = (evt) => {
        try {
          const parsed = JSON.parse(String(evt.data || '{}'))
          if (parsed?.type === 'auth.ok') {
            setWsStatus('live')
            return
          }
          window.dispatchEvent(new CustomEvent(LIVE_EVENT, { detail: parsed }))
          if (parsed?.type === 'agent.iteration' || parsed?.type === 'agent.code') {
            setWsStatus('live')
            return // streamed into the live log / command bridge; no full reload per frame
          }
        } catch {
          // non-JSON event triggers reload
        }
        setWsStatus('live')
        loadAllRef.current()
      }
      ws.onerror = () => {
        setWsStatus('offline')
      }
      ws.onclose = () => {
        if (cancelled) return
        setWsStatus('offline')
        reconnectTimer = window.setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      cancelled = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
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
      <aside className="hidden w-64 shrink-0 flex-col border-e border-slate-800 bg-slate-900/80 p-4 md:flex md:sticky md:top-0 md:h-screen">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-600 font-bold text-white">AA</div>
          <div>
            <div className="text-base font-bold tracking-tight text-white">{t.appName}</div>
            <div className="text-[11px] text-slate-400">{t.appSubtitle}</div>
          </div>
        </div>

        <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto pe-1">
          <button
            onClick={() => setTab('bridge')}
            className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-start text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 ${
              tab === 'bridge'
                ? 'bg-cyan-500/20 font-medium text-cyan-300'
                : 'text-slate-300 hover:bg-slate-800/60 hover:text-white'
            }`}
            data-testid="nav-bridge"
          >
            <span className="flex items-center gap-2.5">
              <Cpu className="h-4 w-4" />
              {t.bridge.title}
            </span>
          </button>
          <div className="mt-3 mb-1 flex items-center justify-between px-3 text-[11px] uppercase tracking-wider text-slate-500">
            <span>{t.bridge.advanced}</span>
            <span className="normal-case tracking-normal text-slate-600">{t.bridge.advancedHint}</span>
          </div>
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => setTab(item.id)}
              className={`flex w-full items-center justify-between rounded-lg px-3 py-2 text-start text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 ${
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
          <div className="flex w-full items-center gap-1.5 overflow-x-auto pb-1 md:hidden" aria-label="Mobile navigation">
            <Button size="sm" className="shrink-0" variant={tab === 'bridge' ? 'primary' : 'secondary'} onClick={() => setTab('bridge')}>
              {t.bridge.title}
            </Button>
            {navItems.map((item) => (
              <Button
                key={item.id}
                size="sm"
                className="shrink-0"
                variant={tab === item.id ? 'primary' : 'secondary'}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </Button>
            ))}
          </div>

          <div className="hidden items-center gap-2 text-xs text-slate-400 md:flex">
            <Badge variant={wsStatus === 'live' ? 'success' : wsStatus === 'connecting' ? 'warning' : 'neutral'}>
              {wsStatus === 'live' ? t.common.liveConnected : wsStatus === 'connecting' ? t.common.liveConnecting : t.common.liveOffline}
            </Badge>
            <Badge variant="success">
              {formatNumber(agents.length)} {t.common.employees}
            </Badge>
            <Badge variant="info">
              {formatNumber(devices.length)} {t.common.deviceNodes}
            </Badge>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {activeProject && <span data-testid="active-project-banner" className="max-w-[14rem] truncate rounded-lg border border-indigo-500/40 bg-indigo-500/10 px-2 py-1 text-xs text-indigo-100"
              title={activeProject.name}>{t.common.activeProject}: {activeProject.name}</span>}
            <NeedsYouBadge onOpen={() => setTab('dashboard')} />
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

        {tab === 'bridge' &&
          createPortal(
            <div className="bridge-shell" data-testid="bridge-overlay">
              <CommandBridge wsStatus={wsStatus} onOpenAdvanced={(next) => setTab((next as NavTab) || 'dashboard')} />
            </div>,
            document.body,
          )}

        <main className="flex-1 space-y-6 p-5">
          {errorBanner && (
            <div className="rounded-lg border border-rose-500/40 bg-rose-950/40 px-4 py-2 text-sm text-rose-200">
              {errorBanner}
            </div>
          )}

          {tab === 'dashboard' && <MissionControl projectId={activeProjectId} onNavigate={(next) => setTab(next as NavTab)} />}
          {tab === 'dashboard' && (
            <DashboardView
              overview={overview}
              error={errors.overview}
              onRefresh={() => loadAll()}
              onRunCycle={() => apiFetch('/orchestration/cycle', { method: 'POST' }).then(() => loadAll()).catch((e) => setErrorBanner(e.message))}
              onSelfHeal={() => apiFetch('/qa/self-heal', { method: 'POST' }).then(() => loadAll()).catch((e) => setErrorBanner(e.message))}
              onOpenProvidersWizard={() => setTab('providers')}
            />
          )}
          {tab === 'chat' && (
            <ManagerChatView messages={chatMessages} projectId={activeProjectId} projects={projects} onSelectProject={selectProject} error={errors.chat} onReload={() => loadAll()} onChatCleared={() => setChatMessages([])} />
          )}
          {tab === 'agents' && (
            <div className="space-y-6">
              <TeamScalingPanel onReload={() => loadAll()} />
              <EmployeeCreatorAndOrgPanel agents={agents} templates={templates} onReload={() => loadAll()} />
              <AgentsView agents={agents} providers={providers} error={errors.agents} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'tasks' && (
            <div className="space-y-6">
              <AgentMessagingPanel agents={agents} messages={agentMessages} projectId={activeProjectId} onReload={() => loadAll()} />
              <TasksView tasks={tasks} projectId={activeProjectId} error={errors.tasks} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'workspaces' && <WorkspacesView agents={agents} />}
          {tab === 'browsers' && (
            <div className="space-y-6">
              <ArenaAgentDashboard />
              <ArenaConnectorPanel />
              <UserscriptsEnginesPanel engines={browserEngines} userscripts={userscripts} onReload={() => loadAll()} />
              <BrowserWallView browsers={browsers} capabilities={browserCaps} error={errors.browsers} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'providers' && (
            <div className="space-y-6">
              <ProvidersView providers={providers} usage={providerUsage} agents={agents} sessions={sessions} error={errors.providers} onReload={() => loadAll()} />
              <ArenaAccountsSessionsPanel accounts={accounts} sessions={sessions} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'projects' && (
            <div className="space-y-6">
              <SDLCIdeaExecutorPanel onReload={() => loadAll()} />
              <ProjectsMemoryView
                projects={projects}
                memories={memories}
                activeProjectId={activeProjectId}
                onSelectProject={selectProject}
                onReload={(q) => loadAll(q)}
              />
            </div>
          )}
          {tab === 'git' && (
            <div className="space-y-6">
              <PullRequestsView projectId={activeProjectId} />
              <GitWorkflowsView gitData={gitData} approvals={approvals} agents={agents} error={errors.git} onReload={() => loadAll()} />
            </div>
          )}
          {tab === 'qa' && (
            <div className="space-y-6">
              <QAView qaData={qaData} securityPosture={securityPosture} error={errors.qa} onReload={() => loadAll()} />
              <MultiDeviceNodesPanel devices={devices} onReload={() => loadAll()} />
              <PromptInjectionGuardPanel />
            </div>
          )}
          {tab === 'approvals' && (
            <ApprovalsAuditView approvals={approvals} auditLogs={auditLogs} error={errors.approvals} onReload={() => loadAll()} />
          )}
          {tab === 'settings' && (
            <SettingsNotificationsView
              notifications={notifications}
              theme={theme}
              error={errors.notifications}
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
