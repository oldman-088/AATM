import React from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock } from '../../test/mockApi'
import { ManagerDraftPanel } from '../ArenaViews'

/**
 * E28: the owner's Approve / Edit / Reject buttons on a manager plan draft.
 *
 * The panel renders only for a manager message that carries a plan, calls the three draft
 * endpoints of `backend/app/api/routes/arena.py` (~356-406) and keeps the button names, error
 * copy and direction (RTL) identical in EN / AR / FA.
 */

const PLAN = {
  summary_ar: 'خطة مسودة لبناء لوحة القيادة',
  tasks: [
    { title: 'Build the dashboard shell', assignee: 'worker-1', description: 'Create the layout.', acceptance_criteria: ['Layout renders'], depends_on: [] },
    { title: 'Wire the API', assignee: 'worker-2', description: 'Connect the endpoints.', acceptance_criteria: ['Data loads'], depends_on: [0] },
  ],
}

/** A manager chat message carrying a draft plan (the shape ManagerArenaRuntime._make_draft writes). */
const draftMessage = (overrides: Record<string, any> = {}) => ({
  id: 'msg-1',
  project_id: 'project-1',
  sender_type: 'manager',
  sender_id: 'manager-0',
  recipient_id: 'user',
  content: 'مسودة خطة بانتظار موافقتك (اعتماد / تعديل / رفض):\n- Build the dashboard shell ← worker-1',
  created_at: '2026-11-23T09:00:00Z',
  metadata: {
    runtime: 'arena_agent',
    manager_job_id: 'job-draft-1',
    job_kind: 'plan',
    job_status: 'draft',
    draft: true,
    plan: PLAN,
    draft_reasons: ['The plan has 6 tasks; the maximum without approval is 5.'],
    warnings: [],
    ...overrides,
  },
})

function renderPanel(message: any = draftMessage(), lang: 'en' | 'ar' | 'fa' = 'en', onUpdated: () => void = () => {}) {
  localStorage.setItem('aatm_language', lang)
  return render(
    <I18nProvider>
      <ManagerDraftPanel message={message} onUpdated={onUpdated} />
    </I18nProvider>,
  )
}

describe('ManagerDraftPanel — Approve / Edit / Reject (E28)', () => {
  beforeEach(() => localStorage.clear())

  it('approves the draft through the approve endpoint and shows the created tasks', async () => {
    const { callsTo } = installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/approve': { id: 'job-draft-1', status: 'done', task_ids: ['task-1', 'task-2'] },
    })
    renderPanel()

    // Every task of the draft is listed so the owner decides on real content.
    expect(screen.getByTestId('manager-draft-tasks').textContent).toContain('Build the dashboard shell')
    expect(screen.getByTestId('manager-draft-tasks').textContent).toContain('Wire the API')
    expect(screen.getByTestId('manager-draft-reasons').textContent).toContain('The plan has 6 tasks')

    fireEvent.click(screen.getByRole('button', { name: /Approve plan/ }))

    await waitFor(() => expect(screen.getByTestId('manager-draft-status').textContent).toContain('Plan approved'))
    const approve = callsTo('/draft/approve')
    expect(approve).toHaveLength(1)
    expect(approve[0].method).toBe('POST')
    expect(approve[0].path).toBe('/api/v1/arena/manager/jobs/job-draft-1/draft/approve')
    expect(callsTo('/draft/reject')).toHaveLength(0)
    // The decision buttons disappear once the draft is no longer a draft.
    expect(screen.queryByRole('button', { name: /Approve plan/ })).toBeNull()
    expect(screen.getByTestId('manager-draft-status').textContent).toContain('task-1')
  })

  it('opens the JSON editor and saves the edited plan with PUT on the draft endpoint', async () => {
    // The route echoes the saved draft, like PUT /arena/manager/jobs/{id}/draft does.
    const { callsTo } = installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft': (_path: string, init: RequestInit) => ({
        id: 'job-draft-1', status: 'draft', draft: JSON.parse(String(init.body)), warnings: [], draft_reasons: [],
      }),
    })
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: /Edit plan/ }))
    const editor = (await screen.findByTestId('manager-draft-editor')) as HTMLTextAreaElement
    expect(JSON.parse(editor.value).tasks).toHaveLength(2)

    const edited = { ...PLAN, tasks: [PLAN.tasks[0], { ...PLAN.tasks[1], title: 'Wire the API and the timeline' }] }
    fireEvent.change(editor, { target: { value: JSON.stringify(edited) } })
    fireEvent.click(screen.getByRole('button', { name: /Save plan/ }))

    await waitFor(() => expect(callsTo('/draft')).toHaveLength(1))
    const save = callsTo('/draft')[0]
    expect(save.method).toBe('PUT')
    expect(save.path).toBe('/api/v1/arena/manager/jobs/job-draft-1/draft')
    expect(save.body.tasks[1].title).toBe('Wire the API and the timeline')
    await waitFor(() => expect(screen.getByTestId('manager-draft-status').textContent).toContain('Plan saved'))
    // It stays a draft: the owner can still approve it after saving.
    expect(screen.getByRole('button', { name: /Approve plan/ })).toBeInTheDocument()
    expect(screen.getByTestId('manager-draft-tasks').textContent).toContain('Wire the API and the timeline')
  })

  it('rejects invalid JSON in the editor without calling the server', async () => {
    const { calls } = installFetchMock()
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Edit plan/ }))
    const editor = (await screen.findByTestId('manager-draft-editor')) as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: '{ "tasks": [' } })
    fireEvent.click(screen.getByRole('button', { name: /Save plan/ }))
    await waitFor(() => expect(screen.getByTestId('manager-draft-error').textContent).toContain('not valid JSON'))
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(0)
  })

  it('rejects the draft through the reject endpoint', async () => {
    const { callsTo } = installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/reject': { id: 'job-draft-1', status: 'failed' },
    })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Reject plan/ }))
    await waitFor(() => expect(callsTo('/draft/reject')).toHaveLength(1))
    expect(callsTo('/draft/reject')[0].method).toBe('POST')
    expect(callsTo('/draft/reject')[0].path).toBe('/api/v1/arena/manager/jobs/job-draft-1/draft/reject')
    await waitFor(() => expect(screen.getByTestId('manager-draft-status').textContent).toContain('Plan rejected'))
    expect(screen.queryByRole('button', { name: /Reject plan/ })).toBeNull()
  })

  it('notifies the parent once so the list updates without a full page reload', async () => {
    let updated = 0
    installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/approve': { id: 'job-draft-1', status: 'done', task_ids: ['task-1'] },
    })
    renderPanel(draftMessage(), 'en', () => { updated += 1 })
    fireEvent.click(screen.getByRole('button', { name: /Approve plan/ }))
    await waitFor(() => expect(updated).toBe(1))
  })

  it('shows a busy state while the decision is in flight and cannot be sent twice', async () => {
    let release: (value: any) => void = () => {}
    const pending = new Promise((resolve) => { release = resolve })
    const { callsTo } = installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/approve': (() => pending) as any,
    })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Approve plan/ }))
    await waitFor(() => expect(screen.getByTestId('manager-draft-busy')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('manager-draft-panel').querySelector('[data-testid="manager-draft-approve"]') as HTMLElement)
    expect(callsTo('/draft/approve')).toHaveLength(1)
    await act(async () => { release({ id: 'job-draft-1', status: 'done', task_ids: ['task-1'] }) })
  })
})

describe('ManagerDraftPanel — decision state and 403/409 errors (E28)', () => {
  beforeEach(() => localStorage.clear())

  it('hides the decision buttons and says so when the job is no longer a draft', () => {
    renderPanel(draftMessage({ job_status: 'done', draft: false }))
    expect(screen.queryByRole('button', { name: /Approve plan/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Edit plan/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reject plan/ })).toBeNull()
    expect(screen.getByTestId('manager-draft-decided').textContent).toContain('no longer a draft')
  })

  it('renders nothing for a manager message that carries no draft plan', () => {
    const { container } = renderPanel(draftMessage({ plan: undefined }))
    expect(container.querySelector('[data-testid="manager-draft-panel"]')).toBeNull()
  })

  it('renders nothing without a manager job id (a chat-only reply)', () => {
    const { container } = renderPanel(draftMessage({ manager_job_id: undefined }))
    expect(container.querySelector('[data-testid="manager-draft-panel"]')).toBeNull()
  })

  it('explains a 403 as “an admin role is required” and keeps the buttons usable', async () => {
    const { callsTo } = installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/approve': { __error: { status: 403, detail: 'Requires role in (admin,)' } },
    })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Approve plan/ }))
    await waitFor(() => expect(screen.getByTestId('manager-draft-error')).toBeInTheDocument())
    const alert = screen.getByTestId('manager-draft-error')
    expect(alert.getAttribute('data-reason')).toBe('forbidden')
    expect(alert.textContent).toContain('admin')
    expect(callsTo('/draft/approve')).toHaveLength(1)
    expect(screen.getByRole('button', { name: /Approve plan/ })).toBeInTheDocument()
  })

  it('explains a 409 as “the draft changed or is gone”', async () => {
    installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/reject': {
        __error: { status: 409, detail: { code: 'not_a_draft', message: 'The job is done, not a draft.' } },
      },
    })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Reject plan/ }))
    await waitFor(() => expect(screen.getByTestId('manager-draft-error')).toBeInTheDocument())
    const alert = screen.getByTestId('manager-draft-error')
    expect(alert.getAttribute('data-reason')).toBe('conflict')
    expect(alert.textContent).toContain('not a draft')
  })

  it('shows the server validation message when an edit is refused (422)', async () => {
    installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft': {
        __error: { status: 422, detail: { code: 'invalid_plan', message: "Task 1: unknown assignee 'worker-9'." } },
      },
    })
    renderPanel()
    fireEvent.click(screen.getByRole('button', { name: /Edit plan/ }))
    fireEvent.click(screen.getByRole('button', { name: /Save plan/ }))
    await waitFor(() => expect(screen.getByTestId('manager-draft-error').textContent).toContain("unknown assignee 'worker-9'"))
    expect(screen.getByTestId('manager-draft-error').getAttribute('data-reason')).toBe('validation')
  })

  it('labels every button with an accessible Arabic name and stays RTL', () => {
    renderPanel(draftMessage(), 'ar')
    expect(screen.getByRole('button', { name: 'اعتماد الخطة' })).toHaveAttribute('aria-label', 'اعتماد الخطة')
    expect(screen.getByRole('button', { name: 'تعديل الخطة' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'رفض الخطة' })).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
    expect(screen.getByTestId('manager-draft-panel').className).toContain('text-start')
  })

  it('shows Persian labels in the RTL direction', () => {
    renderPanel(draftMessage(), 'fa')
    expect(screen.getByRole('button', { name: 'تأیید برنامه' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'ویرایش برنامه' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'رد برنامه' })).toBeInTheDocument()
    expect(document.documentElement.dir).toBe('rtl')
  })

  it('keeps keyboard focus on the decision button after a failed call', async () => {
    installFetchMock({
      '/api/v1/arena/manager/jobs/job-draft-1/draft/approve': { __error: { status: 409, detail: 'gone' } },
    })
    renderPanel()
    const approve = screen.getByRole('button', { name: /Approve plan/ })
    approve.focus()
    fireEvent.click(approve)
    await waitFor(() => expect(screen.getByTestId('manager-draft-error')).toBeInTheDocument())
    expect(document.activeElement).toBe(approve)
  })
})
