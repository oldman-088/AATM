import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { NeedsYouBadge, NeedsYouPanel } from '../DashboardViews'

function renderPanel(lang: 'en' | 'ar' | 'fa' = 'en', onNavigate = vi.fn()) {
  localStorage.setItem('aatm_language', lang)
  render(<I18nProvider><NeedsYouPanel onNavigate={onNavigate} /></I18nProvider>)
  return onNavigate
}

const items = [
  { kind: 'approval', id: 'a1', title: 'Merge or push into a protected branch', detail: 'merge', tab: 'approvals', at: '2026-11-24T20:00:00' },
  { kind: 'task_blocked', id: 't1', title: 'Stuck task', detail: 'session closed', tab: 'tasks', at: '2026-11-24T19:00:00' },
]

describe('NeedsYouPanel (P1)', () => {
  beforeEach(() => localStorage.clear())

  it('lists every waiting item and opens its screen', async () => {
    installFetchMock({ '/api/v1/dashboard/attention': { count: 2, items } })
    const onNavigate = renderPanel()
    const row = await screen.findByTestId('needs-you-task_blocked')
    expect(row.textContent).toContain('Blocked task')
    expect(row.textContent).toContain('Stuck task')
    expect(screen.getByTestId('needs-you').textContent).toContain('Needs you now')
    fireEvent.click(screen.getAllByRole('button', { name: 'Open' })[0])
    expect(onNavigate).toHaveBeenCalledWith('approvals')
  })

  it('says nothing needs the owner when the list is empty, in Arabic', async () => {
    installFetchMock({ '/api/v1/dashboard/attention': { count: 0, items: [] } })
    renderPanel('ar')
    expect((await screen.findByTestId('needs-you-empty')).textContent).toBe('لا شيء يحتاجك الآن.')
  })

  it('labels the kinds in Persian', async () => {
    installFetchMock({ '/api/v1/dashboard/attention': { count: 2, items } })
    renderPanel('fa')
    expect((await screen.findByTestId('needs-you-approval')).textContent).toContain('تأیید')
  })
})

describe('NeedsYouBadge (P1 T07)', () => {
  beforeEach(() => localStorage.clear())

  it('shows the count on every page and opens the dashboard', async () => {
    installFetchMock({ '/api/v1/dashboard/attention': { count: 3, items } })
    localStorage.setItem('aatm_language', 'en')
    const onOpen = vi.fn()
    render(<I18nProvider><NeedsYouBadge onOpen={onOpen} /></I18nProvider>)
    const badge = await screen.findByTestId('needs-you-badge')
    expect(badge.textContent).toContain('3')
    fireEvent.click(badge)
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('is hidden when nothing waits', async () => {
    const { calls } = installFetchMock({ '/api/v1/dashboard/attention': { count: 0, items: [] } })
    localStorage.setItem('aatm_language', 'en')
    render(<I18nProvider><NeedsYouBadge onOpen={vi.fn()} /></I18nProvider>)
    await waitFor(() => expect(calls.some((c) => c.path === '/api/v1/dashboard/attention')).toBe(true))
    expect(screen.queryByTestId('needs-you-badge')).toBeNull()
  })
})
