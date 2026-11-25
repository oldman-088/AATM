/**
 * NEXUS COMMAND application shell (owner spec §3): collapsible sidebar with tooltips, mobile drawer,
 * and the top header (search, system status, active workers, notifications, quick actions, clock, profile).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  BarChart3,
  Bell,
  Bot,
  Building2,
  ChevronsLeft,
  ChevronsRight,
  Clock,
  Crown,
  FolderKanban,
  LayoutDashboard,
  ListPlus,
  LogOut,
  Menu,
  MessageSquare,
  Moon,
  Network,
  OctagonAlert,
  Play,
  PlugZap,
  Search,
  Settings2,
  Sun,
  UserCircle2,
  X,
  Zap,
} from 'lucide-react'
import { apiFetch } from '../lib/api'
import { useI18n } from '../i18n/I18nContext'
import type { Language } from '../i18n/types'
import { cn } from '../components/ui'
import { fill, StatusDot, useAction, useRelativeTime } from '../components/nexus'
import { NeedsYouBadge } from '../views/DashboardViews'
import { type Route, SECTION_ORDER, type SectionId, SECTION_SUBS } from './sections'

export const SECTION_ICONS: Record<SectionId, React.ComponentType<{ className?: string }>> = {
  command: LayoutDashboard,
  executive: Crown,
  workforce: Bot,
  projects: FolderKanban,
  org: Network,
  integrations: PlugZap,
  analytics: BarChart3,
  admin: Settings2,
}

const SIDEBAR_KEY = 'nexus_sidebar_collapsed'

function readCollapsed(): boolean {
  try { return localStorage.getItem(SIDEBAR_KEY) === '1' } catch { return false }
}

/** Closes on outside click and Escape. */
function usePopover() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [open])
  return { open, setOpen, ref }
}

const iconButton = 'relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-nexus-border bg-nexus-raised/60 text-nexus-muted transition-colors hover:border-nexus-cyan/40 hover:text-nexus-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan'
const menuPanel = 'absolute end-0 top-full z-[60] mt-2 rounded-xl border border-nexus-border bg-nexus-surface p-2 shadow-card animate-nx-fade-in'

// ---------------------------------------------------------------- brand

function Brand({ compact }: { compact?: boolean }) {
  const { t } = useI18n()
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <div className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-nexus-cyan to-nexus-violet text-nexus-on-accent shadow-glow">
        <Zap className="h-5 w-5" aria-hidden />
      </div>
      {!compact && (
        <div className="min-w-0 leading-tight">
          <div className="truncate font-display text-[13px] font-bold tracking-[0.18em] text-nexus-text">{t.nexus.brand}</div>
          <div className="truncate text-[11px] text-nexus-muted">
            <span className="font-semibold text-nexus-cyan">{t.appName}</span> · <span>{t.appSubtitle}</span>
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- sidebar

function NavList({ route, onNavigate, collapsed, badges }: {
  route: Route; onNavigate: (r: Route) => void; collapsed: boolean; badges: Partial<Record<SectionId, number>>
}) {
  const { t, formatNumber } = useI18n()
  return (
    <nav aria-label={t.nexus.shell.search} className="space-y-1">
      {SECTION_ORDER.map((id) => {
        const Icon = SECTION_ICONS[id]
        const active = route.section === id
        const badge = badges[id]
        return (
          <div key={id} className="group relative">
            <button type="button" data-testid={`nav-${id}`} aria-current={active ? 'page' : undefined}
              aria-label={collapsed ? t.nexus.nav[id] : undefined}
              onClick={() => onNavigate({ section: id, sub: SECTION_SUBS[id][0] as string })}
              className={cn('relative flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-start text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan',
                collapsed && 'justify-center px-0',
                active ? 'bg-nexus-cyan/10 font-medium text-nexus-text' : 'text-nexus-muted hover:bg-nexus-raised hover:text-nexus-text')}>
              {active && <span className="absolute inset-y-1.5 start-0 w-0.5 rounded-full bg-nexus-cyan" aria-hidden />}
              <Icon className={cn('h-[18px] w-[18px] shrink-0', active ? 'text-nexus-cyan' : '')} />
              {!collapsed && <span className="min-w-0 flex-1 truncate">{t.nexus.nav[id]}</span>}
              {!!badge && (
                <span className={cn('rounded-full bg-nexus-warning/15 px-1.5 text-[11px] font-semibold text-nexus-warning',
                  collapsed && 'absolute -top-0.5 end-1')}>{formatNumber(badge)}</span>
              )}
            </button>
            {collapsed && (
              <span role="tooltip" className="pointer-events-none absolute start-full top-1/2 z-[60] ms-3 hidden w-max max-w-[16rem] -translate-y-1/2 rounded-lg border border-nexus-border bg-nexus-surface px-3 py-2 text-xs shadow-card group-hover:block group-focus-within:block">
                <span className="block font-medium text-nexus-text">{t.nexus.nav[id]}</span>
                <span className="mt-0.5 block text-nexus-muted">{t.nexus.navHint[id]}</span>
              </span>
            )}
          </div>
        )
      })}
    </nav>
  )
}

export function NexusSidebar({ route, onNavigate, badges, user, onLogout }: {
  route: Route; onNavigate: (r: Route) => void; badges: Partial<Record<SectionId, number>>; user: any; onLogout: () => void
}) {
  const { t } = useI18n()
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const toggle = () => setCollapsed((c) => {
    try { localStorage.setItem(SIDEBAR_KEY, c ? '0' : '1') } catch { /* private mode */ }
    return !c
  })
  const Chevron = collapsed ? ChevronsRight : ChevronsLeft
  return (
    <aside data-testid="nexus-sidebar" data-collapsed={collapsed ? 'true' : 'false'}
      className={cn('sticky top-0 hidden h-screen shrink-0 flex-col border-e border-nexus-border bg-nexus-surface/90 backdrop-blur transition-[width] duration-200 lg:flex',
        collapsed ? 'w-[4.5rem] px-2 py-4' : 'w-64 p-4')}>
      <div className={cn('mb-6 flex items-center', collapsed ? 'justify-center' : 'justify-between gap-2')}>
        <Brand compact={collapsed} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overflow-x-visible">
        <NavList route={route} onNavigate={onNavigate} collapsed={collapsed} badges={badges} />
      </div>
      <div className="mt-4 space-y-2 border-t border-nexus-border pt-3">
        {!collapsed && user && (
          <div className="truncate px-1 text-xs text-nexus-muted">{t.nexus.shell.signedInAs}: <span className="text-nexus-text">{user.email}</span></div>
        )}
        <div className={cn('flex gap-2', collapsed && 'flex-col items-center')}>
          <button type="button" onClick={toggle} data-testid="sidebar-toggle" aria-label={collapsed ? t.nexus.shell.expand : t.nexus.shell.collapse}
            title={collapsed ? t.nexus.shell.expand : t.nexus.shell.collapse} className={iconButton}>
            <Chevron className="h-4 w-4 rtl:rotate-180" />
          </button>
          <button type="button" onClick={onLogout} aria-label={t.auth.signOut} title={t.auth.signOut}
            className={cn(iconButton, !collapsed && 'w-auto flex-1 gap-2 px-3 text-xs')}>
            <LogOut className="h-4 w-4 rtl:rotate-180" />
            {!collapsed && t.auth.signOut}
          </button>
        </div>
      </div>
    </aside>
  )
}

export function MobileNav({ open, onClose, route, onNavigate, badges }: {
  open: boolean; onClose: () => void; route: Route; onNavigate: (r: Route) => void; badges: Partial<Record<SectionId, number>>
}) {
  const { t } = useI18n()
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-[75] bg-black/60 backdrop-blur-sm lg:hidden" onMouseDown={onClose}>
      <div role="dialog" aria-modal="true" aria-label={t.nexus.shell.openMenu} data-testid="mobile-nav"
        onMouseDown={(e) => e.stopPropagation()}
        className="flex h-full w-[min(18rem,85vw)] flex-col border-e border-nexus-border bg-nexus-surface p-4 animate-nx-fade-in">
        <div className="mb-6 flex items-center justify-between gap-2">
          <Brand />
          <button type="button" onClick={onClose} aria-label={t.nexus.shell.closeMenu} className={iconButton}><X className="h-4 w-4" /></button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <NavList route={route} onNavigate={(r) => { onNavigate(r); onClose() }} collapsed={false} badges={badges} />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- header pieces

function ClockDisplay() {
  const { language, t } = useI18n()
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 30_000)
    return () => window.clearInterval(id)
  }, [])
  const locale = language === 'ar' ? 'ar' : language === 'fa' ? 'fa-IR' : 'en-GB'
  return (
    <div className="hidden items-center gap-1.5 text-xs tabular-nums text-nexus-muted xl:flex" title={t.nexus.shell.currentTime} data-testid="header-clock">
      <Clock className="h-3.5 w-3.5" aria-hidden />
      <time dateTime={now.toISOString()}>{now.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}</time>
    </div>
  )
}

export type HealthState = 'ok' | 'degraded' | 'down' | 'unknown'

/** Polls /api/health; the header and the Command Center read the same result. */
export function useBackendHealth(intervalMs = 30_000) {
  const [health, setHealth] = useState<{ state: HealthState; data: any }>({ state: 'unknown', data: null })
  const load = useCallback(async () => {
    try {
      const data = await apiFetch<any>('/api/health')
      const degraded = data?.status !== 'ok' || data?.sandbox?.available === false || data?.model_status?.configured === false
      setHealth({ state: degraded ? 'degraded' : 'ok', data })
    } catch {
      setHealth({ state: 'down', data: null })
    }
  }, [])
  useEffect(() => {
    load()
    const id = window.setInterval(load, intervalMs)
    return () => window.clearInterval(id)
  }, [load, intervalMs])
  return health
}

function SystemStatus({ health, wsStatus }: { health: HealthState; wsStatus: 'connecting' | 'live' | 'offline' }) {
  const { t } = useI18n()
  const s = t.nexus.shell
  const tone = health === 'ok' ? 'success' : health === 'degraded' ? 'warning' : health === 'down' ? 'error' : 'muted'
  const label = health === 'ok' ? s.statusOk : health === 'degraded' ? s.statusDegraded : health === 'down' ? s.statusDown : t.nexus.common.loading
  const live = wsStatus === 'live' ? s.liveOn : wsStatus === 'connecting' ? s.liveConnecting : s.liveOff
  return (
    <div className="hidden items-center gap-3 rounded-lg border border-nexus-border bg-nexus-raised/50 px-3 py-1.5 md:flex" data-testid="system-status"
      title={`${s.systemStatus}: ${label}`}>
      <StatusDot tone={tone} pulse={health === 'ok'} label={label} />
      <span className="h-3 w-px bg-nexus-border" aria-hidden />
      <StatusDot tone={wsStatus === 'live' ? 'cyan' : wsStatus === 'connecting' ? 'warning' : 'muted'} label={live} />
    </div>
  )
}

function ActiveWorkers({ agents }: { agents: any[] }) {
  const { t, formatNumber } = useI18n()
  const workers = agents.filter((a) => a.role !== 'manager')
  const active = workers.filter((a) => ['working', 'busy', 'running'].includes(String(a.status))).length
  return (
    <div className="hidden items-center gap-1.5 rounded-lg border border-nexus-border bg-nexus-raised/50 px-3 py-1.5 text-xs text-nexus-muted sm:flex"
      data-testid="active-workers" title={fill(t.nexus.shell.activeWorkersHint, { n: active, total: workers.length }, formatNumber)}>
      <Activity className={cn('h-3.5 w-3.5', active ? 'text-nexus-cyan' : '')} aria-hidden />
      <span className="tabular-nums text-nexus-text">{fill(t.nexus.shell.activeWorkers, { n: active }, formatNumber)}</span>
      <span className="text-nexus-dim">/ {formatNumber(workers.length)}</span>
    </div>
  )
}

interface SearchHit { id: string; group: string; label: string; detail?: string; route: Route }

function GlobalSearch({ agents, tasks, projects, onNavigate, onOpenAgent, onOpenTask }: {
  agents: any[]; tasks: any[]; projects: any[]; onNavigate: (r: Route) => void
  onOpenAgent: (id: string) => void; onOpenTask: (id: string) => void
}) {
  const { t } = useI18n()
  const s = t.nexus.shell
  const [q, setQ] = useState('')
  const [index, setIndex] = useState(0)
  const pop = usePopover()
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); inputRef.current?.focus(); pop.setOpen(true) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pop])

  const hits = useMemo<SearchHit[]>(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return []
    const match = (...values: any[]) => values.some((v) => String(v ?? '').toLowerCase().includes(needle))
    const sectionHits: SearchHit[] = SECTION_ORDER.filter((id) => match(t.nexus.nav[id], t.nexus.navHint[id]))
      .map((id) => ({ id: `section:${id}`, group: s.searchSections, label: t.nexus.nav[id], route: { section: id, sub: SECTION_SUBS[id][0] as string } }))
    const agentHits: SearchHit[] = agents.filter((a) => match(a.name, a.id, a.specialty, a.role)).slice(0, 5)
      .map((a) => ({ id: `agent:${a.id}`, group: s.searchAgents, label: a.name, detail: a.id, route: { section: 'workforce', sub: 'agents' } }))
    const taskHits: SearchHit[] = tasks.filter((task) => match(task.title, task.id, task.description)).slice(0, 6)
      .map((task) => ({ id: `task:${task.id}`, group: s.searchTasks, label: task.title, detail: task.status, route: { section: 'projects', sub: 'tasks' } }))
    const projectHits: SearchHit[] = projects.filter((p) => match(p.name, p.slug, p.repo_url)).slice(0, 4)
      .map((p) => ({ id: `project:${p.id}`, group: s.searchProjects, label: p.name, detail: p.status, route: { section: 'projects', sub: 'projectsList' } }))
    return [...sectionHits, ...agentHits, ...taskHits, ...projectHits]
  }, [q, agents, tasks, projects, s, t])

  const choose = (hit: SearchHit) => {
    onNavigate(hit.route)
    const [kind, id] = hit.id.split(':')
    if (kind === 'agent') onOpenAgent(id)
    if (kind === 'task') onOpenTask(id)
    setQ('')
    pop.setOpen(false)
  }

  return (
    <div ref={pop.ref} className="relative min-w-0 flex-1 md:max-w-md">
      <label className="relative block">
        <span className="sr-only">{s.search}</span>
        <Search className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-nexus-dim" aria-hidden />
        <input ref={inputRef} type="search" value={q} data-testid="global-search" placeholder={s.searchPlaceholder}
          role="combobox" aria-expanded={pop.open && !!q} aria-controls="global-search-results" aria-autocomplete="list"
          onFocus={() => pop.setOpen(true)}
          onChange={(e) => { setQ(e.target.value); setIndex(0); pop.setOpen(true) }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setIndex((i) => Math.min(i + 1, hits.length - 1)) }
            if (e.key === 'ArrowUp') { e.preventDefault(); setIndex((i) => Math.max(i - 1, 0)) }
            if (e.key === 'Enter' && hits[index]) { e.preventDefault(); choose(hits[index]) }
          }}
          className="h-9 w-full rounded-lg border border-nexus-border bg-nexus-bg/70 pe-14 ps-9 text-sm text-nexus-text placeholder:text-nexus-dim focus:border-nexus-cyan focus:outline-none focus:ring-1 focus:ring-nexus-cyan/40" />
        <kbd className="pointer-events-none absolute end-2 top-1/2 hidden -translate-y-1/2 rounded border border-nexus-border px-1.5 text-[10px] text-nexus-dim md:block" dir="ltr">Ctrl K</kbd>
      </label>
      {pop.open && q.trim() && (
        <div id="global-search-results" role="listbox" data-testid="global-search-results" className={cn(menuPanel, 'start-0 end-auto w-full max-w-[min(32rem,calc(100vw-2rem))]')}>
          {hits.length === 0 && <p className="px-3 py-2 text-sm text-nexus-muted">{fill(s.searchEmpty, { q })}</p>}
          {hits.map((hit, i) => (
            <React.Fragment key={hit.id}>
              {(i === 0 || hits[i - 1].group !== hit.group) && <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{hit.group}</div>}
              <button type="button" role="option" aria-selected={i === index} onMouseEnter={() => setIndex(i)} onClick={() => choose(hit)}
                className={cn('flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-start text-sm', i === index ? 'bg-nexus-cyan/10 text-nexus-text' : 'text-nexus-muted')}>
                <span className="min-w-0 truncate">{hit.label}</span>
                {hit.detail && <span className="shrink-0 truncate text-xs text-nexus-dim" dir="ltr">{hit.detail}</span>}
              </button>
            </React.Fragment>
          ))}
        </div>
      )}
    </div>
  )
}

function NotificationsMenu({ notifications, onChanged, onNavigate }: { notifications: any[]; onChanged: () => void; onNavigate: (r: Route) => void }) {
  const { t, formatNumber } = useI18n()
  const s = t.nexus.shell
  const pop = usePopover()
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const unread = notifications.filter((n) => !n.is_read)
  const markRead = (id: string) => run(`read:${id}`, () => apiFetch(`/notifications/${id}/read`, { method: 'POST' }), { onDone: onChanged })
  const markAll = () => run('read:all', () => Promise.all(unread.map((n) => apiFetch(`/notifications/${n.id}/read`, { method: 'POST' }))), { onDone: onChanged })
  const tone = (sev: string) => (sev === 'critical' || sev === 'error' ? 'error' : sev === 'warning' ? 'warning' : 'cyan') as any
  return (
    <div ref={pop.ref} className="relative">
      <button type="button" className={iconButton} aria-label={`${s.notifications} (${formatNumber(unread.length)})`} aria-expanded={pop.open}
        data-testid="notifications-button" onClick={() => pop.setOpen(!pop.open)}>
        <Bell className="h-4 w-4" />
        {unread.length > 0 && (
          <span className="absolute -top-1 -end-1 min-w-[1.1rem] rounded-full bg-nexus-error px-1 text-center text-[10px] font-semibold leading-[1.1rem] text-white" data-testid="notifications-count">
            {formatNumber(Math.min(unread.length, 99))}
          </span>
        )}
      </button>
      {pop.open && (
        <div className={cn(menuPanel, 'w-[min(24rem,calc(100vw-2rem))]')} data-testid="notifications-panel">
          <div className="flex items-center justify-between gap-2 px-2 pb-2">
            <span className="text-sm font-semibold text-nexus-text">{s.notifications}</span>
            {unread.length > 0 && (
              <button type="button" onClick={markAll} disabled={isBusy('read:all')} className="text-xs text-nexus-cyan hover:underline disabled:opacity-50">{s.markAllRead}</button>
            )}
          </div>
          <ul className="max-h-[60vh] space-y-1 overflow-y-auto">
            {notifications.length === 0 && <li className="px-2 py-3 text-sm text-nexus-muted">{s.noNotifications}</li>}
            {notifications.slice(0, 20).map((n) => (
              <li key={n.id} className={cn('rounded-lg border px-3 py-2 text-start', n.is_read ? 'border-transparent opacity-70' : 'border-nexus-border bg-nexus-card')}>
                <div className="flex items-start gap-2">
                  <span className="mt-1.5"><StatusDot tone={tone(n.severity)} /></span>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-nexus-text">{n.title}</p>
                    {n.message && <p className="mt-0.5 line-clamp-3 text-xs text-nexus-muted">{n.message}</p>}
                    <div className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-nexus-dim">
                      <span>{ago(n.created_at)}</span>
                      {!n.is_read && (
                        <button type="button" onClick={() => markRead(n.id)} disabled={isBusy(`read:${n.id}`)} className="text-nexus-cyan hover:underline disabled:opacity-50">{s.markRead}</button>
                      )}
                    </div>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          <button type="button" onClick={() => { onNavigate({ section: 'admin', sub: 'settings' }); pop.setOpen(false) }}
            className="mt-2 w-full rounded-lg px-3 py-2 text-center text-xs text-nexus-cyan hover:bg-nexus-raised">{s.viewAll}</button>
        </div>
      )}
    </div>
  )
}

function QuickActions({ onNavigate, onReload }: { onNavigate: (r: Route) => void; onReload: () => void }) {
  const { t } = useI18n()
  const s = t.nexus.shell
  const pop = usePopover()
  const { run, isBusy } = useAction()
  const go = (r: Route) => { onNavigate(r); pop.setOpen(false) }
  const item = 'flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-start text-sm text-nexus-muted hover:bg-nexus-raised hover:text-nexus-text disabled:opacity-50'
  return (
    <div ref={pop.ref} className="relative">
      <button type="button" className={iconButton} aria-label={s.quickActions} title={s.quickActions} aria-expanded={pop.open}
        data-testid="quick-actions" onClick={() => pop.setOpen(!pop.open)}>
        <Zap className="h-4 w-4" />
      </button>
      {pop.open && (
        <div className={cn(menuPanel, 'w-64')} role="menu" data-testid="quick-actions-menu">
          <div className="px-3 pb-1 pt-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{s.quickActions}</div>
          <button type="button" role="menuitem" className={item} onClick={() => go({ section: 'projects', sub: 'tasks' })}><ListPlus className="h-4 w-4" />{s.quickNewTask}</button>
          <button type="button" role="menuitem" className={item} onClick={() => go({ section: 'executive', sub: 'chat' })}><MessageSquare className="h-4 w-4" />{s.quickChat}</button>
          <button type="button" role="menuitem" className={item} onClick={() => go({ section: 'command', sub: 'mission' })}><Building2 className="h-4 w-4" />{s.quickNeedsYou}</button>
          <button type="button" role="menuitem" className={item} disabled={isBusy('cycle')} data-testid="quick-cycle"
            onClick={() => run('cycle', () => apiFetch('/orchestration/cycle', { method: 'POST' }), { success: s.cycleDone, onDone: () => { onReload(); pop.setOpen(false) } })}>
            <Play className="h-4 w-4 rtl:rotate-180" />{s.quickCycle}
          </button>
        </div>
      )}
    </div>
  )
}

function ProfileMenu({ user, onLogout, onNavigate }: { user: any; onLogout: () => void; onNavigate: (r: Route) => void }) {
  const { t } = useI18n()
  const s = t.nexus.shell
  const pop = usePopover()
  return (
    <div ref={pop.ref} className="relative">
      <button type="button" className={iconButton} aria-label={s.profile} title={s.profile} aria-expanded={pop.open}
        data-testid="profile-menu" onClick={() => pop.setOpen(!pop.open)}>
        <UserCircle2 className="h-4 w-4" />
      </button>
      {pop.open && (
        <div className={cn(menuPanel, 'w-64')} role="menu">
          <div className="border-b border-nexus-border px-3 pb-2 pt-1">
            <p className="text-[11px] text-nexus-dim">{s.signedInAs}</p>
            <p className="truncate text-sm text-nexus-text" dir="ltr">{user?.email}</p>
            <p className="mt-1 text-xs text-nexus-muted">{s.role}: {user?.role}</p>
          </div>
          <button type="button" role="menuitem" onClick={() => { onNavigate({ section: 'admin', sub: 'settings' }); pop.setOpen(false) }}
            className="mt-1 flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-start text-sm text-nexus-muted hover:bg-nexus-raised hover:text-nexus-text">
            <Settings2 className="h-4 w-4" />{t.nexus.sub.settings}
          </button>
          <button type="button" role="menuitem" onClick={onLogout}
            className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-start text-sm text-nexus-error hover:bg-nexus-error/10">
            <LogOut className="h-4 w-4 rtl:rotate-180" />{t.auth.signOut}
          </button>
        </div>
      )}
    </div>
  )
}

export function LanguageSwitch({ language, onChange }: { language: Language; onChange: (l: Language) => void }) {
  const { t } = useI18n()
  return (
    <div className="flex items-center gap-0.5 rounded-lg border border-nexus-border bg-nexus-raised/50 p-0.5" role="group" aria-label={t.nexus.shell.language}>
      {(['en', 'ar', 'fa'] as const).map((code) => (
        <button key={code} type="button" onClick={() => onChange(code)} aria-pressed={language === code}
          className={cn('rounded-md px-2 py-1 text-[11px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan',
            language === code ? 'bg-nexus-cyan text-nexus-on-accent' : 'text-nexus-muted hover:text-nexus-text')}>
          {code.toUpperCase()}
        </button>
      ))}
    </div>
  )
}

export function NexusHeader(props: {
  route: Route
  onNavigate: (r: Route) => void
  onOpenMenu: () => void
  agents: any[]
  tasks: any[]
  projects: any[]
  notifications: any[]
  activeProject: any
  wsStatus: 'connecting' | 'live' | 'offline'
  health: HealthState
  user: any
  theme: 'dark' | 'light'
  language: Language
  emergencyStopped: boolean
  onToggleEmergency: () => void
  onToggleTheme: () => void
  onLanguage: (l: Language) => void
  onLogout: () => void
  onReload: () => void
  onOpenAgent: (id: string) => void
  onOpenTask: (id: string) => void
}) {
  const { t } = useI18n()
  const s = t.nexus.shell
  return (
    <header className="sticky top-0 z-40 border-b border-nexus-border bg-nexus-bg/85 backdrop-blur" data-testid="nexus-header">
      <div className="flex items-center gap-2 px-3 py-2.5 md:gap-3 md:px-5">
        <button type="button" className={cn(iconButton, 'lg:hidden')} onClick={props.onOpenMenu} aria-label={s.openMenu} data-testid="mobile-nav-button">
          <Menu className="h-4 w-4" />
        </button>
        <div className="lg:hidden"><Brand compact /></div>
        <GlobalSearch agents={props.agents} tasks={props.tasks} projects={props.projects} onNavigate={props.onNavigate}
          onOpenAgent={props.onOpenAgent} onOpenTask={props.onOpenTask} />
        <div className="ms-auto flex items-center gap-2">
          <SystemStatus health={props.health} wsStatus={props.wsStatus} />
          <ActiveWorkers agents={props.agents} />
          <ClockDisplay />
          <QuickActions onNavigate={props.onNavigate} onReload={props.onReload} />
          <NotificationsMenu notifications={props.notifications} onChanged={props.onReload} onNavigate={props.onNavigate} />
          <button type="button" className={cn(iconButton, 'hidden sm:inline-flex')} onClick={props.onToggleTheme} data-testid="theme-toggle"
            aria-label={`${s.changeTheme}: ${props.theme === 'dark' ? s.themeLight : s.themeDark}`} title={s.changeTheme}>
            {props.theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
          <ProfileMenu user={props.user} onLogout={props.onLogout} onNavigate={props.onNavigate} />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-nexus-border/60 px-3 py-2 md:px-5">
        {props.activeProject && (
          <span data-testid="active-project-banner" title={props.activeProject.name}
            className="max-w-[16rem] truncate rounded-lg border border-nexus-violet/40 bg-nexus-violet/10 px-2.5 py-1 text-xs text-nexus-text">
            {t.common.activeProject}: {props.activeProject.name}
          </span>
        )}
        <NeedsYouBadge onOpen={() => props.onNavigate({ section: 'command', sub: 'mission' })} />
        <div className="ms-auto flex items-center gap-2">
          <LanguageSwitch language={props.language} onChange={props.onLanguage} />
          <button type="button" onClick={props.onToggleEmergency} data-testid="emergency-toggle"
            className={cn('inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan',
              props.emergencyStopped ? 'border-nexus-success/40 bg-nexus-success/10 text-nexus-success hover:bg-nexus-success/20' : 'border-nexus-error/50 bg-nexus-error/10 text-nexus-error hover:bg-nexus-error/20')}>
            <OctagonAlert className="h-3.5 w-3.5" />
            {props.emergencyStopped ? t.common.resumeSystem : t.common.emergencyStop}
          </button>
        </div>
      </div>
    </header>
  )
}
