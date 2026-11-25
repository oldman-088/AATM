import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { TasksView } from '../CoreViews'

function renderTasks(projectId: string | null, projects: any[]) {
  localStorage.setItem('aatm_language', 'en')
  render(<I18nProvider><TasksView tasks={[]} projectId={projectId} projects={projects} onReload={() => {}} /></I18nProvider>)
}

afterEach(() => vi.restoreAllMocks())

describe('Create task form', () => {
  it('explains that a disabled project takes no tasks and sends nothing', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderTasks('p', [{ id: 'p', name: 'AATM Platform', status: 'disabled' }])
    expect(screen.getByTestId('task-create-notice').textContent).toContain('AATM Platform')
    fireEvent.click(screen.getByTestId('task-create-submit'))
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('asks for a title instead of doing nothing', () => {
    renderTasks('p', [{ id: 'p', name: 'Live', status: 'active' }])
    fireEvent.click(screen.getByTestId('task-create-submit'))
    expect(screen.getByTestId('task-create-notice').textContent).toContain('title')
  })

  it('shows the server error when creation fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: 'project is disabled' }), { status: 409 }))
    renderTasks('p', [{ id: 'p', name: 'Live', status: 'active' }])
    fireEvent.change(screen.getByPlaceholderText('Task Title'), { target: { value: 'Build it' } })
    fireEvent.click(screen.getByTestId('task-create-submit'))
    await waitFor(() => expect(screen.getByTestId('task-create-notice').textContent).toContain('project is disabled'))
  })

  it('confirms when the task is created', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ id: 't1' }), { status: 201 }))
    renderTasks('p', [{ id: 'p', name: 'Live', status: 'active' }])
    fireEvent.change(screen.getByPlaceholderText('Task Title'), { target: { value: 'Build it' } })
    fireEvent.click(screen.getByTestId('task-create-submit'))
    await waitFor(() => expect(screen.getByTestId('task-create-notice').textContent).toContain('Task created'))
  })
})
