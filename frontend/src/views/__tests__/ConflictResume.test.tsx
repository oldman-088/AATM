import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { PullRequestStatus } from '../DashboardViews'

const conflicting = { available: true, state: 'OPEN', conflicting: true, ci: 'success', checks: [] }

function renderCard(lang: 'en' | 'ar' = 'en') {
  localStorage.setItem('aatm_language', lang)
  render(<I18nProvider><PullRequestStatus taskId="t1" /></I18nProvider>)
}

describe('PR card: Resolve conflict (P1)', () => {
  afterEach(() => { vi.restoreAllMocks(); localStorage.clear() })

  it('asks first, then sends the PR back to its worker', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    const { calls } = installFetchMock({
      '/api/v1/reviews/tasks/t1/pr-state': conflicting,
      '/api/v1/reviews/tasks/t1/resolve-conflict': { follow_up_created: true },
    })
    renderCard()
    const button = await screen.findByTestId('resolve-conflict-t1')
    fireEvent.click(button)
    expect(calls.some((c) => c.path.endsWith('/resolve-conflict'))).toBe(false)
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByTestId('resolve-conflict-note-t1').textContent).toBe('Sent to the worker'))
    expect(calls.filter((c) => c.method === 'POST' && c.path.endsWith('/resolve-conflict'))).toHaveLength(1)
    expect(confirm.mock.calls[0][0]).toContain('same pull request')
  })

  it('has no button when there is no conflict', async () => {
    installFetchMock({ '/api/v1/reviews/tasks/t1/pr-state': { ...conflicting, conflicting: false } })
    renderCard('ar')
    await screen.findByTestId('pr-state-t1')
    expect(screen.queryByTestId('resolve-conflict-t1')).toBeNull()
  })
})
