/**
 * NEXUS System Administration (owner spec §11).
 *  - Settings: every runtime setting from GET /settings with its current value, the recommended value and
 *    what is actually running; edits go through PUT /settings/{key} (admin/operator, audited server side).
 *  - System logs: the audit log with search, severity filter and paging.
 *  - Security: the live security posture.
 *  - Users & permissions: the signed-in user and what each role may do. The backend has no /users API yet,
 *    so listing or inviting users is shown as unavailable instead of pretending.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { FileClock, KeyRound, Lock, Pencil, Search, Settings2, ShieldCheck, UserCog } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Badge, Button, Input, Select, Textarea } from '../../components/ui'
import { Field, fill, PageHeader, SectionCard, StatusDot, Unavailable, useAction, useRelativeTime } from '../../components/nexus'

function pretty(value: unknown) {
  try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}

function SettingRow({ name, setting, recommended, runtime, canEdit, onSaved }: {
  name: string; setting: any; recommended?: string; runtime?: React.ReactNode; canEdit: boolean; onSaved: () => void
}) {
  const { t } = useI18n()
  const ad = t.nexus.admin
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(pretty(setting.value))
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setDraft(pretty(setting.value)) }, [setting.value])

  const save = () => {
    let value: unknown
    try { value = JSON.parse(draft) } catch { setError(ad.invalidJson); return }
    setError(null)
    run(`setting:${name}`, () => apiFetch(`/settings/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ value }) }), {
      success: fill(ad.settingSaved, { key: name }), onDone: () => { setEditing(false); onSaved() },
    })
  }

  return (
    <article className="rounded-xl border border-nexus-border/70 bg-nexus-bg/40 p-4" data-testid={`setting-${name}`}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-mono text-sm font-semibold text-nexus-text" dir="ltr">{name}</h3>
          {setting.description && <p className="mt-0.5 text-xs text-nexus-muted">{setting.description}</p>}
        </div>
        <div className="flex items-center gap-2">
          {setting.updated_at && <span className="text-[11px] text-nexus-dim">{ago(setting.updated_at)}</span>}
          {canEdit && !editing && <Button size="sm" variant="secondary" onClick={() => setEditing(true)} data-testid={`setting-edit-${name}`}><Pencil className="h-3.5 w-3.5" />{t.nexus.workforce.edit}</Button>}
        </div>
      </header>
      <div className="mt-3 grid gap-3 lg:grid-cols-3">
        <div className="lg:col-span-1">
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{ad.current}</p>
          {editing ? (
            <>
              <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={6} dir="ltr" className="font-mono" aria-label={fill(ad.editSetting, { key: name })} data-testid={`setting-editor-${name}`} />
              {error && <p className="mt-1 text-xs text-nexus-error" role="alert">{error}</p>}
              <div className="mt-2 flex gap-2">
                <Button size="sm" onClick={save} disabled={isBusy(`setting:${name}`)} aria-busy={isBusy(`setting:${name}`)} data-testid={`setting-save-${name}`}>{t.nexus.common.save}</Button>
                <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setDraft(pretty(setting.value)); setError(null) }}>{t.nexus.common.cancel}</Button>
              </div>
            </>
          ) : <pre className="max-h-48 overflow-auto rounded-lg bg-nexus-bg/70 p-2 text-start text-xs text-nexus-text" dir="ltr">{pretty(setting.value)}</pre>}
        </div>
        <div>
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{ad.recommended}</p>
          <p className="text-xs text-nexus-muted">{recommended ?? '—'}</p>
        </div>
        <div>
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-nexus-dim">{ad.runtime}</p>
          <div className="text-xs text-nexus-muted">{runtime ?? <Unavailable />}</div>
        </div>
      </div>
    </article>
  )
}

export function AdminSettings({ user, health, children }: { user: any; health: any; children?: React.ReactNode }) {
  const { t, formatNumber } = useI18n()
  const ad = t.nexus.admin
  const [settings, setSettings] = useState<Record<string, any> | null>(null)
  const [scaling, setScaling] = useState<any>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    apiFetch<Record<string, any>>('/settings').then((s) => { setSettings(s); setError(null) }).catch((e) => setError(e?.message ?? 'error'))
    apiFetch<any>('/scaling/status').then(setScaling).catch(() => setScaling(null))
  }, [])
  useEffect(() => { load() }, [load])
  const canEdit = user?.role === 'admin' || user?.role === 'operator'

  const recommended: Record<string, string> = { concurrency: ad.recMaxWorkers + ' ' + ad.recStalled, budgets: ad.recBudget, autonomy: ad.recAutonomy }
  const runtime: Record<string, React.ReactNode> = {
    concurrency: scaling ? fill(ad.workersRuntime, { a: scaling.default_workers, b: scaling.extra_workers, c: scaling.parallel_slots }, formatNumber) : undefined,
    autonomy: scaling ? `scaling: ${scaling.mode}` : undefined,
    manager_provider: health?.model_status ? `${health.model_status.status} · ${(health.model_status.configured_providers ?? []).join(', ')}` : undefined,
  }

  return (
    <div className="space-y-5" data-testid="admin-settings">
      <PageHeader title={ad.title} subtitle={ad.subtitle} icon={<Settings2 className="h-6 w-6" />} />
      <SectionCard title={ad.general}>
        {error ? <p className="text-sm text-nexus-error" role="alert">{fill(t.nexus.common.failed, { reason: error })}</p>
          : settings === null ? <p className="text-sm text-nexus-muted">{t.nexus.common.loading}</p> : (
            <div className="space-y-3">
              {Object.entries(settings).map(([key, s]) => (
                <SettingRow key={key} name={key} setting={s} recommended={recommended[key]} runtime={runtime[key]} canEdit={canEdit} onSaved={load} />
              ))}
            </div>
          )}
      </SectionCard>
      <SectionCard title={ad.apiConfig} testId="admin-api">
        <dl>
          <Field label={ad.apiBase}><code dir="ltr">/api/v1</code></Field>
          <Field label={ad.apiRealtime}><code dir="ltr">/ws/events</code></Field>
          <Field label={ad.apiVersion}>{health?.version ?? <Unavailable />}</Field>
          <Field label={ad.apiEnvironment}>{health?.environment ?? <Unavailable />}</Field>
        </dl>
      </SectionCard>
      {children}
    </div>
  )
}

const PAGE = 25

export function SystemLogs({ auditLogs }: { auditLogs: any[] }) {
  const { t, formatDate, formatNumber } = useI18n()
  const ad = t.nexus.admin
  const [q, setQ] = useState('')
  const [severity, setSeverity] = useState('all')
  const [page, setPage] = useState(0)
  const rows = useMemo(() => auditLogs.filter((l) => {
    const needle = q.trim().toLowerCase()
    return (severity === 'all' || (l.severity ?? 'info') === severity)
      && (!needle || [l.action, l.actor, l.resource_type, l.resource_id].some((v) => String(v ?? '').toLowerCase().includes(needle)))
  }), [auditLogs, q, severity])
  const pages = Math.max(1, Math.ceil(rows.length / PAGE))
  const current = rows.slice(page * PAGE, page * PAGE + PAGE)
  useEffect(() => { setPage(0) }, [q, severity])
  const sevTone = (s: string) => (s === 'critical' || s === 'error' ? 'error' : s === 'warning' ? 'warning' : 'cyan') as any

  return (
    <SectionCard testId="system-logs" title={<span className="flex items-center gap-2"><FileClock className="h-4 w-4 text-nexus-cyan" />{ad.systemLogs}</span>}
      actions={<span className="text-xs text-nexus-dim">{formatNumber(rows.length)}</span>}>
      <div className="mb-3 flex flex-wrap gap-2">
        <label className="relative min-w-[12rem] flex-1 sm:max-w-xs">
          <span className="sr-only">{t.nexus.common.search}</span>
          <Search className="pointer-events-none absolute start-3 top-1/2 h-4 w-4 -translate-y-1/2 text-nexus-dim" aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.nexus.common.search} className="ps-9" data-testid="logs-search" />
        </label>
        <Select value={severity} onChange={(e) => setSeverity(e.target.value)} aria-label={t.nexus.common.filter} data-testid="logs-severity">
          <option value="all">{t.nexus.common.all}</option>
          {['info', 'warning', 'error', 'critical'].map((s) => <option key={s} value={s}>{s}</option>)}
        </Select>
      </div>
      {current.length === 0 ? <p className="text-sm text-nexus-muted">{ad.logsEmpty}</p> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[40rem] text-xs">
            <thead className="text-nexus-dim"><tr>
              <th className="py-2 text-start font-medium">{t.nexus.common.status}</th>
              <th className="py-2 text-start font-medium">{ad.colAction}</th>
              <th className="py-2 text-start font-medium">{ad.colActor}</th>
              <th className="py-2 text-start font-medium">{ad.colResource}</th>
              <th className="py-2 text-start font-medium">{ad.colTime}</th>
            </tr></thead>
            <tbody className="divide-y divide-nexus-border/50">
              {current.map((l) => (
                <tr key={l.id}>
                  <td className="py-1.5 pe-2"><StatusDot tone={sevTone(l.severity ?? 'info')} label={l.severity ?? 'info'} /></td>
                  <td className="py-1.5 pe-2 font-mono text-nexus-text" dir="ltr">{l.action}</td>
                  <td className="py-1.5 pe-2 text-nexus-muted" dir="ltr">{l.actor}</td>
                  <td className="py-1.5 pe-2 text-nexus-muted" dir="ltr">{[l.resource_type, l.resource_id?.slice?.(0, 8)].filter(Boolean).join(':')}</td>
                  <td className="py-1.5 text-nexus-dim">{l.created_at ? formatDate(l.created_at) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pages > 1 && (
        <nav className="mt-3 flex items-center justify-end gap-2 text-xs" aria-label={ad.pagination}>
          <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)} aria-label={ad.prev}><span className="rtl:rotate-180 inline-block">‹</span></Button>
          <span className="tabular-nums text-nexus-muted">{formatNumber(page + 1)} / {formatNumber(pages)}</span>
          <Button size="sm" variant="ghost" disabled={page >= pages - 1} onClick={() => setPage((p) => p + 1)} aria-label={ad.next}><span className="rtl:rotate-180 inline-block">›</span></Button>
        </nav>
      )}
    </SectionCard>
  )
}

export function SecurityOverview({ posture, children }: { posture: any; children?: React.ReactNode }) {
  const { t, formatNumber } = useI18n()
  const ad = t.nexus.admin
  return (
    <div className="space-y-4" data-testid="security-overview">
      <SectionCard title={<span className="flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-nexus-success" />{ad.securitySettings}</span>}>
        {!posture ? <Unavailable /> : (
          <dl>
            <Field label={ad.apiEnvironment}>{posture.environment}</Field>
            <Field label={t.nexus.cc.healthSandbox}><StatusDot tone={posture.sandbox?.available ? 'success' : 'error'} label={`${posture.sandbox?.status ?? '—'} · ${posture.sandbox?.mode ?? '—'}`} /></Field>
            <Field label={t.nexus.cc.healthModels}>{posture.model_status?.status ?? '—'}</Field>
            <Field label={t.nexus.cc.kpi.pendingApprovals}>{formatNumber(posture.pending_approvals ?? 0)}</Field>
            <Field label={ad.securityWarnings}>{formatNumber(posture.security_warnings_count ?? 0)}</Field>
            <Field label={ad.passwordChange}>{formatNumber(posture.users_requiring_password_change ?? 0)}</Field>
            <Field label={ad.headers}>
              <span className="flex flex-wrap gap-1">{(posture.headers_enforced ?? []).map((h: string) => <Badge key={h} variant="success" className="font-mono text-[10px]">{h}</Badge>)}</span>
            </Field>
          </dl>
        )}
      </SectionCard>
      {children}
    </div>
  )
}

/** Admin-only user list from GET /users; role and access changes go through PATCH /users/{id} (audited, signs the user out). */
function UserDirectory({ me }: { me: any }) {
  const { t } = useI18n()
  const ad = t.nexus.admin
  const { run, isBusy } = useAction()
  const [users, setUsers] = useState<any[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = useCallback(() => {
    apiFetch<any[]>('/users').then((u) => { setUsers(u); setError(null) }).catch((e) => setError(e?.message ?? 'error'))
  }, [])
  useEffect(() => { load() }, [load])
  const change = (u: any, body: Record<string, unknown>, message: string) =>
    run(`user:${u.id}`, () => apiFetch(`/users/${u.id}`, { method: 'PATCH', body: JSON.stringify(body) }), {
      confirm: { message, danger: body.is_active === false }, success: fill(ad.userSaved, { email: u.email }), onDone: load,
    })

  if (error) return <p className="mt-4 text-sm text-nexus-error" role="alert">{fill(t.nexus.common.failed, { reason: error })}</p>
  if (users === null) return <p className="mt-4 text-sm text-nexus-muted">{t.nexus.common.loading}</p>
  return (
    <ul className="mt-4 divide-y divide-nexus-border/60 rounded-lg border border-nexus-border/60" data-testid="user-list">
      {users.map((u) => {
        const self = u.id === me?.id || u.email === me?.email
        return (
          <li key={u.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm" data-testid={`user-${u.id}`}>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-nexus-text" dir="ltr">{u.email}</span>
              <span className="text-[11px] text-nexus-dim">{u.full_name}{u.is_active ? '' : ` · ${ad.userInactive}`}</span>
            </span>
            {self ? <Badge variant="info">{u.role} · {ad.you}</Badge> : (
              <>
                <Select value={u.role} disabled={isBusy(`user:${u.id}`)} aria-label={fill(ad.userRole, { email: u.email })} data-testid={`user-role-${u.id}`}
                  onChange={(e) => change(u, { role: e.target.value }, fill(ad.userRoleConfirm, { email: u.email, role: e.target.value }))}>
                  {['admin', 'operator', 'viewer'].map((r) => <option key={r} value={r}>{r}</option>)}
                </Select>
                <Button size="sm" variant={u.is_active ? 'ghost' : 'secondary'} disabled={isBusy(`user:${u.id}`)} data-testid={`user-active-${u.id}`}
                  onClick={() => change(u, { is_active: !u.is_active }, fill(u.is_active ? ad.userDeactivateConfirm : ad.userActivateConfirm, { email: u.email }))}>
                  {u.is_active ? ad.userDeactivate : ad.userActivate}
                </Button>
              </>
            )}
          </li>
        )
      })}
    </ul>
  )
}

export function UsersPermissions({ user }: { user: any }) {
  const { t } = useI18n()
  const ad = t.nexus.admin
  const roles = [
    { id: 'admin', text: ad.roleAdmin, icon: <KeyRound className="h-4 w-4" /> },
    { id: 'operator', text: ad.roleOperator, icon: <UserCog className="h-4 w-4" /> },
    { id: 'viewer', text: ad.roleViewer, icon: <Lock className="h-4 w-4" /> },
  ]
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2" data-testid="users-permissions">
      <SectionCard title={ad.users}>
        <dl>
          <Field label={ad.you}><span dir="ltr">{user?.email}</span></Field>
          <Field label={t.nexus.shell.role}><Badge variant="info">{user?.role}</Badge></Field>
        </dl>
        {user?.role === 'admin' ? <UserDirectory me={user} /> : <p className="mt-4 text-xs text-nexus-muted" role="note" data-testid="users-admin-only">{ad.usersAdminOnly}</p>}
        <p className="mt-4 text-xs text-nexus-dim">{ad.usersCreateNote}</p>
      </SectionCard>
      <SectionCard title={ad.permissions}>
        <ul className="space-y-2">
          {roles.map((r) => (
            <li key={r.id} className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-sm ${user?.role === r.id ? 'border-nexus-cyan/50 bg-nexus-cyan/5' : 'border-nexus-border/60'}`}>
              <span className="text-nexus-cyan" aria-hidden>{r.icon}</span>
              <span className="text-nexus-text">{r.text}</span>
            </li>
          ))}
        </ul>
      </SectionCard>
    </div>
  )
}
