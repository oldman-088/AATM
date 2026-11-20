import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Phone,
  RefreshCw,
  Search,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import { Badge, Button, Card, Input, Progress } from '../components/ui'
import { EmptyState } from '../components/EmptyState'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'
import { AvatarFallbackIcon, PlatformIcon } from '../lookups/PlatformIcon'
import {
  COUNTRIES,
  avatarProxyUrl,
  composeE164,
  countryName,
  isValidE164,
  jobProgress,
  mapProviderResult,
  summarizeResults,
  toAsciiDigits,
  type CountryOption,
  type SocialResult,
} from '../lookups/phone'
import type { LookupCreateResponse, LookupJobDetail } from '../lookups/types'

/** Polling cadence while a job is `queued` / `running`. */
export const POLL_INTERVAL_MS = 3000

const LOCALES = { en: 'en-US', ar: 'ar-EG', fa: 'fa-IR' } as const

/** Locale-aware percentage, so Arabic/Persian counters use their own numerals. */
function percentText(ratio: number, language: 'en' | 'ar' | 'fa'): string {
  return new Intl.NumberFormat(LOCALES[language], { style: 'percent', maximumFractionDigits: 0 }).format(ratio)
}

function useElapsedSeconds(startIso: string | null, active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  if (!startIso) return 0
  const started = new Date(startIso).getTime()
  if (Number.isNaN(started)) return 0
  return Math.max(0, Math.round((now - started) / 1000))
}

/** Round avatar rendered through our own origin (CSP: `img-src 'self'`). */
function ResultAvatar({ result }: { result: SocialResult }) {
  const { t } = useI18n()
  const [failed, setFailed] = useState(false)
  const name = result.displayName ?? result.platform
  if (!result.avatarUrl || failed) {
    return (
      <div
        className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full border border-slate-700 bg-slate-800 text-slate-400"
        role="img"
        aria-label={t.lookup.avatarFallback}
        data-testid={`avatar-fallback-${result.id}`}
      >
        <AvatarFallbackIcon />
      </div>
    )
  }
  return (
    <img
      src={avatarProxyUrl(result.avatarUrl)}
      alt={t.lookup.avatarAlt.replace('{name}', name).replace('{platform}', result.platform)}
      loading="lazy"
      width={48}
      height={48}
      onError={() => setFailed(true)}
      className="h-12 w-12 shrink-0 rounded-full border border-slate-700 bg-slate-800 object-cover"
    />
  )
}

function ConfidenceMeter({ value }: { value: number }) {
  const { t, language } = useI18n()
  const percent = Math.round(value * 100)
  const localeTag = language === 'ar' ? 'ar-EG' : language === 'fa' ? 'fa-IR' : 'en-US'
  const percentText = new Intl.NumberFormat(localeTag, { style: 'percent', maximumFractionDigits: 0 }).format(value)
  const tone = percent >= 70 ? 'success' : percent >= 40 ? 'warning' : 'danger'
  return (
    <div className="flex items-center gap-2" data-testid="confidence-meter">
      <span className="text-xs text-slate-400">{t.lookup.confidence}</span>
      <Badge variant={tone}>
        <span aria-label={t.lookup.confidenceValue.replace('{value}', percentText)}>{percentText}</span>
      </Badge>
      {/* The badge is the accessible value; the bar is decorative redundancy. */}
      <div className="hidden h-1.5 w-16 overflow-hidden rounded-full bg-slate-800 sm:block" aria-hidden="true">
        <div
          className={`h-full rounded-full ${percent >= 70 ? 'bg-emerald-400' : percent >= 40 ? 'bg-amber-400' : 'bg-rose-400'}`}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  )
}

function ResultCard({ result }: { result: SocialResult }) {
  const { t, formatNumber } = useI18n()
  const headingId = `result-${result.id}-heading`
  const notFound = result.state === 'not_found'
  const failed = result.state === 'error'

  return (
    <Card
      className={`space-y-3 ${notFound ? 'border-dashed opacity-90' : ''} ${failed ? 'border-rose-500/40' : ''}`}
      data-testid={`result-card-${result.id}`}
    >
      <div className="flex items-start gap-3">
        <ResultAvatar result={result} />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <PlatformIcon platformKey={result.platformKey} className="h-4 w-4 text-sky-400" />
            <h3 id={headingId} className="truncate text-sm font-semibold text-white">
              {result.platform}
            </h3>
            {result.state === 'found' && (
              <Badge variant="success">
                <CheckCircle2 className="me-1 h-3 w-3" aria-hidden="true" />
                {t.lookup.foundCount}
              </Badge>
            )}
            {notFound && (
              <Badge variant="neutral" data-testid={`not-found-${result.id}`}>
                <XCircle className="me-1 h-3 w-3" aria-hidden="true" />
                {t.lookup.notFound}
              </Badge>
            )}
            {failed && (
              <Badge variant="danger">
                <AlertCircle className="me-1 h-3 w-3" aria-hidden="true" />
                {t.lookup.providerUnavailable}
              </Badge>
            )}
          </div>

          {result.state === 'found' && (
            <p className="truncate text-sm text-slate-200">{result.displayName ?? t.lookup.noDetails}</p>
          )}
          {notFound && <p className="text-xs text-slate-400">{t.lookup.notFoundHint}</p>}
          {failed && <p className="text-xs text-rose-300">{t.lookup.providerUnavailableHint}</p>}
        </div>
      </div>

      {result.state === 'found' && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          {result.confidence !== null ? <ConfidenceMeter value={result.confidence} /> : <span />}
          {result.profileUrl && (
            <a
              href={result.profileUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-describedby={headingId}
              className="inline-flex items-center gap-1 rounded-lg border border-sky-500/50 bg-sky-600/20 px-2.5 py-1 text-xs font-medium text-sky-200 transition hover:bg-sky-600/30"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
              {t.lookup.openProfile}
            </a>
          )}
        </div>
      )}

      {(result.latencyMs !== null || result.errorMessage) && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
          {result.latencyMs !== null && (
            <span>
              {result.status} · {formatNumber(Math.round(result.latencyMs))} ms
            </span>
          )}
          {result.errorMessage && <span className="truncate">{result.errorMessage}</span>}
        </div>
      )}

      {result.attributes.length > 0 && (
        <details className="text-xs text-slate-400">
          <summary className="cursor-pointer select-none">{t.lookup.details}</summary>
          <dl className="mt-2 grid gap-1 sm:grid-cols-2">
            {result.attributes.map((attribute) => (
              <div key={attribute.key} className="flex min-w-0 gap-1.5">
                <dt className="shrink-0 text-slate-500">{attribute.key}:</dt>
                <dd className="truncate text-slate-300">{attribute.value}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </Card>
  )
}

function AggregateSummary({ job }: { job: LookupJobDetail }) {
  const { t, formatNumber } = useI18n()
  const aggregate = job.result
  if (!aggregate) return <p className="text-xs text-slate-400">{t.lookup.noAggregate}</p>
  const fields: Array<{ label: string; value: string }> = []
  if (aggregate.display_name) fields.push({ label: t.lookup.aggregateTitle, value: aggregate.display_name })
  if (aggregate.carrier) fields.push({ label: t.lookup.carrier, value: aggregate.carrier })
  if (aggregate.line_type) fields.push({ label: t.lookup.lineType, value: aggregate.line_type })
  if (aggregate.country) fields.push({ label: t.lookup.country, value: aggregate.country })
  if (aggregate.spam_score !== null && aggregate.spam_score !== undefined) {
    fields.push({ label: t.lookup.spamScore, value: formatNumber(Math.round(aggregate.spam_score)) })
  }
  if (fields.length === 0 && !aggregate.is_spam) {
    return <p className="text-xs text-slate-400">{t.lookup.noAggregate}</p>
  }
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-300" data-testid="aggregate-summary">
      {fields.map((field) => (
        <span key={field.label}>
          <span className="text-slate-500">{field.label}: </span>
          <span className="font-medium text-slate-200">{field.value}</span>
        </span>
      ))}
      {aggregate.is_spam && (
        <Badge variant="danger">
          <ShieldAlert className="me-1 h-3 w-3" aria-hidden="true" />
          {t.lookup.spamFlagged}
        </Badge>
      )}
    </div>
  )
}

/**
 * Phone-number lookup screen.
 *
 * Flow: `POST /api/v1/lookups` with the E.164 number, then poll
 * `GET /api/v1/lookups/{job_id}` until the job reaches `completed` / `failed`.
 * One card per provider row shows the picture, the name, the profile link, the
 * confidence and the `not_found` state. Every string comes from the i18n
 * dictionaries, and layout uses logical (start/end) classes so the page mirrors
 * correctly in Arabic and Persian.
 */
export function PhoneLookupView({ pollIntervalMs = POLL_INTERVAL_MS }: { pollIntervalMs?: number } = {}) {
  const { t, isRtl, formatNumber, language } = useI18n()

  const [countryIso, setCountryIso] = useState('SA')
  const [national, setNational] = useState('')
  const [job, setJob] = useState<LookupJobDetail | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [invalidPhone, setInvalidPhone] = useState(false)

  const pollTimer = useRef<number | null>(null)

  const country: CountryOption = useMemo(
    () => COUNTRIES.find((option) => option.iso === countryIso) ?? COUNTRIES[0],
    [countryIso],
  )
  const e164 = composeE164(country, national)
  // A number typed with + / 00 carries its own prefix — say so instead of
  // pretending the selected country is being used.
  const countryIgnored = /^\s*(\+|00)/.test(toAsciiDigits(national))
  const inFlight = submitting || job?.status === 'queued' || job?.status === 'running'
  const elapsed = useElapsedSeconds(job?.started_at ?? job?.created_at ?? null, Boolean(inFlight))

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  useEffect(() => stopPolling, [stopPolling])

  // Poll while the job is not finalized; stop as soon as it settles.
  useEffect(() => {
    if (!job || (job.status !== 'queued' && job.status !== 'running')) {
      stopPolling()
      return
    }
    const jobId = job.job_id
    stopPolling()
    pollTimer.current = window.setInterval(async () => {
      try {
        const detail = await apiFetch<LookupJobDetail>(`/lookups/${jobId}`)
        setJob(detail)
      } catch (err: any) {
        setError(err?.message || t.lookup.timeout)
        stopPolling()
      }
    }, pollIntervalMs)
    return stopPolling
  }, [job?.job_id, job?.status, pollIntervalMs, stopPolling, t.lookup.timeout])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    const candidate = composeE164(country, national)
    if (!isValidE164(candidate)) {
      setInvalidPhone(true)
      return
    }
    setInvalidPhone(false)
    setSubmitting(true)
    try {
      const created = await apiFetch<LookupCreateResponse>('/lookups', {
        method: 'POST',
        body: JSON.stringify({ phone: candidate }),
      })
      setJob({
        job_id: created.job_id,
        status: created.status,
        phone_e164: created.phone_e164,
        phone_raw: created.phone_raw,
        created_at: created.created_at,
        updated_at: created.created_at,
        started_at: null,
        completed_at: null,
        error_message: null,
        result: null,
        provider_results: [],
      })
    } catch (err: any) {
      setError(err?.message || t.lookup.timeout)
    } finally {
      setSubmitting(false)
    }
  }

  const reset = () => {
    stopPolling()
    setJob(null)
    setNational('')
    setError(null)
    setInvalidPhone(false)
  }

  const results = useMemo(
    () => (job?.provider_results ?? []).map(mapProviderResult),
    [job?.provider_results],
  )
  const summary = summarizeResults(results)
  const progress = jobProgress(job)
  const progressText = percentText(progress / 100, language)

  const statusLabel =
    job?.status === 'completed'
      ? t.lookup.statusCompleted
      : job?.status === 'failed'
        ? t.lookup.statusFailed
        : job?.status === 'running'
          ? t.lookup.statusRunning
          : job?.status === 'queued'
            ? t.lookup.statusQueued
            : ''

  return (
    <div className="space-y-6" data-testid="phone-lookup-view">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="flex items-center gap-2 text-xl font-semibold text-white">
            <Phone className="h-5 w-5 text-sky-400" aria-hidden="true" />
            {t.lookup.title}
          </h2>
          <p className="max-w-2xl text-sm text-slate-400">{t.lookup.subtitle}</p>
        </div>
        {isRtl && <Badge variant="info">RTL</Badge>}
      </header>

      <Card>
        <form onSubmit={submit} className="space-y-4" aria-label={t.lookup.title}>
          <div className="grid gap-3 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
            <div className="space-y-1">
              <label htmlFor="lookup-country" className="block text-xs font-medium text-slate-300">
                {t.lookup.country}
              </label>
              <select
                id="lookup-country"
                value={countryIso}
                onChange={(event) => setCountryIso(event.target.value)}
                aria-describedby="lookup-country-hint"
                disabled={countryIgnored}
                className="w-full rounded-lg border border-slate-700 bg-slate-950/70 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
              >
                {COUNTRIES.map((option) => (
                  <option key={option.iso} value={option.iso}>
                    {countryName(option.iso, language)} (+{option.dial})
                  </option>
                ))}
              </select>
              <p id="lookup-country-hint" className="text-[11px] text-slate-500">
                {countryIgnored ? t.lookup.countryIgnored : t.lookup.countryHint}
              </p>
            </div>

            <div className="space-y-1">
              <label htmlFor="lookup-phone" className="block text-xs font-medium text-slate-300">
                {t.lookup.phone}
              </label>
              <div className="flex items-stretch gap-2">
                {!countryIgnored && (
                  <span
                    aria-hidden="true"
                    className="flex items-center rounded-lg border border-slate-700 bg-slate-900 px-2.5 font-mono text-xs text-slate-400"
                  >
                    +{country.dial}
                  </span>
                )}
                <Input
                  id="lookup-phone"
                  name="phone"
                  type="tel"
                  inputMode="tel"
                  dir="ltr"
                  autoComplete="tel-national"
                  placeholder={t.lookup.phonePlaceholder}
                  value={national}
                  onChange={(event) => setNational(event.target.value)}
                  aria-describedby="lookup-phone-hint lookup-phone-normalized"
                  aria-invalid={invalidPhone || undefined}
                  className="font-mono"
                  required
                />
              </div>
              <p id="lookup-phone-hint" className="text-[11px] text-slate-500">
                {t.lookup.phoneHint}
              </p>
              <p id="lookup-phone-normalized" className="text-[11px] text-slate-400" data-testid="normalized-phone">
                {t.lookup.normalized}: <span dir="ltr" className="font-mono text-sky-300">{e164 || '—'}</span>
              </p>
              {invalidPhone && (
                <p role="alert" className="text-[11px] text-rose-300">
                  {t.lookup.invalidPhone}
                </p>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={submitting || !national.trim()}>
              {submitting ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Search className="h-4 w-4" aria-hidden="true" />
              )}
              {submitting ? t.lookup.searching : t.lookup.search}
            </Button>
            {job && (
              <Button type="button" variant="secondary" onClick={reset}>
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                {t.lookup.newSearch}
              </Button>
            )}
          </div>
        </form>
      </Card>

      {error && (
        <div role="alert" className="flex items-center gap-2 rounded-lg border border-rose-500/40 bg-rose-950/40 px-4 py-2 text-sm text-rose-200">
          <AlertCircle className="h-4 w-4" aria-hidden="true" />
          {error}
        </div>
      )}

      {job && (
        <Card className="space-y-3" data-testid="lookup-progress">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-white">{t.lookup.progressTitle}</span>
              <Badge
                variant={
                  job.status === 'completed'
                    ? 'success'
                    : job.status === 'failed'
                      ? 'danger'
                      : job.status === 'running'
                        ? 'info'
                        : 'warning'
                }
              >
                {job.status === 'queued' || job.status === 'running' ? (
                  <Loader2 className="me-1 h-3 w-3 animate-spin" aria-hidden="true" />
                ) : null}
                {statusLabel}
              </Badge>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400">
              <span dir="ltr" className="font-mono">
                {job.phone_e164}
              </span>
              <span>
                {formatNumber(job.provider_results.length)} {t.lookup.providersAnswered}
              </span>
              {(job.started_at || job.completed_at) && (
                <span>
                  {t.lookup.elapsed}: {formatNumber(elapsed)}s
                </span>
              )}
              <span className="font-mono text-slate-500">{job.job_id.slice(0, 8)}</span>
            </div>
          </div>

          <div
            role="progressbar"
            aria-label={t.lookup.progressLabel}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={progress}
            aria-valuetext={progressText}
            data-testid="lookup-progressbar"
          >
            <Progress value={progress} />
          </div>

          {inFlight && <p className="text-[11px] text-slate-500">{t.lookup.autoRefresh}</p>}
          {job.status === 'failed' && (
            <p role="alert" className="text-xs text-rose-300">
              {t.lookup.jobError}
              {job.error_message ? `: ${job.error_message}` : ''}
            </p>
          )}
          <AggregateSummary job={job} />
        </Card>
      )}

      {job && (
        <section className="space-y-3" aria-labelledby="lookup-results-title">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 id="lookup-results-title" className="text-lg font-semibold text-white">
              {t.lookup.resultsTitle}
            </h3>
            <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400">
              <div className="flex items-center gap-1">
                <dt>{t.lookup.foundCount}</dt>
                <dd className="font-semibold text-emerald-300">{formatNumber(summary.found)}</dd>
              </div>
              <div className="flex items-center gap-1">
                <dt>{t.lookup.notFoundCount}</dt>
                <dd className="font-semibold text-slate-200">{formatNumber(summary.notFound)}</dd>
              </div>
              <div className="flex items-center gap-1">
                <dt>{t.lookup.errorCount}</dt>
                <dd className="font-semibold text-rose-300">{formatNumber(summary.errors)}</dd>
              </div>
              {summary.foundRatio !== null && (
                <div className="flex items-center gap-1">
                  <dt>{t.lookup.coverage}</dt>
                  <dd className="font-semibold text-sky-300">{percentText(summary.foundRatio, language)}</dd>
                </div>
              )}
            </dl>
          </div>

          {results.length === 0 ? (
            <EmptyState
              variant="empty"
              title={t.lookup.noResults}
              hint={t.lookup.noResultsHint}
              testId="lookup-no-results"
            />
          ) : summary.found === 0 && summary.errors === 0 ? (
            <EmptyState
              variant="empty"
              title={t.lookup.notFoundEmptyTitle}
              hint={t.lookup.notFoundEmptyHint}
              testId="lookup-all-not-found"
            />
          ) : (
            <ul className="grid gap-3 md:grid-cols-2" data-testid="lookup-results">
              {results.map((result) => (
                <li key={result.id} className="min-w-0">
                  <ResultCard result={result} />
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {!job && !error && (
        <EmptyState variant="empty" title={t.lookup.emptyTitle} hint={t.lookup.emptyHint} testId="lookup-empty" />
      )}
    </div>
  )
}

export default PhoneLookupView
