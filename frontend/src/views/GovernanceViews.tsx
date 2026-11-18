import React from 'react'
import { Bell, Check, ShieldCheck, Sparkles, X } from 'lucide-react'
import { Badge, Button, Card } from '../components/ui'
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import type { Language } from '../i18n/types'
import { apiFetch } from '../lib/api'

export function QAView({
  qaData,
  securityPosture,
  error,
  onReload,
}: {
  qaData: any
  securityPosture?: any
  error?: string | null
  onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  if (error) return <EmptyState variant="error" onRetry={onReload} testId="qa-error" />
  if (!qaData) return <EmptyState variant="loading" testId="qa-loading" />

  const triggerHeal = async () => {
    await apiFetch('/qa/self-heal', { method: 'POST' })
    onReload()
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
          <ShieldCheck className="h-5 w-5 text-emerald-400" />
          {t.qa.title}
        </h2>
        <Button onClick={triggerHeal}>
          <Sparkles className="h-4 w-4" />
          {t.qa.triggerSelfHeal}
        </Button>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-white">{t.qa.securityScan}</span>
            <Badge variant={qaData.security_scan.status === 'passed' ? 'success' : 'warning'}>
              {qaData.security_scan.status}
            </Badge>
          </div>
          <div className="text-sm text-slate-300">
            {t.qa.filesScanned}: {formatNumber(qaData.security_scan.files_scanned)}
          </div>
          <div className="text-sm text-slate-300">
            {t.qa.findings}: {formatNumber(qaData.security_scan.findings.length)}
          </div>
        </Card>

        <Card className="space-y-2">
          <div className="font-semibold text-white">{t.dashboard.intelligence}</div>
          <ul className="space-y-1.5 text-xs text-slate-300">
            {qaData.intelligence.recommendations.map((r: string, i: number) => (
              <li key={i} className="rounded border border-slate-800 bg-slate-950/60 p-2">
                {r}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      {securityPosture && (
        <Card className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-semibold text-white">{t.qa.securityPosture}</span>
            <Badge variant="info">{securityPosture.environment}</Badge>
          </div>
          <div className="grid gap-2 text-xs text-slate-300 sm:grid-cols-3">
            <div>
              {t.qa.warningsCount}: <span className="font-semibold text-amber-300">{formatNumber(securityPosture.security_warnings_count || 0)}</span>
            </div>
            <div>
              {t.qa.pendingPasswordChanges}:{' '}
              <span className="font-semibold text-sky-300">{formatNumber(securityPosture.users_requiring_password_change || 0)}</span>
            </div>
            <div>
              {t.qa.headersEnforced}:{' '}
              <span className="font-mono text-emerald-400">
                {Array.isArray(securityPosture.headers_enforced) ? formatNumber(securityPosture.headers_enforced.length) : 0}
              </span>
            </div>
          </div>
        </Card>
      )}
    </div>
  )
}

export function ApprovalsAuditView({
  approvals,
  auditLogs,
  error,
  onReload,
}: {
  approvals: any[]
  auditLogs: any[]
  error?: string | null
  onReload: () => void
}) {
  const { t, formatDate } = useI18n()

  const decide = async (id: string, decision: 'approved' | 'rejected') => {
    await apiFetch(`/approvals/${id}/decide`, {
      method: 'POST',
      body: JSON.stringify({ decision, review_note: `info: decision=${decision}` }),
    })
    onReload()
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold text-white">{t.approvals.title}</h2>

      {error ? (
        <EmptyState variant="error" onRetry={onReload} testId="approvals-error" />
      ) : approvals.length === 0 ? (
        <EmptyState variant="empty" testId="approvals-empty" />
      ) : (
        <div className="space-y-3">
        {approvals.map((ap) => (
          <Card key={ap.id} className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <Badge variant={ap.status === 'approved' ? 'success' : ap.status === 'rejected' ? 'danger' : 'warning'}>
                  {ap.status}
                </Badge>
                <Badge variant="info">{ap.action_type}</Badge>
                <span className="font-semibold text-white">{ap.title}</span>
              </div>
              <p className="mt-1 text-xs text-slate-400">{ap.description}</p>
            </div>
            {ap.status === 'pending' && (
              <div className="flex gap-2">
                <Button size="sm" onClick={() => decide(ap.id, 'approved')}>
                  <Check className="h-3.5 w-3.5" />
                  {t.approvals.approve}
                </Button>
                <Button size="sm" variant="danger" onClick={() => decide(ap.id, 'rejected')}>
                  <X className="h-3.5 w-3.5" />
                  {t.approvals.reject}
                </Button>
              </div>
            )}
          </Card>
        ))}
        </div>
      )}

      <Card className="space-y-3">
        <h3 className="text-sm font-semibold text-slate-200">{t.approvals.auditTimeline}</h3>
        {auditLogs.length === 0 ? (
          <EmptyState variant="empty" testId="audit-empty" />
        ) : (
        <div className="max-h-72 space-y-2 overflow-y-auto text-xs">
          {auditLogs.map((lg) => (
            <div key={lg.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/60 p-2">
              <div>
                <span className="font-mono text-sky-400">{lg.action}</span>{' '}
                <span className="text-slate-400">
                  {t.common.by} {lg.actor}
                </span>
              </div>
              <span className="text-slate-500">{formatDate(lg.created_at)}</span>
            </div>
          ))}
        </div>
        )}
      </Card>
    </div>
  )
}

export function SettingsNotificationsView({
  notifications,
  theme,
  error,
  onToggleTheme,
  onChangeLanguage,
  onReload,
}: {
  notifications: any[]
  theme: 'dark' | 'light'
  error?: string | null
  onToggleTheme: (next: 'dark' | 'light') => void
  onChangeLanguage: (lang: Language) => void
  onReload: () => void
}) {
  const { t, language, formatDate } = useI18n()

  const markRead = async (id: string) => {
    await apiFetch(`/notifications/${id}/read`, { method: 'POST' })
    onReload()
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold text-white">{t.settings.title}</h2>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="space-y-3">
          <div className="text-sm font-semibold text-slate-200">{t.settings.language}</div>
          <div className="flex flex-wrap gap-2">
            {(
              [
                ['en', 'English (LTR)'],
                ['ar', 'العربية (RTL)'],
                ['fa', 'فارسی (RTL)'],
              ] as const
            ).map(([code, label]) => (
              <Button
                key={code}
                size="sm"
                variant={language === code ? 'primary' : 'secondary'}
                onClick={() => onChangeLanguage(code)}
              >
                {label}
              </Button>
            ))}
          </div>
        </Card>

        <Card className="space-y-3">
          <div className="text-sm font-semibold text-slate-200">{t.settings.theme}</div>
          <div className="flex gap-2">
            <Button size="sm" variant={theme === 'dark' ? 'primary' : 'secondary'} onClick={() => onToggleTheme('dark')}>
              {t.settings.dark}
            </Button>
            <Button size="sm" variant={theme === 'light' ? 'primary' : 'secondary'} onClick={() => onToggleTheme('light')}>
              {t.settings.light}
            </Button>
          </div>
        </Card>
      </div>

      <Card className="space-y-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-200">
          <Bell className="h-4 w-4 text-sky-400" />
          {t.settings.notifications}
        </h3>
        <div className="space-y-2">
          {error ? (
            <EmptyState variant="error" onRetry={onReload} testId="notifications-error" />
          ) : notifications.length === 0 ? (
            <EmptyState variant="empty" testId="notifications-empty" />
          ) : notifications.map((n) => (
            <div key={n.id} className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs">
              <div>
                <div className="font-semibold text-white">{n.title}</div>
                <div className="text-slate-400">{n.message}</div>
                <div className="mt-1 text-[10px] text-slate-500">{formatDate(n.created_at)}</div>
              </div>
              {!n.is_read && (
                <Button size="sm" variant="secondary" onClick={() => markRead(n.id)}>
                  {t.settings.markRead}
                </Button>
              )}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
