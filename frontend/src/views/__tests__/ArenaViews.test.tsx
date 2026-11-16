import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { ArenaConnectorPanel } from '../ArenaViews'

function renderWith(lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider><ArenaConnectorPanel /></I18nProvider>)
}

const card = (overrides: Record<string, any> = {}) => ({
  agent_id: 'worker-1', agent_name: 'Backend Engineer', agent_role: 'worker', color: '#6366f1',
  profile_id: 'profile-backend-docs', connector_status: 'offline', health: 'yellow', token_issued: false,
  needs_human_reason: null, ...overrides,
})

describe('ArenaConnectorPanel (Phase 6)', () => {
  beforeEach(() => localStorage.clear())

  it('shows that the connector is off and disables script generation while it is disabled', async () => {
    installFetchMock({ '/api/v1/arena/profiles': { gate: { mode: 'disabled', enabled: false, real_enabled: false }, profiles: [card()] } })
    renderWith()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('ARENA_CONNECTOR_ENABLED=true')
    expect((screen.getByRole('button', { name: /Generate userscript/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('generates a reviewable userscript in mock mode and resumes a needs_human profile', async () => {
    const { calls } = installFetchMock({
      '/api/v1/arena/profiles/profile-backend-docs/userscript': { script: '// ==UserScript==\n// @grant        none' },
      '/api/v1/arena/profiles/profile-backend-docs/resume': { requeued_tasks: ['t1'] },
      '/api/v1/arena/profiles': { gate: { mode: 'mock', enabled: true, real_enabled: false },
        profiles: [card({ connector_status: 'needs_human', health: 'red', needs_human_reason: 'human check element on page' })] },
    })
    renderWith()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('mock Arena page')
    fireEvent.click(screen.getByRole('button', { name: /Generate userscript/ }))
    await waitFor(() => expect(screen.getByTestId('arena-script').textContent).toContain('Tampermonkey'))
    expect((screen.getByLabelText(/Review this script/) as HTMLTextAreaElement).value).toContain('@grant        none')
    fireEvent.click(screen.getByRole('button', { name: /Resume/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/resume') && c.method === 'POST')).toBeDefined())
    expect(screen.getByText('human check element on page', { exact: false })).toBeTruthy()
  })

  it('renders Arabic RTL labels', async () => {
    installFetchMock({ '/api/v1/arena/profiles': { gate: { mode: 'disabled', enabled: false, real_enabled: false }, profiles: [] } })
    renderWith('ar')
    expect(await screen.findByText('موصّل Arena عبر المتصفح')).toBeTruthy()
    expect((await screen.findByTestId('arena-gate')).textContent).toContain('موصل Arena متوقف')
  })
})
