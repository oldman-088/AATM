import React, { useCallback, useEffect, useState } from 'react'
import {
  Bell,
  Bot,
  Brain,
  CheckSquare,
  Cpu,
  FolderGit2,
  Languages,
  LayoutDashboard,
  ListTodo,
  LogIn,
  LogOut,
  MessageSquare,
  Monitor,
  ShieldCheck,
  SunMoon,
} from 'lucide-react'
import { Badge, Button, Input } from './components/ui'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { I18nProvider, useI18n } from './i18n/I18nContext'
import type { Language } from './i18n/types'
import { apiFetch } from './lib/api'
import { AgentsView, DashboardView, ManagerChatView, TasksView } from './views/CoreViews'
import { ApprovalsAuditView, QAView, SettingsNotificationsView } from './views/GovernanceViews'
import { BrowserWallView, GitWorkflowsView, ProjectsMemoryView, ProvidersView } from './views/InfraViews'

type NavTab =
  | 'dashboard'
  | 'chat'
  | 'agents'
  | 'tasks'
  | 'browsers'
  | 'providers'
  | 'projects'
  | 'git'
  | 'qa'
  | 'approvals'
  | 'settings'

function AATMWorkspace() {
  const { t, language, setLanguage } = useI18n()
  const { user, login, logout, syncPreferences } = useAuth()

  const [tab, setTab] = useState<NavTab>('dashboard')
  const [theme, setTheme] = useState<'dark' | 'light'>(() => (localStorage.getItem('aatm_theme') === 'light' ? 'light' : 'dark'))
  const [errorBanner, setErrorBanner] = useState<string | null>(null)

  // Auth form state
  const [email, setEmail] = useState('admin@aatm.local')
  const [password, setPassword] = useState('aatm-admin-2026')
  const [showAuthModal, setShowAuthModal] = useState(false)

  // Data state
  const [overview, setOverview] = useState<any>(null)
  const [agents, setAgents] = useState<any[]>([])
  const [tasks, setTasks] = useState<any[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const [memories, setMemories] = useState<any[]>([])
  const [chatMessages, setChatMessages] = useState<any[]>([])
  const [browsers, setBrowsers] = useState<any[]>([])
  const [browserCaps, setBrowserCaps] = useState<any>(null)
  const [providers, setProviders] = useState<any[]>([])
  const [providerUsage, setProviderUsage] = useState<any>(null)
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
      const [ov, ag, tk, pr, ch, br, bc, pv, pu, gt, qa, ap, au, nf, mm] = await Promise.all([
        apiFetch<any>('/dashboard/overview'),
        apiFetch<any[]>('/agents'),
        apiFetch<any[]>('/tasks'),
        apiFetch<any[]>('/projects'),
        apiFetch<any[]>('/chat'),
        apiFetch<any[]>('/browsers'),
        apiFetch<any>('/browsers/capabilities'),
        apiFetch<any[]>('/providers'),
        apiFetch<any>('/providers/usage'),
        apiFetch<any>('/git/status'),
        apiFetch<any>('/qa/summary'),
        apiFetch<any[]>('/approvals'),
        apiFetch<any[]>('/audit'),
        apiFetch<any[]>('/notifications'),
        apiFetch<any[]>(`/memory?q=${encodeURIComponent(memQuery)}`),
      ])
      setOverview(ov)
      setAgents(ag)
      setTasks(tk)
      setProjects(pr)
      if (!activeProjectId && pr.length > 0) setActiveProjectId(pr[0].id)
      setChatMessages(ch)
      setBrowsers(br)
      setBrowserCaps(bc)
      setProviders(pv)
      setProviderUsage(pu)
      setGitData(gt)
      setQaData(qa)
      setApprovals(ap)
      setAuditLogs(au)
      setNotifications(nf)
      setMemories(mm)
      setErrorBanner(null)
    } catch (err: any) {
      setErrorBanner(err?.message || 'Failed to load AATM telemetry')
    }
  }, [activeProjectId])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  // WebSocket live event subscription
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`)
    ws.onmessage = () => {
      loadAll()
    }
    return () => ws.close()
  }, [loadAll])

  const handleLanguageChange = (next: Language) => {
    setLanguage(next)
    syncPreferences({ language: next })
  }

  const handleThemeToggle = (next: 'dark' | 'light') => {
    setTheme(next)
    syncPreferences({ theme: next })
  }

  const handleSignIn = async (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    try {
      await login(email, password)
      setShowAuthModal(false)
      setErrorBanner(null)
    } catch (err: any) {
      setErrorBanner(err?.message || 'Login failed')
    }
  }

  const navItems: Array<{ id: NavTab; label: string; icon: React.ReactNode }> = [
    { id: 'dashboard', label: t.nav.dashboard, icon: <LayoutDashboard className="h-4 w-4" /> },
    { id: 'chat', label: t.nav.chat, icon: <MessageSquare className="h-4 w-4" /> },
    { id: 'agents', label: t.nav.agents, icon: <Bot className="h-4 w-4" /> },
    { id: 'tasks', label: t.nav.tasks, icon: <ListTodo className="h-4 w-4" /> },
    { id: 'browsers', label: t.nav.browsers, icon: <Monitor className="h-4 w-4" /> },
    { id: 'providers', label: t.nav.providers, icon: <Cpu className="h-4 w-4" /> },
    { id: 'projects', label: t.nav.projects, icon: <Brain className="h-4 w-4" /> },
    { id: 'git', label: t.nav.git, icon: <FolderGit2 className="h-4 w-4" /> },
    { id: 'qa', label: t.nav.qa, icon: <ShieldCheck className="h-4 w-4" /> },
    { id: 'approvals', label: t.nav.approvals, icon: <CheckSquare className="h-4 w-4" /> },
    { id: 'settings', label: t.nav.settings, icon: <Bell className="h-4 w-4" /> },
  ]

  const unreadNotifs = notifications.filter((n) => !n.is_read).length

  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-100">
      {/* Sidebar */}
      <aside className="hidden w-64 shrink-0 flex-col border-e border-slate-800 bg-slate-900/70 p-4 md:flex">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-600 font-bold text-white">
            AA
          </div>
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
          {user ? (
            <div className="space-y-2">
              <div className="truncate text-slate-400">
                {t.auth.signedInAs}: <span className="text-slate-200">{user.email}</span>
              </div>
              <Button size="sm" variant="secondary" className="w-full" onClick={logout}>
                <LogOut className="h-3.5 w-3.5" />
                {t.auth.signOut}
              </Button>
            </div>
          ) : (
            <Button size="sm" className="w-full" onClick={() => setShowAuthModal(true)}>
              <LogIn className="h-3.5 w-3.5" />
              {t.auth.signIn}
            </Button>
          )}
        </div>
      </aside>

      {/* Main Content */}
      <div className="flex flex-1 flex-col">
        {/* Topbar */}
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
            <Badge variant="success">4 Workers Online</Badge>
            <Badge variant="info">12 Divisions</Badge>
            {!user && <span className="text-amber-400">{t.auth.authRequiredHint}</span>}
          </div>

          <div className="flex items-center gap-2">
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
              aria-label="Toggle Theme"
            >
              <SunMoon className="h-4 w-4" />
            </Button>

            {!user && (
              <Button size="sm" onClick={() => handleSignIn()}>
                {t.auth.quickAdmin}
              </Button>
            )}
          </div>
        </header>

        <main className="flex-1 p-5">
          {errorBanner && (
            <div className="mb-4 rounded-lg border border-rose-500/40 bg-rose-950/40 px-4 py-2 text-sm text-rose-200">
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
          {tab === 'agents' && <AgentsView agents={agents} onReload={() => loadAll()} />}
          {tab === 'tasks' && <TasksView tasks={tasks} projectId={activeProjectId} onReload={() => loadAll()} />}
          {tab === 'browsers' && (
            <BrowserWallView browsers={browsers} capabilities={browserCaps} onReload={() => loadAll()} />
          )}
          {tab === 'providers' && (
            <ProvidersView providers={providers} usage={providerUsage} onReload={() => loadAll()} />
          )}
          {tab === 'projects' && (
            <ProjectsMemoryView
              projects={projects}
              memories={memories}
              activeProjectId={activeProjectId}
              onSelectProject={setActiveProjectId}
              onReload={(q) => loadAll(q)}
            />
          )}
          {tab === 'git' && <GitWorkflowsView gitData={gitData} onReload={() => loadAll()} />}
          {tab === 'qa' && <QAView qaData={qaData} onReload={() => loadAll()} />}
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

      {/* Sign In Modal */}
      {showAuthModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <form onSubmit={handleSignIn} className="w-full max-w-sm space-y-4 rounded-xl border border-slate-800 bg-slate-900 p-5">
            <h3 className="text-lg font-semibold text-white">{t.auth.signIn}</h3>
            <Input value={email} onChange={(e) => setEmail(e.target.value)} placeholder={t.auth.email} />
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t.auth.password}
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => setShowAuthModal(false)}>
                {t.common.cancel}
              </Button>
              <Button type="submit">{t.auth.signIn}</Button>
            </div>
          </form>
        </div>
      )}
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
