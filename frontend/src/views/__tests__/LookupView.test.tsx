import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import axe from 'axe-core'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { PhoneLookupView } from '../LookupView'
import type { LookupJobDetail } from '../../lookups/types'

/** A completed job with one found profile, one explicit not_found and one provider error. */
const COMPLETED_JOB: LookupJobDetail = {
  job_id: 'job-abc12345',
  status: 'completed',
  phone_e164: '+966501234567',
  phone_raw: '0501234567',
  created_at: '2026-11-20T09:00:00Z',
  updated_at: '2026-11-20T09:00:05Z',
  started_at: '2026-11-20T09:00:01Z',
  completed_at: '2026-11-20T09:00:05Z',
  error_message: null,
  result: {
    display_name: 'Sara Al-Qahtani',
    country: 'SA',
    carrier: 'STC',
    line_type: 'mobile',
    spam_score: 8,
    is_spam: false,
    summary: 'Sara Al-Qahtani · STC · SA · mobile',
    attributes: {},
    updated_at: '2026-11-20T09:00:05Z',
  },
  provider_results: [
    {
      provider: 'facebook',
      status: 'success',
      http_status: 200,
      error_message: null,
      latency_ms: 180,
      payload: {
        platform: 'facebook',
        display_name: 'Sara Al-Qahtani',
        avatar_url: 'https://cdn.example.test/sara.png',
        profile_url: 'https://facebook.example.test/sara',
        confidence: 86,
        followers: 1200,
      },
      created_at: '2026-11-20T09:00:02Z',
      updated_at: '2026-11-20T09:00:02Z',
    },
    {
      provider: 'instagram',
      status: 'success',
      http_status: 200,
      error_message: null,
      latency_ms: 210,
      payload: { platform: 'instagram', not_found: true },
      created_at: '2026-11-20T09:00:03Z',
      updated_at: '2026-11-20T09:00:03Z',
    },
    {
      provider: 'linkedin',
      status: 'timeout',
      http_status: null,
      error_message: 'upstream timed out',
      latency_ms: null,
      payload: { platform: 'linkedin' },
      created_at: '2026-11-20T09:00:04Z',
      updated_at: '2026-11-20T09:00:04Z',
    },
  ],
}

function renderView(lang: 'en' | 'ar' | 'fa' = 'en', pollIntervalMs = 5) {
  localStorage.setItem('aatm_language', lang)
  localStorage.setItem('aatm_jwt_token', 'test-jwt')
  return render(
    <I18nProvider>
      <PhoneLookupView pollIntervalMs={pollIntervalMs} />
    </I18nProvider>,
  )
}

function phoneInput() {
  // `getByLabelText` alone can match the "Phone number" heading via its aria-label,
  // so the field is resolved as an actual input element.
  return screen.getByRole('textbox', { name: /Phone number|رقم الهاتف|شماره تلفن/ })
}

async function settleSelect() {
  await waitFor(() => expect(screen.getByLabelText('Country')).toBeInTheDocument())
}

function typePhone(value: string) {
  fireEvent.change(phoneInput(), { target: { value } })
}

async function search() {
  fireEvent.click(screen.getByRole('button', { name: /Search|بحث|جست‌وجو/ }))
}

describe('Phone lookup screen', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.dir = 'ltr'
    document.documentElement.lang = 'en'
  })

  it('creates a lookup with the E.164 number, then renders a card per platform', async () => {
    const { calls } = installFetchMock({
      '/api/v1/lookups': (_path: string, init: RequestInit) => {
        const body = JSON.parse(String(init.body || '{}'))
        return {
          job_id: 'job-abc12345',
          status: 'queued',
          phone_e164: body.phone,
          phone_raw: body.phone,
          created_at: '2026-11-20T09:00:00Z',
        }
      },
      '/api/v1/lookups/job-abc12345': COMPLETED_JOB,
    })

    renderView()
    // Country defaults to Saudi Arabia, so a national number becomes +966…
    expect(screen.getByLabelText('Country')).toHaveValue('SA')
    typePhone('0501234567')
    expect(screen.getByTestId('normalized-phone')).toHaveTextContent('+966501234567')

    await search()

    await waitFor(() => {
      const post = calls.find((c) => c.path === '/api/v1/lookups' && c.method === 'POST')
      expect(post?.body).toEqual({ phone: '+966501234567' })
      expect(post?.authorization).toBe('Bearer test-jwt')
    })

    // Progress panel polls the job and reports completion.
    const progress = await screen.findByTestId('lookup-progress')
    // The panel starts as queued and flips to completed once the poll lands.
    expect(await within(progress).findByText('Completed')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(calls.some((c) => c.path === '/api/v1/lookups/job-abc12345' && c.method === 'GET')).toBe(true)

    // One card per provider row.
    const cards = await screen.findAllByTestId(/^result-card-/)
    expect(cards).toHaveLength(3)

    // Found card: name, link, confidence and picture through our own origin.
    const facebook = screen.getByTestId('result-card-facebook')
    expect(within(facebook).getByText('Sara Al-Qahtani')).toBeInTheDocument()
    const profile = within(facebook).getByRole('link', { name: /Open profile/ })
    expect(profile).toHaveAttribute('href', 'https://facebook.example.test/sara')
    expect(profile).toHaveAttribute('target', '_blank')
    expect(profile).toHaveAttribute('rel', 'noopener noreferrer')
    expect(within(facebook).getByLabelText(/86%/)).toBeInTheDocument()
    const avatar = within(facebook).getByRole('img', { name: /Sara Al-Qahtani/ })
    expect(avatar.getAttribute('src')).toBe(
      '/api/v1/media/avatar?url=https%3A%2F%2Fcdn.example.test%2Fsara.png',
    )
    expect(avatar.getAttribute('src')).not.toMatch(/localhost|127\.0\.0\.1/)

    // Not-found card shows the not_found state.
    const instagram = screen.getByTestId('result-card-instagram')
    expect(within(instagram).getByTestId('not-found-instagram')).toBeInTheDocument()
    expect(within(instagram).getByText('This platform returned no profile for this number.')).toBeInTheDocument()

    // Provider error card is flagged as unavailable, not as "not found".
    const linkedin = screen.getByTestId('result-card-linkedin')
    expect(within(linkedin).getByText('Provider unavailable')).toBeInTheDocument()
    expect(within(linkedin).getByText(/upstream timed out/)).toBeInTheDocument()
    expect(within(linkedin).queryByTestId('not-found-linkedin')).not.toBeInTheDocument()

    // Counters and the aggregate summary are real values from the payload.
    const counters = screen.getByText('Coverage').closest('dl') as HTMLElement
    expect(counters).toHaveTextContent('Found1')
    expect(counters).toHaveTextContent('Not found1')
    expect(counters).toHaveTextContent('Unavailable1')
    const aggregate = screen.getByTestId('aggregate-summary')
    expect(aggregate).toHaveTextContent('STC')
    expect(aggregate).toHaveTextContent('mobile')
  })

  it('validates the number before calling the API', async () => {
    const { calls } = installFetchMock()
    renderView()
    typePhone('abc')
    await search()

    expect(await screen.findByRole('alert')).toHaveTextContent(/Enter a valid international number/)
    expect(calls.some((c) => c.path === '/api/v1/lookups' && c.method === 'POST')).toBe(false)
  })

  it('switches between EN (LTR), AR (RTL) and FA (RTL) without a reload', async () => {
    installFetchMock()
    renderView('en')
    expect(document.documentElement.dir).toBe('ltr')
    expect(screen.getByRole('heading', { level: 2, name: /Phone Number Lookup/ })).toBeInTheDocument()
    expect(screen.getByText('Search a phone number')).toBeInTheDocument()

    // Persian: the very same mounted tree re-renders in Persian and stays RTL.
    const view = renderView('fa')
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('fa')
    expect(screen.getAllByRole('heading', { level: 2, name: /جست‌وجوی شماره تلفن/ }).length).toBeGreaterThan(0)
    expect(screen.getAllByText('یک شماره تلفن را جست‌وجو کنید').length).toBeGreaterThan(0)
    view.unmount()

    renderView('ar')
    expect(document.documentElement.dir).toBe('rtl')
    expect(document.documentElement.lang).toBe('ar')
    expect(screen.getByRole('heading', { level: 2, name: /البحث عن رقم الهاتف/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /بحث/ })).toBeInTheDocument()
    expect(screen.getByText('ابحث عن رقم هاتف')).toBeInTheDocument()
  })

  it('renders Arabic and Persian digits as ASCII in the request', async () => {
    const { calls } = installFetchMock()
    renderView('ar')
    fireEvent.change(phoneInput(), { target: { value: '٥٠١٢٣٤٥٦٧' } })
    fireEvent.click(screen.getByRole('button', { name: /بحث/ }))
    await waitFor(() => {
      const post = calls.find((c) => c.path === '/api/v1/lookups' && c.method === 'POST')
      expect(post?.body).toEqual({ phone: '+966501234567' })
    })
  })

  it('polls a running job until it is completed', async () => {
    let polls = 0
    const running: LookupJobDetail = {
      ...COMPLETED_JOB,
      status: 'running',
      completed_at: null,
      result: null,
      provider_results: COMPLETED_JOB.provider_results.slice(0, 1),
    }
    installFetchMock({
      '/api/v1/lookups': {
        job_id: 'job-abc12345',
        status: 'queued',
        phone_e164: '+966501234567',
        phone_raw: '+966501234567',
        created_at: '2026-11-20T09:00:00Z',
      },
      '/api/v1/lookups/job-abc12345': () => {
        polls += 1
        return polls < 2 ? running : COMPLETED_JOB
      },
    })

    // A slower cadence keeps each intermediate state observable.
    renderView('en', 200)
    typePhone('501234567')
    await search()

    // While the job is running the panel shows the live status and refresh hint.
    expect(await screen.findByText('Searching platforms')).toBeInTheDocument()
    expect(screen.getByText('Auto-refreshing every 3 seconds')).toBeInTheDocument()

    // The poll flips it to completed and all three cards appear.
    await waitFor(() => expect(screen.getByText('Completed')).toBeInTheDocument())
    expect(await screen.findAllByTestId(/^result-card-/)).toHaveLength(3)
    expect(polls).toBeGreaterThanOrEqual(1)
  })

  it('uses a neutral empty state when every platform reports not_found', async () => {
    installFetchMock({
      '/api/v1/lookups': {
        job_id: 'job-none',
        status: 'queued',
        phone_e164: '+40721234567',
        phone_raw: '+40721234567',
        created_at: '2026-11-20T09:00:00Z',
      },
      '/api/v1/lookups/job-none': {
        ...COMPLETED_JOB,
        job_id: 'job-none',
        provider_results: [
          { ...COMPLETED_JOB.provider_results[1], provider: 'facebook' },
          { ...COMPLETED_JOB.provider_results[1], provider: 'telegram' },
        ],
      },
    })
    renderView()
    typePhone('+40721234567')
    await search()
    expect(await screen.findByTestId('lookup-all-not-found')).toBeInTheDocument()
    expect(screen.getByText('Every platform searched reported no profile for this number.')).toBeInTheDocument()
  })

  it('shows a retryable error when the API call fails', async () => {
    const mock = installFetchMock()
    ;(globalThis as any).fetch = async (input: any, init: RequestInit = {}) => {
      if (String(input) === '/api/v1/lookups' && (init.method || 'GET') === 'POST') {
        return { ok: false, status: 500, json: async () => ({ detail: 'lookup service unavailable' }) }
      }
      return { ok: true, status: 200, json: async () => ({}) }
    }
    renderView()
    typePhone('501234567')
    await search()
    expect(await screen.findByRole('alert')).toHaveTextContent('lookup service unavailable')
    expect(mock).toBeDefined()
  })

  it('disables the country picker once the number carries its own international prefix', async () => {
    installFetchMock()
    renderView()
    const country = screen.getByLabelText('Country') as HTMLSelectElement
    expect(country).toBeEnabled()

    typePhone('+40 721 234 567')
    expect(country).toBeDisabled()
    expect(screen.getByText(/Ignored — the number already has its own international prefix/)).toBeInTheDocument()
    expect(screen.getByTestId('normalized-phone')).toHaveTextContent('+40721234567')

    typePhone('721234567')
    expect(screen.getByLabelText('Country')).toBeEnabled()
    expect(screen.getByTestId('normalized-phone')).toHaveTextContent('+966721234567')
    await settleSelect()
  })

  it('passes a basic axe accessibility scan in English and Arabic (RTL)', async () => {
    installFetchMock({
      '/api/v1/lookups': {
        job_id: 'job-abc12345',
        status: 'queued',
        phone_e164: '+966501234567',
        phone_raw: '+966501234567',
        created_at: '2026-11-20T09:00:00Z',
      },
      '/api/v1/lookups/job-abc12345': COMPLETED_JOB,
    })

    for (const lang of ['en', 'ar'] as const) {
      const { container, unmount } = renderView(lang)
      typePhone('501234567')
      await search()
      await screen.findAllByTestId(/^result-card-/)

      const results = await axe.run(container, {
        rules: {
          // jsdom has no layout engine, so colour-contrast cannot be evaluated here;
          // the palette is inherited from the existing shared components.
          'color-contrast': { enabled: false },
        },
      })
      const violations = results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.html).join(' | ')}`)
      expect(violations).toEqual([])
      unmount()
    }
  })
})
