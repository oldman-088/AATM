import React from 'react'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { DailySummary, ProjectTimeline } from '../DashboardViews'

const steps = [{ id: 'request', state: 'done', done: 1, total: 1 }]

function renderWith(node: React.ReactNode, lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  render(<I18nProvider>{node}</I18nProvider>)
}

describe('Project progress and daily summary (P3)', () => {
  beforeEach(() => localStorage.clear())

  it('shows done/total, percent and the time left in minutes or hours', async () => {
    installFetchMock({ '/api/v1/projects/p1/timeline': { project_name: 'Shop', steps,
      progress: { total: 6, done: 3, remaining: 3, percent: 50, eta_minutes: 120 } } })
    renderWith(<ProjectTimeline projectId="p1" />)
    const box = await screen.findByTestId('project-progress')
    expect(box.textContent).toContain('3/6')
    expect(box.textContent).toContain('50%')
    expect(screen.getByTestId('project-eta').textContent).toBe('About: 2 h')
  })

  it('says when the time left is not known yet, in Arabic', async () => {
    installFetchMock({ '/api/v1/projects/p1/timeline': { project_name: 'Shop', steps,
      progress: { total: 2, done: 0, remaining: 2, percent: 0, eta_minutes: null } } })
    renderWith(<ProjectTimeline projectId="p1" />, 'ar')
    expect((await screen.findByTestId('project-eta')).textContent).toBe('الوقت المتبقي: غير معروف بعد')
  })

  it('summarises the last 24 hours', async () => {
    installFetchMock({ '/api/v1/dashboard/daily': { hours: 24, completed_tasks: 4, pending_approvals: 1, critical_alerts: 0,
      merged_prs: [{ task_id: 't1', pr_number: 12, title: 'Add answer()' }], failed_or_blocked: [{ task_id: 't2', title: 'Broken', status: 'failed' }] } })
    renderWith(<DailySummary />)
    const card = await screen.findByTestId('daily-summary')
    expect(card.textContent).toContain('Last 24 hours')
    expect(card.textContent).toContain('#12')
    expect(card.textContent).toContain('Broken (failed)')
  })
})
