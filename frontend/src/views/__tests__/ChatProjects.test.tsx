import React from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { ManagerChatView } from '../CoreViews'

const projects = [
  { id: 'old', name: 'AATM Platform', status: 'disabled' },
  { id: 'done', name: 'test-program', status: 'completed' },
  { id: 'a', name: 'aatm-arena-test', status: 'active' },
  { id: 'b', name: 'AATM self-dev', status: 'active' },
]

function renderChat(projectId: string) {
  localStorage.setItem('aatm_language', 'en')
  render(<I18nProvider><ManagerChatView messages={[]} projectId={projectId} projects={projects} onSelectProject={() => {}}
    onReload={() => {}} /></I18nProvider>)
}

describe('Manager chat project list', () => {
  it('lists only active projects', () => {
    renderChat('a')
    const options = Array.from((screen.getByRole('combobox') as HTMLSelectElement).options).map((o) => o.textContent)
    expect(options).toEqual(['aatm-arena-test', 'AATM self-dev'])
  })

  it('still shows a selected inactive project so the choice is visible', () => {
    renderChat('old')
    const options = Array.from((screen.getByRole('combobox') as HTMLSelectElement).options).map((o) => o.textContent)
    expect(options).toEqual(['AATM Platform', 'aatm-arena-test', 'AATM self-dev'])
  })
})

describe('Manager chat on an inactive project', () => {
  it('warns and does not send', () => {
    renderChat('done')
    expect(screen.getByTestId('chat-inactive-project').textContent).toContain('test-program')
    fireEvent.change(screen.getByPlaceholderText(/./), { target: { value: 'build it' } })
    expect((screen.getByRole('button', { name: 'Send Directive' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows no warning on an active project', () => {
    renderChat('a')
    expect(screen.queryByTestId('chat-inactive-project')).toBeNull()
  })
})
