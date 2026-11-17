import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { ProvidersView } from '../InfraViews'

const usage = { summary: { total_cost_usd: 0, total_tokens: 0 }, logs: [] }

function renderProviders(lang: 'en' | 'ar' | 'fa' = 'en') {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider><ProvidersView providers={[]} usage={usage} agents={[]} sessions={[]} onReload={() => {}} /></I18nProvider>)
}

const READY_AR = 'المدير متصل بـ Arena واختبار المحددات ناجح.'

describe('Provider setup wizard — Arena Agent (browser) option (R7)', () => {
  beforeEach(() => localStorage.clear())

  it('offers Arena Agent (browser) as a 4th option, tests the manager profile and assigns it to manager-0', async () => {
    const { calls } = installFetchMock({
      '/api/v1/providers/wizard/presets': { options: [{ id: 'arena-agent', provider_type: 'arena_agent', endpoint_url: 'http://127.0.0.1:4100',
        credential_ref: 'env:ARENA_BRIDGE_TOKEN', default_model: 'arena-agent-orchestrator' }] },
      '/api/v1/providers/wizard/test-connection': { success: true, detail: READY_AR, model: 'arena-agent-orchestrator', latency_ms: 0 },
      '/api/v1/providers/wizard/configure': { provider: { name: 'Arena Agent Runtime', default_model: 'arena-agent-orchestrator' },
        model_status: { status: 'CONFIGURED' }, manager_provider: 'arena_agent' },
    })
    renderProviders()
    const option = await screen.findByRole('button', { name: 'Arena Agent (browser)' })
    await waitFor(() => expect(calls.some((c) => c.path.endsWith('/providers/wizard/presets'))).toBe(true))
    fireEvent.click(option)
    expect(screen.getByTestId('wizard-arena-agent-hint').textContent).toContain('No API key')
    expect((screen.getByLabelText('Default Model') as HTMLInputElement).value).toBe('arena-agent-orchestrator')
    expect((screen.getByLabelText(/Credential Reference/) as HTMLInputElement).value).toBe('env:ARENA_BRIDGE_TOKEN')

    fireEvent.click(screen.getByRole('button', { name: /Test Connection/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/wizard/test-connection'))?.body?.provider_type).toBe('arena_agent'))
    expect(await screen.findByText(new RegExp(READY_AR))).toBeTruthy()

    const manager = screen.getByRole('checkbox', { name: /manager-0/ }) as HTMLInputElement
    expect(manager.checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Save & Activate/ }))
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/wizard/configure'))).toBeDefined())
    const body = calls.find((c) => c.path.endsWith('/wizard/configure'))!.body
    expect(body).toMatchObject({ provider_type: 'arena_agent', assign_to_manager: true, credential_ref: 'env:ARENA_BRIDGE_TOKEN',
      default_model: 'arena-agent-orchestrator', endpoint_url: 'http://127.0.0.1:4100' })
  })

  it('shows the option and the no-API-key hint in Arabic', async () => {
    installFetchMock({ '/api/v1/providers/wizard/presets': { options: [] } })
    renderProviders('ar')
    fireEvent.click(await screen.findByRole('button', { name: 'Arena Agent (المتصفح)' }))
    expect(screen.getByTestId('wizard-arena-agent-hint').textContent).toContain('بلا مفتاح API')
    expect(screen.getByText('تعيين للمدير (manager-0)')).toBeTruthy()
    expect(document.documentElement.dir).toBe('rtl')
  })
})
