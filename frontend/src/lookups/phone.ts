/**
 * Phone-number helpers for the lookup screen: country calling codes, input
 * normalization that mirrors the backend rules, and the result mapping that
 * turns raw provider rows into the per-platform cards.
 *
 * Backend contract: `backend/app/schemas/phone.py::normalize_phone_e164`
 * (Arabic-Indic and Persian digits are accepted, separators are stripped,
 * a leading `00` becomes `+`, and the result must be `+` plus 2–15 digits).
 */
import type { Language } from '../i18n/types'
import type { LookupJobDetail, ProviderPayload, ProviderResult } from './types'

// ---------------------------------------------------------------------------
// Countries
// ---------------------------------------------------------------------------

export interface CountryOption {
  /** ISO 3166-1 alpha-2 code (also the locale region for the display name). */
  iso: string
  /** Calling code without the `+` sign. */
  dial: string
}

/**
 * Countries offered in the picker. The list is intentionally a curated set
 * covering the project's operating regions (Gulf, Levant, North Africa, Iran,
 * Europe, North America) instead of every ISO code, so the phone prefix stays
 * short and predictable; the free-form input still accepts any `+` number.
 */
export const COUNTRIES: CountryOption[] = [
  { iso: 'SA', dial: '966' },
  { iso: 'AE', dial: '971' },
  { iso: 'QA', dial: '974' },
  { iso: 'KW', dial: '965' },
  { iso: 'BH', dial: '973' },
  { iso: 'OM', dial: '968' },
  { iso: 'EG', dial: '20' },
  { iso: 'JO', dial: '962' },
  { iso: 'LB', dial: '961' },
  { iso: 'MA', dial: '212' },
  { iso: 'DZ', dial: '213' },
  { iso: 'TN', dial: '216' },
  { iso: 'IQ', dial: '964' },
  { iso: 'SY', dial: '963' },
  { iso: 'YE', dial: '967' },
  { iso: 'PS', dial: '970' },
  { iso: 'TR', dial: '90' },
  { iso: 'IR', dial: '98' },
  { iso: 'RO', dial: '40' },
  { iso: 'GB', dial: '44' },
  { iso: 'DE', dial: '49' },
  { iso: 'FR', dial: '33' },
  { iso: 'ES', dial: '34' },
  { iso: 'IT', dial: '39' },
  { iso: 'NL', dial: '31' },
  { iso: 'SE', dial: '46' },
  { iso: 'CH', dial: '41' },
  { iso: 'IN', dial: '91' },
  { iso: 'PK', dial: '92' },
  { iso: 'US', dial: '1' },
  { iso: 'CA', dial: '1' },
]

const DISPLAY_LOCALES: Record<Language, string> = { en: 'en', ar: 'ar', fa: 'fa' }

/** Localized country name, falling back to the ISO code when the runtime lacks the data. */
export function countryName(iso: string, language: Language): string {
  try {
    const names = new Intl.DisplayNames([DISPLAY_LOCALES[language]], { type: 'region' })
    const label = names.of(iso)
    // `Intl` answers "Unknown Region" for codes it has no data for; the code
    // itself is the honest label in that case.
    if (!label || /^unknown/i.test(label)) return iso
    return label
  } catch {
    return iso
  }
}

// ---------------------------------------------------------------------------
// Input normalization (mirrors the backend)
// ---------------------------------------------------------------------------

const ARABIC_INDIC = /[\u0660-\u0669]/g
const EXTENDED_ARABIC_INDIC = /[\u06F0-\u06F9]/g
const SEPARATORS = /[\s\-.()/\\]/g
const E164 = /^\+[1-9]\d{1,14}$/

/** Convert Arabic-Indic (٠-٩) and Persian (۰-۹) digits to ASCII digits. */
export function toAsciiDigits(value: string): string {
  return value
    .replace(ARABIC_INDIC, (ch) => String(ch.charCodeAt(0) - 0x0660))
    .replace(EXTENDED_ARABIC_INDIC, (ch) => String(ch.charCodeAt(0) - 0x06f0))
}

/**
 * Build the E.164 candidate the user is typing.
 *
 * When the input already starts with `+` or `00` it is treated as a full
 * international number and the selected country's code is ignored. Otherwise
 * the selected country's dial code is prepended.
 */
export function composeE164(country: CountryOption, nationalInput: string): string {
  const cleaned = toAsciiDigits(nationalInput).replace(SEPARATORS, '')
  if (!cleaned) return ''
  if (cleaned.startsWith('+')) return cleaned
  if (cleaned.startsWith('00')) return `+${cleaned.slice(2)}`
  const national = cleaned.replace(/^0+/, '')
  return `+${country.dial}${national}`
}

/** `true` when `value` is a valid E.164 number (same rule as the backend). */
export function isValidE164(value: string): boolean {
  return E164.test(value)
}

// ---------------------------------------------------------------------------
// Platforms
// ---------------------------------------------------------------------------

/** Known social platform identifiers, used for icon + ordering only. */
export const KNOWN_PLATFORMS = [
  'facebook',
  'instagram',
  'whatsapp',
  'telegram',
  'twitter',
  'x',
  'linkedin',
  'tiktok',
  'youtube',
  'snapchat',
  'pinterest',
  'reddit',
  'github',
  'truecaller',
] as const

export type KnownPlatform = (typeof KNOWN_PLATFORMS)[number]

export function platformKey(name: string): string {
  return name.trim().toLowerCase().replace(/[\s_]+/g, '-')
}

// ---------------------------------------------------------------------------
// Result mapping
// ---------------------------------------------------------------------------

export type SocialResultState = 'found' | 'not_found' | 'error'

export interface SocialResult {
  /** Stable key for lists: the provider name exactly as the backend stores it. */
  id: string
  /** Display platform name (payload `platform`, else the provider name). */
  platform: string
  /** Normalized platform key for the icon lookup. */
  platformKey: string
  state: SocialResultState
  displayName: string | null
  avatarUrl: string | null
  profileUrl: string | null
  /** Confidence 0..1 when the provider reported one, otherwise `null`. */
  confidence: number | null
  /** Provider result status (`success` / `error` / `timeout` / `pending`). */
  status: ProviderResult['status']
  latencyMs: number | null
  errorMessage: string | null
  /** Extra provider keys, shown verbatim (no field is dropped). */
  attributes: Array<{ key: string; value: string }>
}

/** Strings only when the value is a non-empty, trimmed string. */
function text(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const trimmed = value.trim()
  return trimmed ? trimmed : null
}

/** Accept 0..1 or 0..100 and return 0..1; anything else is `null`. */
export function normalizeConfidence(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value < 0) return null
  const ratio = value > 1 ? value / 100 : value
  if (ratio > 1) return null
  return ratio
}

/** Only http(s) profile links are rendered as links (never `javascript:` etc.). */
export function safeHttpUrl(value: unknown): string | null {
  const raw = text(value)
  if (!raw) return null
  try {
    const parsed = new URL(raw)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
    return parsed.toString()
  } catch {
    return null
  }
}

const ATTRIBUTE_EXCLUDED_KEYS = new Set([
  'platform',
  'not_found',
  'found',
  'display_name',
  'name',
  'avatar_url',
  'profile_url',
  'confidence',
])

function formatAttributeValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/** Map one backend provider row onto a social result card. */
export function mapProviderResult(row: ProviderResult): SocialResult {
  const payload = (row.payload ?? {}) as ProviderPayload
  const failed = row.status === 'error' || row.status === 'timeout' || row.status === 'pending'
  const explicitNotFound = payload.not_found === true

  let state: SocialResultState
  if (failed) {
    state = 'error'
  } else if (explicitNotFound || payload.found === false) {
    state = 'not_found'
  } else {
    // A successful answer without an explicit flag counts as found when it
    // carries any of the identity fields; otherwise it is an empty answer.
    const hasIdentity =
      text(payload.display_name) !== null ||
      text(payload.name) !== null ||
      safeHttpUrl(payload.profile_url) !== null ||
      safeHttpUrl(payload.avatar_url) !== null
    state = hasIdentity || payload.found === true ? 'found' : 'not_found'
  }

  const platform = text(payload.platform) ?? row.provider
  const attributes = Object.entries(payload)
    .filter(([key]) => !ATTRIBUTE_EXCLUDED_KEYS.has(key))
    .map(([key, value]) => ({ key, value: formatAttributeValue(value) }))

  return {
    id: row.provider,
    platform,
    platformKey: platformKey(platform),
    state,
    displayName: state === 'found' ? text(payload.display_name) ?? text(payload.name) : null,
    // Avatars are rendered through our own origin (the CSP allows `img-src 'self'`).
    avatarUrl: state === 'found' ? safeHttpUrl(payload.avatar_url) : null,
    profileUrl: state === 'found' ? safeHttpUrl(payload.profile_url) : null,
    confidence: state === 'found' ? normalizeConfidence(payload.confidence) : null,
    status: row.status,
    latencyMs: typeof row.latency_ms === 'number' ? row.latency_ms : null,
    errorMessage: text(row.error_message),
    attributes,
  }
}

export interface SocialResultSummary {
  total: number
  found: number
  notFound: number
  errors: number
  /** Share of searched platforms that produced a profile (0..1), `null` when none were searched. */
  foundRatio: number | null
}

export function summarizeResults(results: SocialResult[]): SocialResultSummary {
  const found = results.filter((r) => r.state === 'found').length
  const notFound = results.filter((r) => r.state === 'not_found').length
  const errors = results.filter((r) => r.state === 'error').length
  const searched = found + notFound
  return {
    total: results.length,
    found,
    notFound,
    errors,
    foundRatio: searched === 0 ? null : found / searched,
  }
}

/** Progress of a job as a 0..100 number for the progress bar. */
export function jobProgress(job: LookupJobDetail | null): number {
  if (!job) return 0
  switch (job.status) {
    case 'queued':
      return 8
    case 'running':
      // Grow with the number of providers that already answered, capped at 90%
      // so the bar only completes when the job itself is finalized.
      return Math.min(90, 20 + job.provider_results.length * 15)
    case 'completed':
    case 'failed':
      return 100
    default:
      return 0
  }
}

/** The avatar URL our own origin serves, for a third-party image URL. */
export function avatarProxyUrl(remoteUrl: string): string {
  return `/api/v1/media/avatar?url=${encodeURIComponent(remoteUrl)}`
}
