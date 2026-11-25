/**
 * NEXUS COMMAND information architecture: 8 sections (owner spec §3A), each with sub-views.
 * The URL hash (#/section/sub) keeps the place on reload and lets links point at a screen.
 */
import type { NexusStrings } from '../i18n/nexus'

export type SectionId = 'command' | 'executive' | 'workforce' | 'projects' | 'org' | 'integrations' | 'analytics' | 'admin'

export const SECTION_SUBS: Record<SectionId, Array<keyof NexusStrings['sub']>> = {
  command: ['overview', 'mission', 'bridge'],
  executive: ['chat', 'plans', 'approvals'],
  workforce: ['agents', 'team', 'workspaces', 'messaging'],
  projects: ['tasks', 'projectsList', 'pullRequests'],
  org: ['orgMap'],
  integrations: ['integrationsOverview', 'providers', 'browsers', 'devices'],
  analytics: ['metrics', 'quality'],
  admin: ['settings', 'audit', 'security', 'users'],
}

export const SECTION_ORDER = Object.keys(SECTION_SUBS) as SectionId[]

export interface Route { section: SectionId; sub: string }

export const DEFAULT_ROUTE: Route = { section: 'command', sub: 'overview' }

export function parseHash(hash: string): Route {
  const [section, sub] = hash.replace(/^#\/?/, '').split('/')
  if (!SECTION_ORDER.includes(section as SectionId)) return DEFAULT_ROUTE
  const subs = SECTION_SUBS[section as SectionId] as string[]
  return { section: section as SectionId, sub: subs.includes(sub) ? sub : subs[0] }
}

export function routeHash(route: Route): string {
  return `#/${route.section}/${route.sub}`
}

/**
 * Old screen ids (before NEXUS) still arrive from other components, for example the Command Bridge
 * or Needs-You links. Map each to its new home so no link breaks.
 */
export function legacyRoute(tab: string): Route {
  const map: Record<string, Route> = {
    bridge: { section: 'command', sub: 'bridge' },
    dashboard: { section: 'command', sub: 'mission' },
    home: { section: 'command', sub: 'mission' },
    chat: { section: 'executive', sub: 'chat' },
    approvals: { section: 'executive', sub: 'approvals' },
    agents: { section: 'workforce', sub: 'agents' },
    workspaces: { section: 'workforce', sub: 'workspaces' },
    tasks: { section: 'projects', sub: 'tasks' },
    projects: { section: 'projects', sub: 'projectsList' },
    git: { section: 'projects', sub: 'pullRequests' },
    browsers: { section: 'integrations', sub: 'browsers' },
    providers: { section: 'integrations', sub: 'providers' },
    qa: { section: 'analytics', sub: 'quality' },
    settings: { section: 'admin', sub: 'settings' },
    recovery: { section: 'integrations', sub: 'browsers' },
  }
  if (map[tab]) return map[tab]
  return parseHash(tab.startsWith('#') ? tab : `#/${tab}`)
}
