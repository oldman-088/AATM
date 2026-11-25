import React, { useCallback, useEffect, useRef, useState } from 'react'
import { LogIn, LogOut, Zap } from 'lucide-react'
import { Button, Input } from './components/ui'
import { createPortal } from 'react-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { I18nProvider, useI18n } from './i18n/I18nContext'
import type { Language } from './i18n/types'
import { apiFetch, getToken } from './lib/api'
import { AgentsView, DashboardView, ManagerChatView, TasksView } from './views/CoreViews'
import { ArenaAgentDashboard, ArenaConnectorPanel } from './views/ArenaViews'
import { LIVE_EVENT, MissionControl, PullRequestsView } from './views/DashboardViews'
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
import { fill, NexusProvider, PageHeader, SubTabs, useConfirm, useToast } from './components/nexus'
import { LanguageSwitch, MobileNav, NexusHeader, NexusSidebar, SECTION_ICONS, useBackendHealth } from './shell/NexusShell'
import { DEFAULT_ROUTE, legacyRoute, parseHash, type Route, routeHash, SECTION_SUBS, type SectionId } from './shell/sections'
import { CommandCenter } from './views/nexus/CommandCenter'
import { WorkforceView } from './views/nexus/Workforce'
import { TaskBoard } from './views/nexus/TaskBoard'
import { OrgMap } from './views/nexus/OrgMap'
import { IntegrationsCenter } from './views/nexus/Integrations'
import { AnalyticsView } from './views/nexus/Analytics'
import { AdminSettings, SecurityOverview, SystemLogs, UsersPermissions } from './views/nexus/Admin'
import { ExecutivePlans } from './views/nexus/ExecutivePlans'

function readRoute(): Route {
  try { return window.location.hash ? parseHash(window.location.hash) : DEFAULT_ROUTE } catch { return DEFAULT_ROUTE }
}

function AATMWorkspace() {
  const { t, language, setLanguage, formatNumber } = useI18n()
  const { user, loading, login, logout, changePassword, syncPreferences } = useAuth()

  const [route, setRoute] = useState<Route>(readRoute)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [openAgentId, setOpenAgentId] = useState<string | null>(null)
  const [openTaskId, setOpenTaskId] = useState<string | null>(null)
  const confirm = useConfirm()
  const toast = useToast()
  const health = useBackendHealth()
  const navigate = useCallback((next: Route) => {
    setRoute(next)
    try {
      if (window.location.hash !== routeHash(next)) window.history.pushState(null, '', routeHash(next))
    } catch { /* sandboxed frames may refuse history changes */ }
    window.scrollTo?.({ top: 0 })
  }, [])
  const navigateLegacy = useCallback((tab: string) => navigate(legacyRoute(tab || 'dashboard')), [navigate])
  useEffect(() => {
    const onHash = () => setRoute(readRoute())
    window.addEventListener('hashchange', onHash)
    window.addEventListener('popstate', onHash)
    return () => { window.removeEventListener('hashchange', onHash); window.removeEventListener('popstate', onHash) }
  }, [])
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
    // Each dataset is shown as soon as its own request answers: a slow endpoint (for example
    // /qa/summary, which runs checks) must not keep every other screen empty.
    const track = async <T,>(key: string, run: () => Promise<T>, fallback: T, apply: (value: T) => void) => {
      let value = fallback
      try {
        value = await run()
        nextErrors[key] = null
      } catch (err: any) {
        nextErrors[key] = err?.message || t.common.loadFailed
      }
      apply(value)
    }
    const applyProjects = (pr: any[]) => {
      setProjects(pr)
      if (pr.length > 0) {
        // A completed or disabled project refuses new requests, so the default is the first active one.
        const usable = pr.filter((p: any) => (p.status ?? 'active') === 'active')
        const stillThere = activeProjectId ? pr.some((p: any) => p.id === activeProjectId) : false
        if (!stillThere || !projectPickedByUser.current) setActiveProjectId((usable[0] ?? pr[0]).id)
      }
    }

    await Promise.all([
      track('overview', () => apiFetch<any>('/dashboard/overview'), null, setOverview),
      track('agents', () => apiFetch<any[]>('/agents'), [], setAgents),
      track('templates', () => apiFetch<any[]>('/employees/templates'), [], setTemplates),
      track('tasks', () => apiFetch<any[]>('/tasks'), [], setTasks),
      track('messages', () => apiFetch<any[]>('/messages'), [], setAgentMessages),
      track('projects', () => apiFetch<any[]>('/projects'), [], applyProjects),
      track('chat', () => apiFetch<any[]>('/chat'), [], setChatMessages),
      track('browsers', () => apiFetch<any[]>('/browsers'), [], setBrowsers),
      track('browserCaps', () => apiFetch<any>('/browsers/capabilities'), null, setBrowserCaps),
      track('browserEngines', () => apiFetch<any[]>('/browsers/engines'), [], setBrowserEngines),
      track('userscripts', () => apiFetch<any[]>('/userscripts'), [], setUserscripts),
      track('providers', () => apiFetch<any[]>('/providers'), [], setProviders),
      track('providerUsage', () => apiFetch<any>('/providers/usage'), null, setProviderUsage),
      track('accounts', () => apiFetch<any[]>('/accounts'), [], setAccounts),
      track('sessions', () => apiFetch<any[]>('/sessions'), [], setSessions),
      track('devices', () => apiFetch<any[]>('/devices'), [], setDevices),
      track('git', () => apiFetch<any>('/git/status'), null, setGitData),
      track('qa', () => apiFetch<any>('/qa/summary'), null, setQaData),
      track('securityPosture', () => apiFetch<any>('/governance/security-posture'), null, setSecurityPosture),
      track('approvals', () => apiFetch<any[]>('/approvals'), [], setApprovals),
      track('audit', () => apiFetch<any[]>('/audit'), [], setAuditLogs),
      track('notifications', () => apiFetch<any[]>('/notifications'), [], setNotifications),
      track('memory', () => apiFetch<any[]>(`/memory?q=${encodeURIComponent(memQuery)}`), [], setMemories),
    ])
    setErrors(nextErrors)
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
    const n = t.nexus.shell
    if (!anyEmergencyStopped && !(await confirm({ message: n.emergencyConfirm, confirmLabel: n.emergencyStop, danger: true }))) return
    try {
      if (anyEmergencyStopped) {
        await apiFetch('/emergency-stop/clear', { method: 'POST' })
        toast.push('success', n.resumeDone)
      } else {
        await apiFetch('/emergency-stop', {
          method: 'POST',
          body: JSON.stringify({ scope: 'global', reason: t.common.emergencyStopReason }),
        })
        toast.push('warning', n.emergencyDone)
      }
      loadAll()
    } catch (err: any) {
      setErrorBanner(err?.message || t.common.emergencyControlFailed)
    }
  }

  const unreadNotifs = notifications.filter((n) => !n.is_read).length
  const pendingApprovals = approvals.filter((a) => String(a.status).toLowerCase() === 'pending').length
  const failedTasks = tasks.filter((x) => ['failed', 'blocked'].includes(String(x.status))).length
  const badges: Partial<Record<SectionId, number>> = { executive: pendingApprovals, projects: failedTasks, admin: unreadNotifs }

  const openAgent = (id: string | null) => {
    setOpenAgentId(id)
    if (id && route.section !== 'workforce') navigate({ section: 'workforce', sub: 'agents' })
  }
  const openTask = (id: string | null) => {
    setOpenTaskId(id)
    if (id && !(route.section === 'projects' && route.sub === 'tasks')) navigate({ section: 'projects', sub: 'tasks' })
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-nexus-bg text-nexus-muted" role="status">
        {t.common.loading}
      </div>
    )
  }

  if (!user) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-nexus-bg bg-[radial-gradient(ellipse_at_top,rgb(var(--nx-cyan)/0.12),transparent_60%)] p-4 text-nexus-text">
        <div className="mb-8 flex flex-col items-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-nexus-cyan to-nexus-violet text-nexus-on-accent shadow-glow">
            <Zap className="h-7 w-7" aria-hidden />
          </div>
          <div>
            <div className="font-display text-lg font-bold tracking-[0.22em] text-nexus-text">{t.nexus.brand}</div>
            <div className="mt-1 text-xs text-nexus-muted">{t.nexus.tagline}</div>
            <div className="mt-1 text-[11px] text-nexus-dim"><span className="font-semibold text-nexus-cyan">{t.appName}</span> · <span>{t.appSubtitle}</span></div>
          </div>
        </div>

        <form onSubmit={handleSignIn} className="w-full max-w-sm space-y-4 rounded-2xl border border-nexus-border bg-nexus-surface/90 p-6 shadow-card backdrop-blur">
          <div className="flex items-center justify-between gap-3">
            <h1 className="text-lg font-semibold text-nexus-text">{t.auth.signIn}</h1>
            <LanguageSwitch language={language} onChange={handleLanguageChange} />
          </div>

          <p className="text-xs text-slate-400">{t.auth.authRequiredHint}</p>

          {authError && (
            <div role="alert" className="rounded-lg border border-nexus-error/40 bg-nexus-error/10 px-3 py-2 text-xs text-nexus-error">
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
          className="w-full max-w-md space-y-4 rounded-2xl border border-nexus-warning/40 bg-nexus-surface p-6 shadow-card"
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
            <div role="alert" className="rounded-lg border border-nexus-error/40 bg-nexus-error/10 px-3 py-2 text-xs text-nexus-error">
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

  const subs = SECTION_SUBS[route.section] as string[]
  const SectionIcon = SECTION_ICONS[route.section]
  const sub = route.sub
  const subLabel = (t.nexus.sub as Record<string, string>)[sub] ?? sub
  const legacyHeader = <PageHeader title={subLabel} subtitle={t.nexus.navHint[route.section]} />
  const reload = () => loadAll()

  return (
    <div className="flex min-h-screen bg-nexus-bg text-nexus-text">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-[100] focus:rounded-lg focus:bg-nexus-cyan focus:px-3 focus:py-2 focus:text-nexus-on-accent">
        {t.nexus.shell.skipToContent}
      </a>
      <NexusSidebar route={route} onNavigate={navigate} badges={badges} user={user} onLogout={logout} />
      <MobileNav open={mobileNavOpen} onClose={() => setMobileNavOpen(false)} route={route} onNavigate={navigate} badges={badges} />

      <div className="flex min-w-0 flex-1 flex-col">
        <NexusHeader
          route={route}
          onNavigate={navigate}
          onOpenMenu={() => setMobileNavOpen(true)}
          agents={agents}
          tasks={tasks}
          projects={projects}
          notifications={notifications}
          activeProject={activeProject}
          wsStatus={wsStatus}
          health={health.state}
          user={user}
          theme={theme}
          language={language}
          emergencyStopped={anyEmergencyStopped}
          onToggleEmergency={toggleEmergencyStop}
          onToggleTheme={() => handleThemeToggle(theme === 'dark' ? 'light' : 'dark')}
          onLanguage={handleLanguageChange}
          onLogout={logout}
          onReload={reload}
          onOpenAgent={(id) => setOpenAgentId(id)}
          onOpenTask={(id) => setOpenTaskId(id)}
        />

        {route.section === 'command' && sub === 'bridge' &&
          createPortal(
            <div className="bridge-shell" data-testid="bridge-overlay">
              <CommandBridge wsStatus={wsStatus} onOpenAdvanced={(next) => navigateLegacy(next ? String(next) : 'dashboard')} />
              <button type="button" onClick={() => navigate({ section: 'command', sub: 'overview' })} data-testid="bridge-exit"
                className="fixed bottom-4 start-4 z-[2147483000] inline-flex items-center gap-2 rounded-lg border border-nexus-cyan/50 bg-nexus-surface/90 px-3 py-2 text-xs font-medium text-nexus-text shadow-glow backdrop-blur hover:bg-nexus-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
                <Zap className="h-3.5 w-3.5 text-nexus-cyan" />{t.nexus.cc.exitBridge}
              </button>
            </div>,
            document.body,
          )}

        <main id="main" className="min-w-0 flex-1 space-y-5 p-4 md:p-6" data-testid="nexus-main" data-section={route.section} data-sub={sub}>
          <div className="space-y-3">
            <p className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-nexus-dim">
              <SectionIcon className="h-3.5 w-3.5 text-nexus-cyan" />{t.nexus.nav[route.section]}
            </p>
            <SubTabs
              label={t.nexus.nav[route.section]}
              active={sub}
              onChange={(next) => navigate({ section: route.section, sub: next })}
              tabs={subs.map((id) => ({
                id,
                label: (t.nexus.sub as Record<string, string>)[id] ?? id,
                badge: id === 'approvals' ? pendingApprovals : undefined,
              }))}
            />
          </div>

          {errorBanner && (
            <div role="alert" className="rounded-lg border border-nexus-error/40 bg-nexus-error/10 px-4 py-2 text-sm text-nexus-error">
              {errorBanner}
            </div>
          )}

          <div className="animate-nx-fade-in space-y-6" key={`${route.section}/${sub}`}>
            {route.section === 'command' && (sub === 'overview' || sub === 'bridge') && (
              <CommandCenter overview={overview} agents={agents} tasks={tasks} projects={projects} approvals={approvals}
                devices={devices} health={health} onNavigate={navigate} />
            )}
            {route.section === 'command' && sub === 'mission' && (
              <>
                {legacyHeader}
                <MissionControl projectId={activeProjectId} onNavigate={navigateLegacy} />
                <DashboardView
                  overview={overview}
                  error={errors.overview}
                  onRefresh={reload}
                  onRunCycle={() => apiFetch('/orchestration/cycle', { method: 'POST' }).then(reload).catch((e) => setErrorBanner(e.message))}
                  onSelfHeal={() => apiFetch('/qa/self-heal', { method: 'POST' }).then(reload).catch((e) => setErrorBanner(e.message))}
                  onOpenProvidersWizard={() => navigate({ section: 'integrations', sub: 'providers' })}
                />
              </>
            )}

            {route.section === 'executive' && (
              <>
                <PageHeader title={t.nexus.executive.title} subtitle={t.nexus.executive.subtitle} />
                {sub === 'chat' && (
                  <ManagerChatView messages={chatMessages} projectId={activeProjectId} projects={projects} onSelectProject={selectProject}
                    error={errors.chat} onReload={reload} onChatCleared={() => setChatMessages([])} />
                )}
                {sub === 'plans' && <ExecutivePlans tasks={tasks} agents={agents} projects={projects} onNavigate={navigate} onOpenTask={openTask} />}
                {sub === 'approvals' && <ApprovalsAuditView approvals={approvals} auditLogs={auditLogs} error={errors.approvals} onReload={reload} />}
              </>
            )}

            {route.section === 'workforce' && sub === 'agents' && (
              <WorkforceView agents={agents} tasks={tasks} projects={projects} templates={templates} onReload={reload}
                openAgentId={openAgentId} onOpenAgent={setOpenAgentId} />
            )}
            {route.section === 'workforce' && sub === 'team' && (
              <>
                {legacyHeader}
                <TeamScalingPanel onReload={reload} />
                <EmployeeCreatorAndOrgPanel agents={agents} templates={templates} onReload={reload} />
                <AgentsView agents={agents} providers={providers} error={errors.agents} onReload={reload} />
              </>
            )}
            {route.section === 'workforce' && sub === 'workspaces' && (<>{legacyHeader}<WorkspacesView agents={agents} /></>)}
            {route.section === 'workforce' && sub === 'messaging' && (
              <>{legacyHeader}<AgentMessagingPanel agents={agents} messages={agentMessages} projectId={activeProjectId} onReload={reload} /></>
            )}

            {route.section === 'projects' && sub === 'tasks' && (
              <>
                <PageHeader title={t.nexus.nav.projects} subtitle={t.nexus.navHint.projects} />
                <TasksView tasks={tasks} projectId={activeProjectId} projects={projects} error={errors.tasks} onReload={reload} />
                <TaskBoard tasks={tasks} agents={agents} projects={projects} projectId={activeProjectId} onReload={reload}
                  openTaskId={openTaskId} onOpenTask={setOpenTaskId} />
              </>
            )}
            {route.section === 'projects' && sub === 'projectsList' && (
              <>
                {legacyHeader}
                <ProjectsMemoryView projects={projects} memories={memories} activeProjectId={activeProjectId}
                  onSelectProject={selectProject} onReload={(q) => loadAll(q)} />
                <SDLCIdeaExecutorPanel onReload={reload} />
              </>
            )}
            {route.section === 'projects' && sub === 'pullRequests' && (
              <>
                {legacyHeader}
                <PullRequestsView projectId={activeProjectId} />
                <GitWorkflowsView gitData={gitData} approvals={approvals} agents={agents} error={errors.git} onReload={reload} />
              </>
            )}

            {route.section === 'org' && (
              <OrgMap agents={agents} tasks={tasks} templates={templates} onOpenAgent={openAgent} onOpenTask={openTask} />
            )}

            {route.section === 'integrations' && sub === 'integrationsOverview' && (
              <IntegrationsCenter providers={providers} browsers={browsers} onNavigate={navigate} onReload={reload} />
            )}
            {route.section === 'integrations' && sub === 'providers' && (
              <>
                {legacyHeader}
                <ProvidersView providers={providers} usage={providerUsage} agents={agents} sessions={sessions} error={errors.providers} onReload={reload} />
                <ArenaAccountsSessionsPanel accounts={accounts} sessions={sessions} onReload={reload} />
              </>
            )}
            {route.section === 'integrations' && sub === 'browsers' && (
              <>
                {legacyHeader}
                <ArenaAgentDashboard />
                <ArenaConnectorPanel />
                <UserscriptsEnginesPanel engines={browserEngines} userscripts={userscripts} onReload={reload} />
                <BrowserWallView browsers={browsers} capabilities={browserCaps} error={errors.browsers} onReload={reload} />
              </>
            )}
            {route.section === 'integrations' && sub === 'devices' && (<>{legacyHeader}<MultiDeviceNodesPanel devices={devices} onReload={reload} /></>)}

            {route.section === 'analytics' && sub === 'metrics' && (
              <AnalyticsView agents={agents} tasks={tasks} projects={projects} usage={providerUsage} devices={devices} />
            )}
            {route.section === 'analytics' && sub === 'quality' && (
              <>{legacyHeader}<QAView qaData={qaData} securityPosture={securityPosture} error={errors.qa} onReload={reload} /></>
            )}

            {route.section === 'admin' && sub === 'settings' && (
              <AdminSettings user={user} health={health.data}>
                <SettingsNotificationsView
                  notifications={notifications}
                  theme={theme}
                  error={errors.notifications}
                  onToggleTheme={handleThemeToggle}
                  onChangeLanguage={handleLanguageChange}
                  onReload={reload}
                />
              </AdminSettings>
            )}
            {route.section === 'admin' && sub === 'audit' && (<>{legacyHeader}<SystemLogs auditLogs={auditLogs} /></>)}
            {route.section === 'admin' && sub === 'security' && (
              <>{legacyHeader}<SecurityOverview posture={securityPosture}><PromptInjectionGuardPanel /></SecurityOverview></>
            )}
            {route.section === 'admin' && sub === 'users' && (<>{legacyHeader}<UsersPermissions user={user} /></>)}
          </div>
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return (
    <I18nProvider>
      <AuthProvider>
        <NexusProvider>
          <AATMWorkspace />
        </NexusProvider>
      </AuthProvider>
    </I18nProvider>
  )
}
