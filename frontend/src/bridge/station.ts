import type { Translations } from '../i18n/types'
import type { StationCode } from './useBridgeLive'

/** Employee seat colours, in seat order (spec §2.1). */
export const SEAT_COLORS = ['#22D3EE', '#34D399', '#A78BFA', '#F97316', '#F472B6', '#FACC15', '#60A5FA', '#2DD4BF']

export type SeatPosition = { left: string; top: string; tilt: string }

/** The 4 default workstations form an arc: outer seats higher, inner seats lower (spec §2.4). */
export const SEAT_POSITIONS: SeatPosition[] = [
  { left: '2.5%', top: '13%', tilt: '18deg' },
  { left: '22%', top: '30%', tilt: '10deg' },
  { left: '55%', top: '30%', tilt: '-10deg' },
  { left: '76.5%', top: '13%', tilt: '-18deg' },
]

/** Extra employees sit in a second, smaller arc behind the first (scale 0.8). */
export const EXTRA_POSITIONS: SeatPosition[] = [
  { left: '12%', top: '22%', tilt: '14deg' },
  { left: '32%', top: '38%', tilt: '6deg' },
  { left: '56%', top: '38%', tilt: '-6deg' },
  { left: '74%', top: '22%', tilt: '-14deg' },
]

export function seatColor(index: number): string {
  return SEAT_COLORS[index % SEAT_COLORS.length]
}

export function seatPosition(index: number): { position: SeatPosition; extra: boolean } {
  if (index < SEAT_POSITIONS.length) return { position: SEAT_POSITIONS[index], extra: false }
  const slot = EXTRA_POSITIONS[(index - SEAT_POSITIONS.length) % EXTRA_POSITIONS.length]
  const row = Math.floor((index - SEAT_POSITIONS.length) / EXTRA_POSITIONS.length)
  const top = Math.min(72, parseFloat(slot.top) + row * 10)
  return { position: { ...slot, top: `${top}%` }, extra: true }
}

export type RobotState = 'idle' | 'writing' | 'thinking' | 'needs_human' | 'done'

export type StationState = 'idle' | 'writing' | 'testing' | 'reviewing' | 'thinking' | 'needs_human' | 'paused' | 'done'

/** Map real agent/connector/frame state onto the robot animation state (spec §2.4). */
export function stationState(agent: any, employee: any, station?: StationCode): StationState {
  if (agent?.is_emergency_stopped || employee?.connector_status === 'needs_human') return 'needs_human'
  if (agent?.is_enabled === false) return 'paused'
  if (station?.action === 'test' || station?.action === 'lint') return 'testing'
  if (station?.action === 'review') return 'reviewing'
  if (station?.action === 'write' || station?.action === 'edit') return 'writing'
  if (agent?.status === 'working') return 'thinking'
  if (agent?.status === 'offline') return 'idle'
  return 'idle'
}

export function robotStateFor(state: StationState): RobotState {
  switch (state) {
    case 'writing':
    case 'testing':
    case 'reviewing':
      return 'writing'
    case 'thinking':
      return 'thinking'
    case 'needs_human':
      return 'needs_human'
    case 'done':
      return 'done'
    default:
      return 'idle'
  }
}

export function stateLabel(state: StationState, t: Translations): string {
  switch (state) {
    case 'writing':
      return t.bridge.writing
    case 'testing':
      return t.bridge.testing
    case 'reviewing':
      return t.bridge.reviewing
    case 'thinking':
      return t.bridge.thinking
    case 'needs_human':
      return t.bridge.needsHuman
    case 'paused':
      return t.bridge.paused
    case 'done':
      return t.bridge.doneLabel
    default:
      return t.bridge.idleLabel
  }
}

/** Monospace footer line on the holo-screen — only real values, otherwise a neutral label. */
export function screenStatus(station: StationCode | undefined, state: StationState, prNumber?: number | null): string {
  if (state === 'needs_human') return 'paused: needs_human'
  if (state === 'paused') return 'paused'
  if (!station) return prNumber ? `pushed · draft PR #${prNumber}` : 'idle'
  if (station.action === 'test' || station.action === 'lint') {
    const command = station.command || (station.action === 'lint' ? 'ruff check' : 'pytest -q')
    const exit = typeof station.exitCode === 'number' ? ` · exit ${station.exitCode}` : ' · running…'
    return `${command}${exit}`
  }
  if (station.action === 'review') return `review ${station.reviewEvent || 'COMMENT'}`
  if (station.action === 'write' || station.action === 'edit') {
    return `${station.action === 'edit' ? 'editing' : 'writing'}… ${station.path || ''}`.trim()
  }
  if (prNumber) return `pushed · draft PR #${prNumber}`
  return station.path || 'idle'
}

export function prForAgent(prs: any[], agentId: string | undefined): any | null {
  if (!agentId) return null
  return prs.find((pr) => pr.assigned_agent_id === agentId) || null
}

export function basename(path?: string | null): string {
  if (!path) return ''
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

export function formatClock(totalSeconds: number | null): string {
  if (totalSeconds === null || totalSeconds < 0) return '—'
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
}

export const STAGE_IDS = [
  'request',
  'analyze_split',
  'distribute',
  'execute',
  'collect',
  'pull_request',
  'review_merge',
  'completed',
] as const

export function stageName(id: string, t: Translations): string {
  const map: Record<string, string> = {
    request: t.home.stepRequest,
    analyze_split: t.home.stepAnalyze,
    distribute: t.home.stepDistribute,
    execute: t.home.stepExecute,
    collect: t.home.stepCollect,
    pull_request: t.home.stepPr,
    review_merge: t.home.stepReview,
    completed: t.home.stepCompleted,
  }
  return map[id] || id
}

export function stageLights(steps: any[] | undefined): Array<{ left: number; bottom: number; state: string; name: string }> {
  const offsets = [
    { left: 6, bottom: 40 },
    { left: 30, bottom: 70 },
    { left: 70, bottom: 92 },
    { left: 110, bottom: 100 },
    { left: 150, bottom: 92 },
    { left: 186, bottom: 70 },
    { left: 200, bottom: 40 },
    { left: 103, bottom: 8 },
  ]
  return offsets.map((offset, index) => ({
    ...offset,
    state: steps?.[index]?.state || 'pending',
    name: steps?.[index]?.id || STAGE_IDS[index],
  }))
}
