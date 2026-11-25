/**
 * NEXUS COMMAND building blocks shared by every section (owner spec §12–§14):
 * toasts, confirmation dialogs, a guarded action runner (loading, success, error, no double submit),
 * page headers, sub-tabs, stat cards, a side drawer, status dots and simple accessible charts.
 *
 * Layout uses logical properties only (ms/me/ps/pe/start/end) so AR and FA mirror correctly.
 */
import React, { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Info, Loader2, X, XCircle } from 'lucide-react'
import { useI18n } from '../i18n/I18nContext'
import { Button, cn } from './ui'

/** Replace {name} placeholders. Numbers pass through the locale formatter when one is given. */
export function fill(template: string, vars: Record<string, string | number | null | undefined> = {}, fmt?: (n: number) => string) {
  return template.replace(/\{(\w+)\}/g, (_, key) => {
    const value = vars[key]
    if (value === null || value === undefined) return ''
    return typeof value === 'number' && fmt ? fmt(value) : String(value)
  })
}

/** "5 min ago" style text from an ISO time, in the current language. */
export function useRelativeTime() {
  const { t, formatNumber } = useI18n()
  const c = t.nexus.common
  return useCallback(
    (iso?: string | null) => {
      if (!iso) return c.never
      const then = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`).getTime()
      if (Number.isNaN(then)) return c.unknown
      const minutes = Math.max(0, Math.round((Date.now() - then) / 60000))
      if (minutes < 1) return c.justNow
      if (minutes < 60) return fill(c.minutesAgo, { n: minutes }, formatNumber)
      const hours = Math.round(minutes / 60)
      if (hours < 48) return fill(c.hoursAgo, { n: hours }, formatNumber)
      return fill(c.daysAgo, { n: Math.round(hours / 24) }, formatNumber)
    },
    [c, formatNumber],
  )
}

// ---------------------------------------------------------------- toasts

type ToastKind = 'success' | 'error' | 'info' | 'warning'
interface Toast { id: number; kind: ToastKind; text: string }
interface ToastApi { push: (kind: ToastKind, text: string) => void }

const ToastContext = createContext<ToastApi | null>(null)

export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { push: () => {} }
}

function ToastViewport({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  const { t } = useI18n()
  const icon = { success: CheckCircle2, error: XCircle, info: Info, warning: AlertTriangle }
  const tone = {
    success: 'border-nexus-success/40 text-nexus-success',
    error: 'border-nexus-error/40 text-nexus-error',
    info: 'border-nexus-cyan/40 text-nexus-cyan',
    warning: 'border-nexus-warning/40 text-nexus-warning',
  }
  return (
    <div className="pointer-events-none fixed bottom-4 end-4 z-[80] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
      {toasts.map((toast) => {
        const Icon = icon[toast.kind]
        return (
          <div key={toast.id} role={toast.kind === 'error' ? 'alert' : 'status'} data-testid={`toast-${toast.kind}`}
            className={cn('pointer-events-auto flex items-start gap-3 rounded-xl border bg-nexus-surface/95 p-3 text-sm shadow-card backdrop-blur animate-nx-fade-in', tone[toast.kind])}>
            <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <p className="flex-1 text-start text-nexus-text">{toast.text}</p>
            <button type="button" onClick={() => onDismiss(toast.id)} aria-label={t.nexus.common.close}
              className="rounded p-0.5 text-nexus-muted hover:text-nexus-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- confirm dialog

interface ConfirmRequest { message: string; confirmLabel?: string; danger?: boolean }
type ConfirmFn = (request: ConfirmRequest | string) => Promise<boolean>
const ConfirmContext = createContext<ConfirmFn | null>(null)

/** Ask before a destructive or costly action. Falls back to window.confirm outside the provider. */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext)
  return ctx ?? (async (req) => window.confirm(typeof req === 'string' ? req : req.message))
}

function ConfirmDialog({ request, onAnswer }: { request: ConfirmRequest; onAnswer: (ok: boolean) => void }) {
  const { t } = useI18n()
  const titleId = useId()
  const confirmRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    confirmRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onAnswer(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onAnswer])
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onMouseDown={() => onAnswer(false)}>
      <div role="alertdialog" aria-modal="true" aria-labelledby={titleId} data-testid="confirm-dialog"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-2xl border border-nexus-border bg-nexus-surface p-5 shadow-card animate-nx-fade-in">
        <h2 id={titleId} className="flex items-center gap-2 text-base font-semibold text-nexus-text">
          <AlertTriangle className={cn('h-5 w-5', request.danger ? 'text-nexus-error' : 'text-nexus-warning')} aria-hidden />
          {t.nexus.common.confirmTitle}
        </h2>
        <p className="mt-3 text-start text-sm leading-relaxed text-nexus-muted">{request.message}</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => onAnswer(false)} data-testid="confirm-cancel">{t.nexus.common.cancel}</Button>
          <Button ref={confirmRef} variant={request.danger ? 'danger' : 'primary'} onClick={() => onAnswer(true)} data-testid="confirm-ok">
            {request.confirmLabel ?? t.nexus.common.confirm}
          </Button>
        </div>
      </div>
    </div>
  )
}

export function NexusProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)
  const dismiss = useCallback((id: number) => setToasts((list) => list.filter((x) => x.id !== id)), [])
  const push = useCallback((kind: ToastKind, text: string) => {
    const id = nextId.current++
    setToasts((list) => [...list.slice(-3), { id, kind, text }])
    window.setTimeout(() => dismiss(id), kind === 'error' ? 9000 : 4500)
  }, [dismiss])
  const toastApi = useMemo(() => ({ push }), [push])

  const [pending, setPending] = useState<{ request: ConfirmRequest; resolve: (ok: boolean) => void } | null>(null)
  const confirm = useCallback<ConfirmFn>((req) => new Promise<boolean>((resolve) => {
    setPending({ request: typeof req === 'string' ? { message: req } : req, resolve })
  }), [])
  const answer = useCallback((ok: boolean) => {
    setPending((current) => { current?.resolve(ok); return null })
  }, [])

  return (
    <ToastContext.Provider value={toastApi}>
      <ConfirmContext.Provider value={confirm}>
        {children}
        {pending && <ConfirmDialog request={pending.request} onAnswer={answer} />}
        <ToastViewport toasts={toasts} onDismiss={dismiss} />
      </ConfirmContext.Provider>
    </ToastContext.Provider>
  )
}

// ---------------------------------------------------------------- guarded actions

interface ActionOptions {
  success?: string
  confirm?: ConfirmRequest | string
  onDone?: () => void
}

/**
 * Run a backend call with one in-flight guard per key: a second click while it runs is ignored,
 * success and failure become toasts, and the caller's refresh runs only after success.
 */
export function useAction() {
  const { t } = useI18n()
  const toast = useToast()
  const confirm = useConfirm()
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const inFlight = useRef<Set<string>>(new Set())

  const run = useCallback(
    async <T,>(key: string, fn: () => Promise<T>, options: ActionOptions = {}): Promise<T | undefined> => {
      if (inFlight.current.has(key)) return undefined
      if (options.confirm && !(await confirm(options.confirm))) return undefined
      inFlight.current.add(key)
      setBusy((b) => ({ ...b, [key]: true }))
      try {
        const result = await fn()
        if (options.success) toast.push('success', options.success)
        options.onDone?.()
        return result
      } catch (err) {
        toast.push('error', fill(t.nexus.common.failed, { reason: err instanceof Error ? err.message : String(err) }))
        return undefined
      } finally {
        inFlight.current.delete(key)
        setBusy((b) => ({ ...b, [key]: false }))
      }
    },
    [confirm, t, toast],
  )
  return { run, busy, isBusy: (key: string) => !!busy[key] }
}

// ---------------------------------------------------------------- layout pieces

export function PageHeader({ title, subtitle, actions, icon }: {
  title: string; subtitle?: string; actions?: React.ReactNode; icon?: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight text-nexus-text md:text-2xl">
          {icon && <span className="text-nexus-cyan" aria-hidden>{icon}</span>}
          {title}
        </h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-nexus-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function SectionCard({ title, actions, children, className, testId, bodyClassName }: {
  title?: React.ReactNode; actions?: React.ReactNode; children: React.ReactNode; className?: string; testId?: string; bodyClassName?: string
}) {
  return (
    <section data-testid={testId} className={cn('rounded-xl border border-nexus-border/80 bg-nexus-card shadow-card', className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-nexus-border/70 px-4 py-3">
          {title && <h2 className="text-sm font-semibold text-nexus-text">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

export interface SubTab { id: string; label: string; badge?: number }

/** Accessible tab strip: arrow keys move between tabs (mirrored in RTL). */
export function SubTabs({ tabs, active, onChange, label }: { tabs: SubTab[]; active: string; onChange: (id: string) => void; label: string }) {
  const { isRtl, formatNumber } = useI18n()
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const onKey = (e: React.KeyboardEvent, index: number) => {
    const forward = isRtl ? 'ArrowLeft' : 'ArrowRight'
    const back = isRtl ? 'ArrowRight' : 'ArrowLeft'
    let next = -1
    if (e.key === forward) next = (index + 1) % tabs.length
    if (e.key === back) next = (index - 1 + tabs.length) % tabs.length
    if (e.key === 'Home') next = 0
    if (e.key === 'End') next = tabs.length - 1
    if (next >= 0) {
      e.preventDefault()
      onChange(tabs[next].id)
      refs.current[tabs[next].id]?.focus()
    }
  }
  if (tabs.length < 2) return null
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b border-nexus-border/70" data-testid="sub-tabs">
      {tabs.map((tab, index) => {
        const selected = tab.id === active
        return (
          <button key={tab.id} ref={(el) => { refs.current[tab.id] = el }} role="tab" type="button" aria-selected={selected}
            tabIndex={selected ? 0 : -1} data-testid={`subtab-${tab.id}`} onKeyDown={(e) => onKey(e, index)} onClick={() => onChange(tab.id)}
            className={cn('-mb-px flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-nexus-cyan',
              selected ? 'border-nexus-cyan font-medium text-nexus-text' : 'border-transparent text-nexus-muted hover:text-nexus-text')}>
            {tab.label}
            {!!tab.badge && <span className="rounded-full bg-nexus-warning/15 px-1.5 text-[11px] font-semibold text-nexus-warning">{formatNumber(tab.badge)}</span>}
          </button>
        )
      })}
    </div>
  )
}

type Tone = 'cyan' | 'violet' | 'success' | 'warning' | 'error' | 'muted'
const toneText: Record<Tone, string> = {
  cyan: 'text-nexus-cyan', violet: 'text-nexus-violet', success: 'text-nexus-success',
  warning: 'text-nexus-warning', error: 'text-nexus-error', muted: 'text-nexus-muted',
}
const toneBg: Record<Tone, string> = {
  cyan: 'bg-nexus-cyan/10', violet: 'bg-nexus-violet/10', success: 'bg-nexus-success/10',
  warning: 'bg-nexus-warning/10', error: 'bg-nexus-error/10', muted: 'bg-nexus-raised',
}

export function StatCard({ label, value, hint, icon, tone = 'cyan', status, onClick, testId }: {
  label: string; value: React.ReactNode; hint?: React.ReactNode; icon: React.ReactNode; tone?: Tone
  status?: React.ReactNode; onClick?: () => void; testId?: string
}) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag type={onClick ? 'button' : undefined} onClick={onClick} data-testid={testId}
      className={cn('group flex min-w-0 flex-col gap-3 rounded-xl border border-nexus-border/80 bg-nexus-card p-4 text-start shadow-card transition-colors',
        onClick && 'hover:border-nexus-cyan/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan')}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-medium text-nexus-muted">{label}</span>
        <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-lg', toneBg[tone], toneText[tone])} aria-hidden>{icon}</span>
      </div>
      <div className="flex items-end justify-between gap-2">
        <span className="text-2xl font-semibold tabular-nums text-nexus-text" data-testid={testId ? `${testId}-value` : undefined}>{value}</span>
        {status}
      </div>
      {hint && <span className="truncate text-xs text-nexus-dim">{hint}</span>}
    </Tag>
  )
}

export function StatusDot({ tone, pulse, label }: { tone: Tone; pulse?: boolean; label?: string }) {
  const bg: Record<Tone, string> = {
    cyan: 'bg-nexus-cyan', violet: 'bg-nexus-violet', success: 'bg-nexus-success',
    warning: 'bg-nexus-warning', error: 'bg-nexus-error', muted: 'bg-nexus-dim',
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn('h-2 w-2 shrink-0 rounded-full', bg[tone], pulse && 'animate-nx-pulse')} aria-hidden />
      {label && <span className="text-xs text-nexus-muted">{label}</span>}
    </span>
  )
}

export function Unavailable({ text, className }: { text?: string; className?: string }) {
  const { t } = useI18n()
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-xs text-nexus-dim', className)} data-testid="data-unavailable">
      <Info className="h-3.5 w-3.5" aria-hidden />
      {text ?? t.nexus.common.dataUnavailable}
    </span>
  )
}

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span role="status" className={cn('inline-flex items-center gap-2 text-sm text-nexus-muted', className)}>
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </span>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('animate-pulse rounded-lg bg-nexus-raised', className)} aria-hidden />
}

/** Side panel on the inline-end edge; Escape and the backdrop close it. */
export function Drawer({ open, title, onClose, children, testId, wide }: {
  open: boolean; title: React.ReactNode; onClose: () => void; children: React.ReactNode; testId?: string; wide?: boolean
}) {
  const { t } = useI18n()
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    panelRef.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-[70] flex justify-end bg-black/50 backdrop-blur-[2px]" onMouseDown={onClose}>
      <div ref={panelRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid={testId}
        onMouseDown={(e) => e.stopPropagation()}
        className={cn('flex h-full w-full flex-col border-s border-nexus-border bg-nexus-surface shadow-card focus:outline-none animate-nx-fade-in', wide ? 'max-w-2xl' : 'max-w-lg')}>
        <header className="flex items-center justify-between gap-3 border-b border-nexus-border px-5 py-4">
          <h2 id={titleId} className="min-w-0 truncate text-base font-semibold text-nexus-text">{title}</h2>
          <button type="button" onClick={onClose} aria-label={t.nexus.common.close} data-testid="drawer-close"
            className="rounded-lg p-1.5 text-nexus-muted hover:bg-nexus-raised hover:text-nexus-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan">
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
      </div>
    </div>
  )
}

/** Small key/value row for detail panels. */
export function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(7rem,38%)_1fr] gap-3 border-b border-nexus-border/50 py-2 text-sm last:border-b-0">
      <dt className="text-nexus-muted">{label}</dt>
      <dd className="min-w-0 break-words text-nexus-text">{children}</dd>
    </div>
  )
}

/** Horizontal bars; each row reads "label — value". Width is relative to the largest value. */
export function BarList({ rows, empty, testId }: {
  rows: Array<{ id: string; label: string; value: number; secondary?: number; display?: string }>; empty?: string; testId?: string
}) {
  const { formatNumber, t } = useI18n()
  const max = Math.max(1, ...rows.map((r) => r.value + (r.secondary ?? 0)))
  if (!rows.length) return <Unavailable text={empty ?? t.nexus.common.dataUnavailable} />
  return (
    <ul className="space-y-2.5" data-testid={testId}>
      {rows.map((row) => (
        <li key={row.id} className="text-xs">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="truncate text-nexus-text">{row.label}</span>
            <span className="shrink-0 tabular-nums text-nexus-muted">{row.display ?? formatNumber(row.value)}</span>
          </div>
          <div className="flex h-2 overflow-hidden rounded-full bg-nexus-raised" aria-hidden>
            <div className="h-full bg-nexus-cyan" style={{ width: `${(row.value / max) * 100}%` }} />
            {!!row.secondary && <div className="h-full bg-nexus-error/80" style={{ width: `${(row.secondary / max) * 100}%` }} />}
          </div>
        </li>
      ))}
    </ul>
  )
}

/** Percentage ring (SVG). `value` is 0–100 or null for unavailable data. */
export function Ring({ value, label, tone = 'cyan', testId }: { value: number | null; label: string; tone?: Tone; testId?: string }) {
  const { formatNumber, t } = useI18n()
  const stroke: Record<Tone, string> = {
    cyan: 'stroke-nexus-cyan', violet: 'stroke-nexus-violet', success: 'stroke-nexus-success',
    warning: 'stroke-nexus-warning', error: 'stroke-nexus-error', muted: 'stroke-nexus-dim',
  }
  const r = 30
  const circumference = 2 * Math.PI * r
  const pct = value === null ? 0 : Math.max(0, Math.min(100, value))
  return (
    <figure className="flex flex-col items-center gap-2" data-testid={testId}>
      <svg viewBox="0 0 80 80" className="h-24 w-24 -rotate-90" role="img" aria-label={`${label}: ${value === null ? t.nexus.common.dataUnavailable : `${formatNumber(Math.round(pct))}%`}`}>
        <circle cx="40" cy="40" r={r} fill="none" className="stroke-nexus-raised" strokeWidth="8" />
        <circle cx="40" cy="40" r={r} fill="none" className={stroke[tone]} strokeWidth="8" strokeLinecap="round"
          strokeDasharray={circumference} strokeDashoffset={circumference * (1 - pct / 100)} />
      </svg>
      <figcaption className="text-center">
        <div className="text-lg font-semibold tabular-nums text-nexus-text">{value === null ? '—' : `${formatNumber(Math.round(pct))}%`}</div>
        <div className="text-xs text-nexus-muted">{label}</div>
      </figcaption>
    </figure>
  )
}

/** Map backend task/agent states to the six workforce states of the spec. */
export function agentState(agent: any): 'working' | 'idle' | 'waiting' | 'blocked' | 'failed' | 'offline' | 'paused' {
  const status = String(agent?.status ?? '').toLowerCase()
  if (agent?.is_enabled === false) return 'paused'
  if (agent?.is_emergency_stopped) return 'blocked'
  if (['working', 'busy', 'running', 'in_progress'].includes(status)) return 'working'
  if (['needs_human', 'blocked', 'stalled'].includes(status)) return 'blocked'
  if (['waiting', 'waiting_connection', 'queued', 'assigned'].includes(status)) return 'waiting'
  if (['failed', 'error', 'crashed'].includes(status)) return 'failed'
  if (['offline', 'disconnected', 'stopped'].includes(status)) return 'offline'
  return 'idle'
}

export const STATE_TONE: Record<ReturnType<typeof agentState>, Tone> = {
  working: 'cyan', idle: 'success', waiting: 'violet', blocked: 'warning', failed: 'error', offline: 'muted', paused: 'muted',
}
