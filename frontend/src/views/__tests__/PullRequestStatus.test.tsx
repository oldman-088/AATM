import React from 'react'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { PullRequestStatus } from '../DashboardViews'

function renderStatus(lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  render(<I18nProvider><PullRequestStatus taskId="task-1" /></I18nProvider>)
}

describe('PullRequestStatus (P1 PR card)', () => {
  beforeEach(() => localStorage.clear())

  it('shows failing CI, the failing checks and a conflict', async () => {
    installFetchMock({ '/api/v1/reviews/tasks/task-1/pr-state': { available: true, state: 'OPEN', conflicting: true, ci: 'failure',
      checks: [{ name: 'lint', result: 'failure' }, { name: 'tests', result: 'success' }] } })
    renderStatus()
    const box = await screen.findByText('CI failed')
    const row = box.closest('[data-testid="pr-state-task-1"]') as HTMLElement
    expect(row.textContent).toContain('Merge conflict')
    expect(row.textContent).toContain('lint')
    expect(row.textContent).not.toContain('tests')
  })

  it('shows passing CI without a conflict in Arabic', async () => {
    installFetchMock({ '/api/v1/reviews/tasks/task-1/pr-state': { available: true, state: 'OPEN', conflicting: false, ci: 'success', checks: [] } })
    renderStatus('ar')
    expect(await screen.findByText('CI ناجح')).toBeTruthy()
    expect(screen.getByText('بلا تعارض')).toBeTruthy()
  })

  it('says when GitHub cannot be read', async () => {
    installFetchMock({ '/api/v1/reviews/tasks/task-1/pr-state': { available: false, error: 'gh not authenticated' } })
    renderStatus()
    expect((await screen.findByTestId('pr-state-task-1')).textContent).toBe('GitHub status unavailable')
  })
})
