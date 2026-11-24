import React from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { TasksView } from '../CoreViews'

const wrap = (ui: React.ReactElement, lang: 'en' | 'ar' | 'fa' = 'en') => {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider>{ui}</I18nProvider>)
}

const tasks = [
  { id: 't1', title: 'Build parser', division_code: 'D01', status: 'in_progress', priority: 'high', retry_count: 0, max_retries: 3 },
  { id: 't2', title: 'Write docs', division_code: 'D02', status: 'pr_open', priority: 'low', retry_count: 0, max_retries: 3 },
  { id: 't3', title: 'Ship API', division_code: 'D03', status: 'completed', priority: 'medium', retry_count: 1, max_retries: 3 },
  { id: 't4', title: 'Broken test', division_code: 'D04', status: 'failed', priority: 'critical', retry_count: 3, max_retries: 3 },
]

describe('TasksView screen', () => {
  beforeEach(() => localStorage.clear())

  it('shows status summary counts computed from the task list', () => {
    wrap(<TasksView tasks={tasks} projectId="p1" onReload={() => {}} />)
    const summary = screen.getByTestId('tasks-summary')
    const cards = summary.querySelectorAll('.text-2xl')
    expect(Array.from(cards).map((n) => n.textContent)).toEqual(['4', '2', '1', '1'])
  })

  it('filters the list by status and restores all tasks on "All statuses"', () => {
    wrap(<TasksView tasks={tasks} projectId="p1" onReload={() => {}} />)
    const list = () => screen.queryAllByText(/^(Build parser|Write docs|Ship API|Broken test)$/)
    expect(list()).toHaveLength(4)

    fireEvent.change(screen.getByTestId('tasks-filter'), { target: { value: 'failed' } })
    expect(list().map((n) => n.textContent)).toEqual(['Broken test'])

    fireEvent.change(screen.getByTestId('tasks-filter'), { target: { value: 'all' } })
    expect(list()).toHaveLength(4)
  })

  it('renders the status badge tone for each task', () => {
    wrap(<TasksView tasks={tasks} projectId="p1" onReload={() => {}} />)
    const failedRow = screen.getByText('Broken test').closest('.rounded-xl') as HTMLElement
    expect(within(failedRow).getByText('failed').className).toMatch(/rose|red|danger/)
  })
})
