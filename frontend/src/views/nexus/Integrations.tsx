/**
 * NEXUS Integrations Center (owner spec §9). One card per category with connection status, configuration status,
 * last successful connection and the error, plus "Test connection" and "Configure".
 * Test buttons call a real endpoint (provider health check, or a fresh read of the status endpoint);
 * no key, token or password is ever requested or shown.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Boxes, Bot, Database, GitBranch, Globe2, MonitorSmartphone, PlugZap, Radio, Server, ShieldCheck, Workflow } from 'lucide-react'
import { apiFetch } from '../../lib/api'
import { useI18n } from '../../i18n/I18nContext'
import { Badge, Button } from '../../components/ui'
import { fill, PageHeader, StatusDot, Unavailable, useAction, useRelativeTime } from '../../components/nexus'
import type { Route } from '../../shell/sections'

type Conn = 'connected' | 'partial' | 'down' | 'unknown'

interface IntegrationCard {
  id: string
  icon: React.ReactNode
  title: string
  connection: Conn
  configured: boolean | null
  detail: React.ReactNode
  lastSuccess?: string | null
  error?: string | null
  test?: () => Promise<unknown>
  configure?: Route
}

function connBadge(conn: Conn, t: any) {
  const i = t.nexus.integrations
  if (conn === 'connected') return <Badge variant="success">{i.connected}</Badge>
  if (conn === 'partial') return <Badge variant="warning">{i.partial}</Badge>
  if (conn === 'down') return <Badge variant="danger">{i.notConnected}</Badge>
  return <Badge variant="neutral">{t.nexus.common.dataUnavailable}</Badge>
}

function Card({ card, onNavigate, onTested }: { card: IntegrationCard; onNavigate: (r: Route) => void; onTested: () => void }) {
  const { t } = useI18n()
  const i = t.nexus.integrations
  const ago = useRelativeTime()
  const { run, isBusy } = useAction()
  const testing = isBusy(`test:${card.id}`)
  return (
    <article className="flex flex-col rounded-xl border border-nexus-border/80 bg-nexus-card p-4 shadow-card" data-testid={`integration-${card.id}`}>
      <header className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-nexus-cyan/10 text-nexus-cyan" aria-hidden>{card.icon}</span>
          <h3 className="truncate text-sm font-semibold text-nexus-text">{card.title}</h3>
        </div>
        <StatusDot tone={card.connection === 'connected' ? 'success' : card.connection === 'partial' ? 'warning' : card.connection === 'down' ? 'error' : 'muted'}
          pulse={card.connection === 'connected'} />
      </header>
      <p className="mt-3 min-h-[2.5rem] text-xs text-nexus-muted">{card.detail}</p>
      <dl className="mt-3 space-y-1.5 text-xs">
        <div className="flex items-center justify-between gap-2"><dt className="text-nexus-dim">{i.connection}</dt><dd>{connBadge(card.connection, t)}</dd></div>
        <div className="flex items-center justify-between gap-2"><dt className="text-nexus-dim">{i.configuration}</dt>
          <dd>{card.configured === null ? <Unavailable /> : <Badge variant={card.configured ? 'success' : 'warning'}>{card.configured ? i.configured : i.notConfigured}</Badge>}</dd></div>
        <div className="flex items-center justify-between gap-2"><dt className="text-nexus-dim">{i.lastSuccess}</dt>
          <dd className="text-nexus-muted">{card.lastSuccess ? ago(card.lastSuccess) : <Unavailable />}</dd></div>
        {card.error && <div className="rounded-md border border-nexus-error/30 bg-nexus-error/5 p-2 text-nexus-error" role="note"><span className="font-medium">{i.error}: </span>{card.error}</div>}
      </dl>
      <div className="mt-auto flex flex-wrap gap-2 pt-4">
        {card.test ? (
          <Button size="sm" variant="secondary" disabled={testing} aria-busy={testing} data-testid={`integration-test-${card.id}`}
            onClick={() => run(`test:${card.id}`, card.test!, { success: `${card.title}: ${i.testOk}`, onDone: onTested })}>
            <Radio className="h-3.5 w-3.5" />{testing ? i.testing : i.test}
          </Button>
        ) : <span className="text-[11px] text-nexus-dim">{i.noTest}</span>}
        {card.configure && (
          <Button size="sm" variant="ghost" onClick={() => onNavigate(card.configure!)} data-testid={`integration-configure-${card.id}`}>{i.configure}</Button>
        )}
      </div>
    </article>
  )
}

export function IntegrationsCenter({ providers, browsers, onNavigate, onReload }: {
  providers: any[]; browsers: any[]; onNavigate: (r: Route) => void; onReload: () => void
}) {
  const { t, formatNumber } = useI18n()
  const i = t.nexus.integrations
  const cat = i.categories
  const [scaling, setScaling] = useState<any>(undefined)
  const [connector, setConnector] = useState<any>(undefined)
  const [github, setGithub] = useState<any>(undefined)
  const [sandbox, setSandbox] = useState<any>(undefined)

  const loadScaling = useCallback(() => apiFetch<any>('/scaling/status').then(setScaling).catch(() => setScaling(null)), [])
  const loadConnector = useCallback(() => apiFetch<any>('/arena/connector/status').then(setConnector).catch(() => setConnector(null)), [])
  const loadGithub = useCallback(() => apiFetch<any>('/github/summary').then(setGithub).catch(() => setGithub(null)), [])
  const loadSandbox = useCallback(() => apiFetch<any>('/sandbox/status').then(setSandbox).catch(() => setSandbox(null)), [])
  useEffect(() => { loadScaling(); loadConnector(); loadGithub(); loadSandbox() }, [loadScaling, loadConnector, loadGithub, loadSandbox])

  const strict = async <T,>(p: Promise<T>, ok: (v: T) => boolean, reason: (v: T) => string) => {
    const v = await p
    if (!ok(v)) throw new Error(reason(v))
    return v
  }

  const enabledProviders = providers.filter((p) => p.is_enabled !== false)
  const healthyProviders = enabledProviders.filter((p) => p.health_status === 'healthy')
  const lastProviderCheck = providers.map((p) => p.last_checked_at).filter(Boolean).sort().pop() ?? null
  const runningBrowsers = browsers.filter((b) => ['running', 'active', 'ready'].includes(String(b.status)))
  const profiles: any[] = connector?.bridge?.profiles ?? []
  const connected = profiles.filter((p) => p.connected)
  const arenaLastSeen = connected.length ? new Date(Date.now() - Math.min(...connected.map((p) => Number(p.last_seen_seconds_ago ?? 0))) * 1000).toISOString() : null
  const external = providers.filter((p) => p.endpoint_url)
  const ratio = (a: number, b: number): Conn => (b === 0 ? 'unknown' : a === b ? 'connected' : a === 0 ? 'down' : 'partial')

  const cards: IntegrationCard[] = [
    {
      id: 'ai', icon: <Bot className="h-5 w-5" />, title: cat.ai,
      connection: ratio(healthyProviders.length, enabledProviders.length),
      configured: providers.length ? providers.some((p) => p.credential_status === 'configured' || p.provider_type?.startsWith('arena')) : false,
      detail: fill(i.detailProviders, { a: healthyProviders.length, b: enabledProviders.length }, formatNumber),
      lastSuccess: lastProviderCheck,
      error: enabledProviders.filter((p) => p.health_status !== 'healthy').map((p) => p.name).join(', ') || null,
      test: () => apiFetch('/providers/health-check', { method: 'POST' }),
      configure: { section: 'integrations', sub: 'providers' },
    },
    {
      id: 'browser', icon: <MonitorSmartphone className="h-5 w-5" />, title: cat.browser,
      connection: ratio(runningBrowsers.length, browsers.length), configured: browsers.length > 0,
      detail: fill(i.detailBrowsers, { a: runningBrowsers.length, b: browsers.length }, formatNumber),
      lastSuccess: browsers.map((b) => b.last_active_at ?? b.updated_at).filter(Boolean).sort().pop() ?? null,
      configure: { section: 'integrations', sub: 'browsers' },
    },
    {
      id: 'arena', icon: <Globe2 className="h-5 w-5" />, title: cat.arena,
      connection: connector === undefined ? 'unknown' : connector === null ? 'down' : ratio(connected.length, profiles.length),
      configured: connector ? !!connector.enabled : connector === null ? false : null,
      detail: connector ? fill(i.detailArena, { a: connected.length, b: profiles.length }, formatNumber) : <Unavailable />,
      lastSuccess: arenaLastSeen,
      error: connector?.message ?? (profiles.filter((p) => !p.connected).map((p) => p.agent_id ?? p.profile_id).join(', ') || null),
      test: () => strict(apiFetch<any>('/arena/connector/status'), (v) => !!v?.bridge?.ok, (v) => v?.message ?? 'bridge unreachable').then((v) => setConnector(v)),
      configure: { section: 'integrations', sub: 'browsers' },
    },
    {
      id: 'github', icon: <GitBranch className="h-5 w-5" />, title: cat.github,
      connection: github === undefined ? 'unknown' : github?.auth?.authenticated ? 'connected' : 'down',
      configured: github === undefined ? null : !!github?.auth?.authenticated,
      detail: github?.auth?.authenticated ? i.detailGithub : i.detailGithubOff,
      test: () => strict(apiFetch<any>('/github/summary'), (v) => !!v?.auth?.authenticated, () => i.detailGithubOff).then((v) => setGithub(v)),
      configure: { section: 'projects', sub: 'pullRequests' },
    },
    {
      id: 'docker', icon: <Boxes className="h-5 w-5" />, title: cat.docker,
      connection: sandbox === undefined ? 'unknown' : sandbox?.available ? 'connected' : 'down',
      configured: sandbox === undefined ? null : !!sandbox?.image,
      detail: sandbox ? fill(i.detailSandbox, { mode: sandbox.mode ?? '—', image: sandbox.image ?? '—' }) : <Unavailable />,
      error: sandbox?.reason ?? null,
      test: () => strict(apiFetch<any>('/sandbox/status'), (v) => !!v?.available, (v) => v?.reason ?? v?.status ?? 'unavailable').then((v) => setSandbox(v)),
    },
    {
      id: 'database', icon: <Database className="h-5 w-5" />, title: cat.database,
      connection: scaling === undefined ? 'unknown' : scaling ? 'connected' : 'down', configured: scaling ? true : null,
      detail: scaling ? <>{fill(i.detailDatabase, { backend: scaling.database_backend })}{scaling.sqlite_warning && <span className="mt-1 block text-nexus-warning">{i.sqliteWarning}</span>}</> : <Unavailable />,
      test: () => strict(apiFetch<any>('/scaling/status'), (v) => !!v?.database_backend, () => 'no database').then((v) => setScaling(v)),
    },
    {
      id: 'redis', icon: <Server className="h-5 w-5" />, title: cat.redis,
      connection: scaling === undefined ? 'unknown' : scaling?.coordination?.backend === 'redis' ? 'connected' : scaling ? 'partial' : 'down',
      configured: scaling ? scaling.coordination?.backend === 'redis' : null,
      detail: scaling ? fill(i.detailRedis, { backend: scaling.coordination?.backend ?? '—' }) : <Unavailable />,
      test: () => apiFetch<any>('/scaling/status').then((v) => setScaling(v)),
    },
    {
      id: 'rabbitmq', icon: <Workflow className="h-5 w-5" />, title: cat.rabbitmq,
      connection: scaling === undefined ? 'unknown' : scaling?.queue?.backend === 'rabbitmq' ? ((scaling.queue.dead_lettered ?? 0) > 0 ? 'partial' : 'connected') : scaling ? 'partial' : 'down',
      configured: scaling ? scaling.queue?.configured_backend === 'rabbitmq' : null,
      detail: scaling ? fill(i.detailQueue, { backend: scaling.queue?.backend ?? '—', n: scaling.queue?.published ?? 0 }, formatNumber) : <Unavailable />,
      error: scaling && scaling.queue?.configured_backend && scaling.queue.configured_backend !== scaling.queue.backend
        ? `${scaling.queue.configured_backend} → ${scaling.queue.backend}` : null,
      test: () => apiFetch<any>('/scaling/status').then((v) => setScaling(v)),
    },
    {
      id: 'external', icon: <PlugZap className="h-5 w-5" />, title: cat.external,
      connection: ratio(external.filter((p) => p.health_status === 'healthy').length, external.length), configured: external.length > 0,
      detail: fill(i.detailExternal, { n: external.length }, formatNumber),
      lastSuccess: external.map((p) => p.last_checked_at).filter(Boolean).sort().pop() ?? null,
      test: () => apiFetch('/providers/health-check', { method: 'POST' }),
      configure: { section: 'integrations', sub: 'providers' },
    },
  ]

  return (
    <div className="space-y-5" data-testid="integrations-center">
      <PageHeader title={i.title} subtitle={i.subtitle} icon={<PlugZap className="h-6 w-6" />} />
      <p className="flex items-center gap-2 rounded-lg border border-nexus-border/70 bg-nexus-card px-3 py-2 text-xs text-nexus-muted">
        <ShieldCheck className="h-4 w-4 shrink-0 text-nexus-success" aria-hidden />{i.secretsHidden}
      </p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {cards.map((card) => <Card key={card.id} card={card} onNavigate={onNavigate} onTested={() => { onReload(); loadScaling(); loadConnector() }} />)}
      </div>
    </div>
  )
}
