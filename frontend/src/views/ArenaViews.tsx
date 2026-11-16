import React, { useEffect, useState } from 'react'
import { KeyRound, PlayCircle, RefreshCw } from 'lucide-react'
import { Badge, Button, Card } from '../components/ui'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'

const DOT: Record<string, string> = { green: 'bg-emerald-400', yellow: 'bg-amber-400', red: 'bg-red-500' }

export function connectorLabel(t: any, status: string | null | undefined): string {
  switch (status) {
    case 'ready': return t.arena.statusReady
    case 'busy': return t.arena.statusBusy
    case 'needs_human': return t.arena.statusNeedsHuman
    default: return t.arena.statusOffline
  }
}

export function ArenaConnectorPanel() {
  const { t } = useI18n()
  const [wall, setWall] = useState<any>(null)
  const [script, setScript] = useState<{ profile: string; text: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const load = async () => {
    try { setWall(await apiFetch<any>('/arena/profiles')) } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) }
  }
  useEffect(() => { load() }, [])
  const gate = wall?.gate
  const generate = async (profileId: string) => {
    setBusy(profileId); setError(''); setCopied(false)
    try {
      const data = await apiFetch<any>(`/arena/profiles/${profileId}/userscript`, { method: 'POST' })
      setScript({ profile: profileId, text: data.script }); await load()
    } catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  const resume = async (profileId: string) => {
    setBusy(profileId); setError('')
    try { await apiFetch<any>(`/arena/profiles/${profileId}/resume`, { method: 'POST' }); await load() }
    catch (err: any) { setError(err?.message || t.enterprise.operationFailed) } finally { setBusy('') }
  }
  const copy = async () => {
    if (!script) return
    try { await navigator.clipboard.writeText(script.text); setCopied(true) } catch { setCopied(false) }
  }
  return <section data-testid="arena-connector-panel"><Card className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold text-white">{t.arena.title}</h3>
      <Button variant="secondary" onClick={load}><RefreshCw className="me-1 h-4 w-4" />{t.common.refresh}</Button>
    </div>
    {gate && <div role="status" data-testid="arena-gate" className={`rounded-lg border p-2 text-start text-xs ${gate.real_enabled ? 'border-emerald-500/40 text-emerald-200' : gate.mode === 'mock' ? 'border-sky-500/40 text-sky-200' : 'border-amber-500/40 bg-amber-500/10 text-amber-200'}`}>
      {gate.real_enabled ? t.arena.approved : gate.mode === 'mock' ? t.arena.mockMode : t.arena.waiting}
    </div>}
    <p className="text-xs text-slate-500">{t.arena.runUserscriptDisabled}</p>
    <div className="grid gap-2 md:grid-cols-2">
      {(wall?.profiles || []).length === 0 && <p className="text-xs text-slate-400">{t.arena.noProfiles}</p>}
      {(wall?.profiles || []).map((p: any) => <div key={p.profile_id} data-testid={`arena-profile-${p.profile_id}`} className="rounded-lg border border-slate-700 p-2" style={{ borderInlineStartColor: p.color, borderInlineStartWidth: 4 }}>
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm text-white">{p.agent_name}</span>
          <span className="flex items-center gap-1 text-xs text-slate-300">
            <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${DOT[p.health] || DOT.yellow}`} />
            {connectorLabel(t, p.connector_status)}
          </span>
        </div>
        {p.connector_status === 'needs_human' && <p className="mt-1 text-xs text-red-300"><Badge variant="danger">{t.arena.needsHuman}</Badge> {p.needs_human_reason}</p>}
        <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="secondary" disabled={!gate?.enabled || busy === p.profile_id} onClick={() => generate(p.profile_id)}>
            <KeyRound className="me-1 h-4 w-4" />{p.token_issued ? t.arena.rotate : t.arena.generate}
          </Button>
          {p.connector_status === 'needs_human' && <Button disabled={busy === p.profile_id} onClick={() => resume(p.profile_id)}><PlayCircle className="me-1 h-4 w-4" />{t.arena.resume}</Button>}
        </div>
      </div>)}
    </div>
    {script && <div className="space-y-2" data-testid="arena-script">
      <p className="text-xs text-amber-200">{t.arena.review} {t.arena.tokenOnce}</p>
      <textarea readOnly dir="ltr" aria-label={t.arena.review} className="h-48 w-full rounded-lg bg-slate-950 p-2 font-mono text-xs text-slate-200" value={script.text} />
      <Button variant="secondary" onClick={copy}>{copied ? t.arena.copied : t.arena.copy}</Button>
    </div>}
    {error && <p className="text-xs text-red-300">{error}</p>}
  </Card></section>
}
