import React from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { ProjectsMemoryView } from '../InfraViews'

/**
 * E28 part 2 & 3 — the project board's Disable / Enable control (`PATCH /projects/{id}` with
 * `status=disabled|active`) and the create-project form that must send `repo_url`
 * (sending only a name makes every task run against AATM's own repository, E14/E28).
 */

const project = (overrides: Record<string, any> = {}) => ({
  id: 'project-1',
  name: 'Shop API',
  slug: 'shop-api',
  description: 'Managed AATM project',
  repo_path: '.',
  repo_url: 'https://github.com/acme/shop-api',
  default_branch: 'main',
  protected_branches: ['main'],
  tech_stack: [],
  config: {},
  status: 'active',
  created_at: '2026-11-20T10:00:00Z',
  ...overrides,
})

function renderBoard({ projects = [project()], onReload = vi.fn() } = {}) {
  localStorage.setItem('aatm_language', 'en')
  const view = render(
    <I18nProvider>
      <ProjectsMemoryView
        projects={projects}
        memories={[]}
        activeProjectId={projects[0]?.id ?? null}
        onSelectProject={() => {}}
        onReload={onReload}
      />
    </I18nProvider>,
  )
  return { onReload, rerender: view.rerender }
}

describe('ProjectsMemoryView — Disable / Enable a project (E28)', () => {
  beforeEach(() => localStorage.clear())

  it('sends status=disabled only after the owner confirms', async () => {
    const { callsTo } = installFetchMock({ '/api/v1/projects/project-1': project({ status: 'disabled' }) })
    const { onReload, rerender } = renderBoard()

    fireEvent.click(screen.getByRole('button', { name: /Disable project Shop API/ }))
    // Nothing is sent before the confirmation.
    expect(callsTo('/projects/project-1')).toHaveLength(0)
    const dialog = screen.getByTestId('project-disable-confirm')
    expect(dialog.textContent).toContain('The team stops planning new work here')

    fireEvent.click(screen.getByTestId('project-disable-confirm-yes'))

    await waitFor(() => expect(callsTo('/projects/project-1')).toHaveLength(1))
    const patch = callsTo('/projects/project-1')[0]
    expect(patch.method).toBe('PATCH')
    expect(patch.body).toEqual({ status: 'disabled' })
    expect(onReload).toHaveBeenCalled()
    // The board shows the project as disabled once the reloaded list arrives (what App.tsx passes down).
    rerender(<I18nProvider><ProjectsMemoryView projects={[project({ status: 'disabled' })]} memories={[]}
      activeProjectId="project-1" onSelectProject={() => {}} onReload={onReload} /></I18nProvider>)
    await waitFor(() => expect(screen.getByTestId('project-status-project-1').textContent).toContain('Disabled'))
    expect(screen.getByRole('button', { name: /Enable project Shop API/ })).toBeInTheDocument()
    expect(screen.queryByTestId('project-disable-confirm')).toBeNull()
  })

  it('cancelling the confirmation sends nothing', async () => {
    const { callsTo } = installFetchMock()
    renderBoard()
    fireEvent.click(screen.getByRole('button', { name: /Disable project Shop API/ }))
    fireEvent.click(screen.getByTestId('project-disable-confirm-cancel'))
    expect(screen.queryByTestId('project-disable-confirm')).toBeNull()
    expect(callsTo('/projects/project-1')).toHaveLength(0)
  })

  it('enables a disabled project again without a confirmation', async () => {
    const { callsTo } = installFetchMock({ '/api/v1/projects/project-1': project({ status: 'active' }) })
    renderBoard({ projects: [project({ status: 'disabled', config: {} })] })
    expect(screen.getByTestId('project-status-project-1').textContent).toContain('Disabled')
    fireEvent.click(screen.getByRole('button', { name: /Enable project Shop API/ }))
    await waitFor(() => expect(callsTo('/projects/project-1')).toHaveLength(1))
    expect(callsTo('/projects/project-1')[0].body).toEqual({ status: 'active' })
  })

  it('does not offer Disable for the default (active) project', async () => {
    const { callsTo } = installFetchMock()
    renderBoard({ projects: [project({ config: { active: true } })] })
    expect(screen.queryByRole('button', { name: /Disable project Shop API/ })).toBeNull()
    expect(screen.getByTestId('project-default-project-1')).toBeInTheDocument()
    expect(callsTo('/projects/project-1')).toHaveLength(0)
  })

  it('respects a 409 from the server and explains it', async () => {
    const { callsTo } = installFetchMock({
      '/api/v1/projects/project-2': {
        __error: { status: 409, detail: 'Only an active project can be the default project' },
      },
    })
    renderBoard({ projects: [project(), project({ id: 'project-2', name: 'Docs Site', slug: 'docs-site', status: 'active', config: {} })] })
    fireEvent.click(screen.getByRole('button', { name: /Disable project Docs Site/ }))
    fireEvent.click(screen.getByTestId('project-disable-confirm-yes'))
    await waitFor(() => expect(screen.getByTestId('project-action-error')).toBeInTheDocument())
    expect(screen.getByTestId('project-action-error').getAttribute('data-reason')).toBe('conflict')
    expect(screen.getByTestId('project-action-error').textContent).toContain('default project')
    expect(callsTo('/projects/project-2')).toHaveLength(1)
  })
})

describe('ProjectsMemoryView — create a project with repo_url (E28)', () => {
  beforeEach(() => localStorage.clear())

  it('refuses an empty repository URL before calling the server', async () => {
    const { calls } = installFetchMock()
    renderBoard({ projects: [] })
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop API' } })
    fireEvent.click(screen.getByRole('button', { name: /^Create$/ }))
    await waitFor(() => expect(screen.getByTestId('project-form-error').textContent).toContain('GitHub repository URL'))
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('refuses a repository URL that is not a GitHub owner/name URL', async () => {
    const { calls } = installFetchMock()
    renderBoard({ projects: [] })
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop API' } })
    fireEvent.change(screen.getByLabelText('Repository URL'), { target: { value: 'https://gitlab.com/acme/shop-api' } })
    fireEvent.click(screen.getByRole('button', { name: /^Create$/ }))
    await waitFor(() => expect(screen.getByTestId('project-form-error').textContent).toContain('github.com/owner/name'))
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('posts the name and the repository URL together', async () => {
    const { callsTo } = installFetchMock({ '/api/v1/projects': project({ id: 'project-9', name: 'Shop API' }) })
    const { onReload } = renderBoard({ projects: [] })
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop API' } })
    fireEvent.change(screen.getByLabelText('Repository URL'), { target: { value: 'https://github.com/acme/shop-api.git' } })
    fireEvent.click(screen.getByRole('button', { name: /^Create$/ }))
    await waitFor(() => expect(callsTo('/projects')).toHaveLength(1))
    const post = callsTo('/projects')[0]
    expect(post.method).toBe('POST')
    expect(post.body).toEqual({ name: 'Shop API', description: 'Managed AATM project', repo_url: 'https://github.com/acme/shop-api.git' })
    expect(onReload).toHaveBeenCalled()
    expect(screen.queryByTestId('project-form-error')).toBeNull()
  })

  it('shows the server error when the name already exists (409)', async () => {
    installFetchMock({
      '/api/v1/projects': { __error: { status: 409, detail: 'Project with this name or slug already exists' } },
    })
    renderBoard({ projects: [] })
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop API' } })
    fireEvent.change(screen.getByLabelText('Repository URL'), { target: { value: 'https://github.com/acme/shop-api' } })
    fireEvent.click(screen.getByRole('button', { name: /^Create$/ }))
    await waitFor(() => expect(screen.getByTestId('project-form-error').textContent).toContain('already exists'))
    expect(screen.getByTestId('project-form-error').getAttribute('data-reason')).toBe('conflict')
  })

  it('accepts the ssh form of a GitHub URL', async () => {
    const { callsTo } = installFetchMock({ '/api/v1/projects': project({ id: 'project-9' }) })
    renderBoard({ projects: [] })
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop API' } })
    fireEvent.change(screen.getByLabelText('Repository URL'), { target: { value: 'git@github.com:acme/shop-api.git' } })
    fireEvent.click(screen.getByRole('button', { name: /^Create$/ }))
    await waitFor(() => expect(callsTo('/projects')).toHaveLength(1))
    expect(callsTo('/projects')[0].body.repo_url).toBe('git@github.com:acme/shop-api.git')
  })

  it('keeps the Arabic form labels and RTL direction', () => {
    localStorage.clear()
    localStorage.setItem('aatm_language', 'ar')
    render(
      <I18nProvider>
        <ProjectsMemoryView projects={[]} memories={[]} activeProjectId={null} onSelectProject={() => {}} onReload={() => {}} />
      </I18nProvider>,
    )
    expect(screen.getByLabelText('رابط المستودع')).toBeInTheDocument()
    expect(screen.getByLabelText('اسم المشروع')).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
  })
})
