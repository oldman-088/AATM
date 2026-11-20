import { describe, expect, it } from 'vitest'
import {
  COUNTRIES,
  avatarProxyUrl,
  composeE164,
  countryName,
  isValidE164,
  jobProgress,
  mapProviderResult,
  normalizeConfidence,
  platformKey,
  safeHttpUrl,
  summarizeResults,
  toAsciiDigits,
} from '../phone'
import type { LookupJobDetail, ProviderResult } from '../types'

const SAUDI = COUNTRIES.find((c) => c.iso === 'SA')!
const ROMANIA = COUNTRIES.find((c) => c.iso === 'RO')!

function row(partial: Partial<ProviderResult> & { provider: string }): ProviderResult {
  return {
    status: 'success',
    http_status: 200,
    error_message: null,
    latency_ms: 120,
    payload: {},
    created_at: '2026-11-20T09:00:00Z',
    updated_at: '2026-11-20T09:00:00Z',
    ...partial,
  }
}

describe('phone normalization (mirrors backend/app/schemas/phone.py)', () => {
  it('converts Arabic-Indic and Persian digits to ASCII', () => {
    expect(toAsciiDigits('٠١٢٣٤٥٦٧٨٩')).toBe('0123456789')
    expect(toAsciiDigits('۰۱۲۳۴۵۶۷۸۹')).toBe('0123456789')
    expect(toAsciiDigits('+٤٠٧٢١٢٣٤٥٦٧')).toBe('+40721234567')
  })

  it('prepends the selected country code to a national number', () => {
    expect(composeE164(SAUDI, '501234567')).toBe('+966501234567')
    expect(composeE164(SAUDI, '0501234567')).toBe('+966501234567') // local trunk zero dropped
    expect(composeE164(SAUDI, '50 123 4567')).toBe('+966501234567') // separators stripped
    expect(composeE164(ROMANIA, '721 234 567')).toBe('+40721234567')
  })

  it('keeps an explicit international prefix instead of adding a country code', () => {
    expect(composeE164(SAUDI, '+40 721 234 567')).toBe('+40721234567')
    expect(composeE164(SAUDI, '0040 721 234 567')).toBe('+40721234567')
    expect(composeE164(SAUDI, '٠٠٤٠ ٧٢١ ٢٣٤ ٥٦٧')).toBe('+40721234567')
    expect(composeE164(ROMANIA, '+1 (415) 555-2671')).toBe('+14155552671')
  })

  it('returns an empty candidate for empty input', () => {
    expect(composeE164(SAUDI, '')).toBe('')
    expect(composeE164(SAUDI, '   ')).toBe('')
  })

  it('validates E.164 exactly like the backend', () => {
    expect(isValidE164('+966501234567')).toBe(true)
    expect(isValidE164('+123456789012345')).toBe(true) // 15 digits is the maximum
    expect(isValidE164('+0123456789')).toBe(false) // leading zero
    expect(isValidE164('+1234567890123456')).toBe(false) // 16 digits
    expect(isValidE164('966501234567')).toBe(false) // missing +
    expect(isValidE164('+')).toBe(false)
    expect(isValidE164('+40 721 234 567')).toBe(false) // spaces are not valid E.164
  })

  it('localizes country names in EN, AR and FA', () => {
    expect(countryName('SA', 'en')).toBe('Saudi Arabia')
    expect(countryName('SA', 'ar')).toContain('السعودية')
    expect(countryName('RO', 'fa')).toContain('رومانی')
    expect(countryName('ZZ', 'en')).toBe('ZZ') // unknown code falls back to the code
  })
})

describe('confidence and URL hardening', () => {
  it('accepts 0..1 ratios and 0..100 percentages', () => {
    expect(normalizeConfidence(0.42)).toBe(0.42)
    expect(normalizeConfidence(1)).toBe(1)
    expect(normalizeConfidence(42)).toBeCloseTo(0.42)
    expect(normalizeConfidence(100)).toBe(1)
  })

  it('rejects nonsense confidence values instead of inventing one', () => {
    for (const value of [-1, 250, Number.NaN, Number.POSITIVE_INFINITY, '80', null, undefined, {}, true]) {
      expect(normalizeConfidence(value as any)).toBeNull()
    }
  })

  it('allows only absolute http(s) URLs', () => {
    expect(safeHttpUrl('https://facebook.com/sara')).toBe('https://facebook.com/sara')
    expect(safeHttpUrl('http://example.test/a.png')).toBe('http://example.test/a.png')
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull()
    expect(safeHttpUrl('data:image/png;base64,AAAA')).toBeNull()
    expect(safeHttpUrl('/relative/path')).toBeNull()
    expect(safeHttpUrl('   ')).toBeNull()
    expect(safeHttpUrl(42)).toBeNull()
  })

  it('routes avatars through our own origin (CSP img-src self)', () => {
    expect(avatarProxyUrl('https://cdn.test/a b.png')).toBe(
      '/api/v1/media/avatar?url=https%3A%2F%2Fcdn.test%2Fa%20b.png',
    )
  })
})

describe('provider row -> social result card mapping', () => {
  it('maps a found profile with picture, link and confidence', () => {
    const result = mapProviderResult(
      row({
        provider: 'fb-scraper',
        payload: {
          platform: 'Facebook',
          display_name: 'Sara Al-Qahtani',
          avatar_url: 'https://cdn.test/sara.png',
          profile_url: 'https://facebook.com/sara',
          confidence: 86,
          followers: 1200,
          verified: true,
        },
      }),
    )
    expect(result.platform).toBe('Facebook')
    expect(result.platformKey).toBe('facebook')
    expect(result.state).toBe('found')
    expect(result.displayName).toBe('Sara Al-Qahtani')
    expect(result.avatarUrl).toBe('https://cdn.test/sara.png')
    expect(result.profileUrl).toBe('https://facebook.com/sara')
    expect(result.confidence).toBeCloseTo(0.86)
    // every extra provider field is preserved
    expect(result.attributes).toEqual([
      { key: 'followers', value: '1200' },
      { key: 'verified', value: 'true' },
    ])
  })

  it('marks an explicit not_found answer and hides identity fields', () => {
    const result = mapProviderResult(
      row({
        provider: 'instagram',
        payload: { platform: 'instagram', not_found: true, display_name: 'stale', profile_url: 'https://x.test/a' },
      }),
    )
    expect(result.state).toBe('not_found')
    expect(result.displayName).toBeNull()
    expect(result.profileUrl).toBeNull()
    expect(result.confidence).toBeNull()
  })

  it('treats an empty successful answer as not_found', () => {
    expect(mapProviderResult(row({ provider: 'twitter', payload: {} })).state).toBe('not_found')
  })

  it('honours an explicit found flag even without identity fields', () => {
    const result = mapProviderResult(row({ provider: 'telegram', payload: { found: true, handle: '@sara' } }))
    expect(result.state).toBe('found')
    expect(result.attributes).toEqual([{ key: 'handle', value: '@sara' }])
  })

  it('maps provider errors and timeouts to the unavailable state', () => {
    for (const status of ['error', 'timeout', 'pending'] as const) {
      const result = mapProviderResult(
        row({ provider: 'linkedin', status, error_message: 'upstream timed out', payload: { display_name: 'x' } }),
      )
      expect(result.state).toBe('error')
      expect(result.errorMessage).toBe('upstream timed out')
      expect(result.displayName).toBeNull()
    }
  })

  it('falls back to the provider name when no platform is declared', () => {
    const result = mapProviderResult(row({ provider: 'Truecaller', payload: { name: 'Ion Popescu' } }))
    expect(result.platform).toBe('Truecaller')
    expect(result.platformKey).toBe('truecaller')
    expect(result.displayName).toBe('Ion Popescu')
  })

  it('never renders an unsafe avatar or profile URL', () => {
    const result = mapProviderResult(
      row({
        provider: 'x',
        payload: {
          display_name: 'Attacker',
          avatar_url: 'javascript:alert(1)',
          profile_url: 'data:text/html,<script>alert(1)</script>',
        },
      }),
    )
    expect(result.state).toBe('found')
    expect(result.avatarUrl).toBeNull()
    expect(result.profileUrl).toBeNull()
  })
})

describe('summary and progress', () => {
  it('counts found / not found / errors and the coverage ratio', () => {
    const results = [
      mapProviderResult(row({ provider: 'a', payload: { display_name: 'A' } })),
      mapProviderResult(row({ provider: 'b', payload: { not_found: true } })),
      mapProviderResult(row({ provider: 'c', status: 'error' })),
      mapProviderResult(row({ provider: 'd', payload: { profile_url: 'https://d.test/p' } })),
    ]
    const summary = summarizeResults(results)
    expect(summary).toMatchObject({ total: 4, found: 2, notFound: 1, errors: 1 })
    expect(summary.foundRatio).toBeCloseTo(2 / 3)
  })

  it('reports no ratio when nothing was searched', () => {
    expect(summarizeResults([]).foundRatio).toBeNull()
    expect(summarizeResults([mapProviderResult(row({ provider: 'a', status: 'timeout' }))]).foundRatio).toBeNull()
  })

  it('derives job progress from the job status', () => {
    const base = {
      job_id: 'j',
      status: 'queued' as const,
      phone_e164: '+966501234567',
      phone_raw: 'x',
      created_at: '2026-11-20T09:00:00Z',
      updated_at: '2026-11-20T09:00:00Z',
      started_at: null,
      completed_at: null,
      error_message: null,
      result: null,
      provider_results: [],
    } as LookupJobDetail

    expect(jobProgress(null)).toBe(0)
    expect(jobProgress({ ...base, status: 'queued' })).toBe(8)
    expect(jobProgress({ ...base, status: 'running' })).toBe(20)
    expect(jobProgress({ ...base, status: 'running', provider_results: [row({ provider: 'a' })] })).toBe(35)
    expect(
      jobProgress({ ...base, status: 'running', provider_results: [row({ provider: 'a' }), row({ provider: 'b' })] }),
    ).toBe(50)
    expect(jobProgress({ ...base, status: 'completed' })).toBe(100)
    expect(jobProgress({ ...base, status: 'failed' })).toBe(100)
  })

  it('normalizes platform keys for icon lookup', () => {
    expect(platformKey(' Facebook ')).toBe('facebook')
    expect(platformKey('tik_tok')).toBe('tik-tok')
    expect(platformKey('X')).toBe('x')
  })
})
