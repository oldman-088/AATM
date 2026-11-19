import React from 'react'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../i18n/I18nContext'
import { installFetchMock, settle } from '../../test/mockApi'
import { DashboardView, AgentsView, TasksView, ManagerChatView } from '../CoreViews'
import { HomeOverview, ProjectTimeline, EmployeeCards, LiveAgentLog, PullRequestsView, RecoveryPanel } from '../DashboardViews'
import { ArenaConnectorPanel, ArenaAgentDashboard } from '../ArenaViews'
import { QAView, ApprovalsAuditView, SettingsNotificationsView } from '../GovernanceViews'
import { BrowserWallView, ProvidersView, ProjectsMemoryView, GitWorkflowsView } from '../InfraViews'

const wrap = (ui: React.ReactElement, lang: 'en' | 'ar' | 'fa' = 'ar') => {
  localStorage.setItem('aatm_language', lang)
  return render(<I18nProvider>{ui}</I18nProvider>)
}

/** Arabic is the default and must be the first copy shown in empty/error states. */
describe('Arabic-first empty & error states (RTL)', () => {
  beforeEach(() => localStorage.clear())

  describe('DashboardView', () => {
    it('shows a clear Arabic error state when the overview fetch fails', async () => {
      wrap(<DashboardView overview={null} error="boom" onRefresh={() => {}} onRunCycle={() => {}} onSelfHeal={() => {}} />, 'ar')
      expect(await screen.findByTestId('dashboard-error')).toBeInTheDocument()
      expect(screen.getByTestId('dashboard-error').textContent).toContain('تعذّر تحميل البيانات')
      expect(document.documentElement.dir).toBe('rtl')
    })

    it('shows a loading state while the overview is pending', () => {
      wrap(<DashboardView overview={null} error={null} onRefresh={() => {}} onRunCycle={() => {}} onSelfHeal={() => {}} />, 'ar')
      expect(screen.getByTestId('dashboard-loading')).toBeInTheDocument()
    })
  })

  describe('HomeOverview', () => {
    it('shows an Arabic empty state when the architecture strip is empty', async () => {
      installFetchMock({ '/api/v1/dashboard/home': { architecture: [], counters: { active_employees: 0, total_employees: 0, tasks_by_status: {}, open_prs: 0, cost_today_usd: 0, daily_budget_usd: 0, needs_human: 0 } } })
      wrap(<HomeOverview />, 'ar')
      expect(await screen.findByTestId('home-overview-empty')).toBeInTheDocument()
    })

    it('shows an Arabic error state when the home fetch fails', async () => {
      installFetchMock({ '/api/v1/dashboard/home': () => { throw new Error('down') } })
      wrap(<HomeOverview />, 'ar')
      expect(await screen.findByTestId('home-overview-error')).toBeInTheDocument()
      expect(screen.getByTestId('home-overview-error').textContent).toContain('تعذّر تحميل البيانات')
    })
  })

  describe('ProjectTimeline', () => {
    it('shows an empty state when the timeline has no steps', async () => {
      installFetchMock({ '/api/v1/projects/p1/timeline': { project_id: 'p1', project_name: 'X', steps: [] } })
      wrap(<ProjectTimeline projectId="p1" />, 'ar')
      expect(await screen.findByTestId('project-timeline-empty')).toBeInTheDocument()
    })

    it('shows an error state when the timeline fetch fails', async () => {
      installFetchMock({ '/api/v1/projects/p1/timeline': () => { throw new Error('nope') } })
      wrap(<ProjectTimeline projectId="p1" />, 'ar')
      expect(await screen.findByTestId('project-timeline-error')).toBeInTheDocument()
    })
  })

  describe('EmployeeCards', () => {
    it('shows an empty state when there are no employees', async () => {
      installFetchMock({ '/api/v1/dashboard/employees': { employees: [] } })
      wrap(<EmployeeCards />, 'ar')
      expect(await screen.findByTestId('employee-cards-empty')).toBeInTheDocument()
    })

    it('shows an error state when the employees fetch fails', async () => {
      installFetchMock({ '/api/v1/dashboard/employees': () => { throw new Error('down') } })
      wrap(<EmployeeCards />, 'ar')
      expect(await screen.findByTestId('employee-cards-error')).toBeInTheDocument()
    })
  })

  describe('LiveAgentLog', () => {
    it('shows an empty state when there are no log entries', async () => {
      installFetchMock({ '/api/v1/dashboard/agent-log': { entries: [] } })
      wrap(<LiveAgentLog />, 'ar')
      expect(await screen.findByTestId('agent-log-empty')).toBeInTheDocument()
    })

    it('shows an error state when the agent-log fetch fails', async () => {
      installFetchMock({ '/api/v1/dashboard/agent-log': () => { throw new Error('down') } })
      wrap(<LiveAgentLog />, 'ar')
      expect(await screen.findByTestId('agent-log-error')).toBeInTheDocument()
    })
  })

  describe('PullRequestsView', () => {
    it('shows an empty state when there are no pull requests', async () => {
      installFetchMock({ '/api/v1/reviews/pull-requests': [] })
      wrap(<PullRequestsView />, 'ar')
      expect(await screen.findByTestId('pr-view-empty')).toBeInTheDocument()
    })

    it('shows an error state when the pull-requests fetch fails', async () => {
      installFetchMock({ '/api/v1/reviews/pull-requests': () => { throw new Error('down') } })
      wrap(<PullRequestsView />, 'ar')
      expect(await screen.findByTestId('pr-view-error')).toBeInTheDocument()
    })
  })

  describe('RecoveryPanel', () => {
    it('shows an error state when the recovery fetch fails', async () => {
      installFetchMock({ '/api/v1/dashboard/recovery': () => { throw new Error('down') } })
      wrap(<RecoveryPanel />, 'ar')
      expect(await screen.findByTestId('recovery-panel-error')).toBeInTheDocument()
    })

    it('shows a loading state while recovery is pending', () => {
      installFetchMock({ '/api/v1/dashboard/recovery': () => new Promise(() => {}) })
      wrap(<RecoveryPanel />, 'ar')
      expect(screen.getByTestId('recovery-panel-loading')).toBeInTheDocument()
    })
  })

  describe('AgentsView', () => {
    it('shows an empty state when there are no agents', () => {
      wrap(<AgentsView agents={[]} providers={[]} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('agents-empty')).toBeInTheDocument()
    })

    it('shows an error state when agents failed to load', () => {
      wrap(<AgentsView agents={[]} providers={[]} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('agents-error')).toBeInTheDocument()
    })
  })

  describe('TasksView', () => {
    it('shows an empty state when there are no tasks', () => {
      wrap(<TasksView tasks={[]} projectId="p1" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('tasks-empty')).toBeInTheDocument()
    })

    it('shows an error state when tasks failed to load', () => {
      wrap(<TasksView tasks={[]} projectId="p1" error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('tasks-error')).toBeInTheDocument()
    })
  })

  describe('ManagerChatView', () => {
    it('shows an empty state when there are no messages', () => {
      wrap(<ManagerChatView messages={[]} projectId="p1" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('chat-empty')).toBeInTheDocument()
    })

    it('shows an error state when chat failed to load', () => {
      wrap(<ManagerChatView messages={[]} projectId="p1" error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('chat-error')).toBeInTheDocument()
    })
  })

  describe('ArenaConnectorPanel', () => {
    it('shows an error state when the arena profiles fetch fails', async () => {
      installFetchMock({ '/api/v1/arena/profiles': () => { throw new Error('down') } })
      wrap(<ArenaConnectorPanel />, 'ar')
      expect(await screen.findByTestId('arena-connector-error')).toBeInTheDocument()
    })

    it('shows an empty state when there are no profiles', async () => {
      installFetchMock({ '/api/v1/arena/profiles': { gate: { mode: 'mock', enabled: true, real_enabled: false }, profiles: [] } })
      wrap(<ArenaConnectorPanel />, 'ar')
      expect(await screen.findByTestId('arena-connector-empty')).toBeInTheDocument()
    })
  })

  describe('ArenaAgentDashboard', () => {
    it('shows an error state when the arena overview fetch fails', async () => {
      installFetchMock({ '/api/v1/arena/agent/overview': () => { throw new Error('down') } })
      wrap(<ArenaAgentDashboard />, 'ar')
      expect(await screen.findByTestId('arena-agent-error')).toBeInTheDocument()
    })

    it('shows an empty state when there are no employee rows', async () => {
      installFetchMock({
        '/api/v1/arena/agent/overview': { gate: { enabled: true, mode: 'mock', real_enabled: false }, counts: {}, employees: [] },
        '/api/v1/tasks': [],
      })
      wrap(<ArenaAgentDashboard />, 'ar')
      expect(await screen.findByTestId('arena-agent-empty')).toBeInTheDocument()
    })
  })

  describe('QAView', () => {
    it('shows an error state when qa failed to load', () => {
      wrap(<QAView qaData={null} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('qa-error')).toBeInTheDocument()
    })

    it('shows a loading state while qa is pending', () => {
      wrap(<QAView qaData={null} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('qa-loading')).toBeInTheDocument()
    })
  })

  describe('ApprovalsAuditView', () => {
    it('shows an empty state when there are no approvals', () => {
      wrap(<ApprovalsAuditView approvals={[]} auditLogs={[]} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('approvals-empty')).toBeInTheDocument()
    })

    it('shows an error state when approvals failed to load', () => {
      wrap(<ApprovalsAuditView approvals={[]} auditLogs={[]} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('approvals-error')).toBeInTheDocument()
    })

    it('shows an empty state for the audit timeline when there are no logs', () => {
      wrap(<ApprovalsAuditView approvals={[]} auditLogs={[]} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('audit-empty')).toBeInTheDocument()
    })
  })

  describe('SettingsNotificationsView', () => {
    it('shows an empty state when there are no notifications', () => {
      wrap(<SettingsNotificationsView notifications={[]} theme="dark" onToggleTheme={() => {}} onChangeLanguage={() => {}} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('notifications-empty')).toBeInTheDocument()
    })

    it('shows an error state when notifications failed to load', () => {
      wrap(<SettingsNotificationsView notifications={[]} theme="dark" error="down" onToggleTheme={() => {}} onChangeLanguage={() => {}} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('notifications-error')).toBeInTheDocument()
    })
  })

  describe('BrowserWallView', () => {
    it('shows an empty state when there are no browsers', () => {
      wrap(<BrowserWallView browsers={[]} capabilities={null} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('browsers-empty')).toBeInTheDocument()
    })

    it('shows an error state when browsers failed to load', () => {
      wrap(<BrowserWallView browsers={[]} capabilities={null} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('browsers-error')).toBeInTheDocument()
    })
  })

  describe('ProvidersView', () => {
    it('shows an empty state when there are no providers', () => {
      wrap(<ProvidersView providers={[]} usage={null} agents={[]} sessions={[]} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('providers-empty')).toBeInTheDocument()
    })

    it('shows an error state when providers failed to load', () => {
      wrap(<ProvidersView providers={[]} usage={null} agents={[]} sessions={[]} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('providers-error')).toBeInTheDocument()
    })
  })

  describe('ProjectsMemoryView', () => {
    it('shows empty states for projects and memories', () => {
      wrap(
        <ProjectsMemoryView
          projects={[]}
          memories={[]}
          activeProjectId={null}
          onSelectProject={() => {}}
          onReload={() => {}}
        />,
        'ar',
      )
      expect(screen.getByTestId('projects-empty')).toBeInTheDocument()
      expect(screen.getByTestId('memories-empty')).toBeInTheDocument()
    })
  })

  describe('GitWorkflowsView', () => {
    it('shows an error state when git failed to load', () => {
      wrap(<GitWorkflowsView gitData={null} approvals={[]} agents={[]} error="down" onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('git-error')).toBeInTheDocument()
    })

    it('shows a loading state while git is pending', () => {
      wrap(<GitWorkflowsView gitData={null} approvals={[]} agents={[]} onReload={() => {}} />, 'ar')
      expect(screen.getByTestId('git-loading')).toBeInTheDocument()
    })
  })
})

describe('EmptyState component copies are Arabic-first and RTL', () => {
  beforeEach(() => localStorage.clear())

  it('renders the Arabic empty copy by default and the document is RTL', async () => {
    installFetchMock({ '/api/v1/dashboard/employees': { employees: [] } })
    wrap(<EmployeeCards />, 'ar')
    const empty = await screen.findByTestId('employee-cards-empty')
    expect(empty.textContent).toContain('لا توجد بيانات')
    expect(document.documentElement.dir).toBe('rtl')
  })

  it('renders the English copy when the language is English', async () => {
    installFetchMock({ '/api/v1/dashboard/employees': { employees: [] } })
    wrap(<EmployeeCards />, 'en')
    expect((await screen.findByTestId('employee-cards-empty')).textContent).toContain('No data')
  })

  it('renders the Persian copy when the language is Persian', async () => {
    installFetchMock({ '/api/v1/dashboard/employees': { employees: [] } })
    wrap(<EmployeeCards />, 'fa')
    expect((await screen.findByTestId('employee-cards-empty')).textContent).toContain('دایره‌ای داده وجود ندارد')
  })
})

describe('settle helper still works for the new states', () => {
  beforeEach(() => localStorage.clear())
  it('resolves after the empty state renders', async () => {
    installFetchMock({ '/api/v1/dashboard/employees': { employees: [] } })
    wrap(<EmployeeCards />, 'ar')
    await settle()
    expect(screen.getByTestId('employee-cards-empty')).toBeInTheDocument()
  })
})
