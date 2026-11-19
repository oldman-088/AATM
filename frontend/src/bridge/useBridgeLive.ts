import { useCallback, useEffect, useRef, useState } from 'react'
import { LIVE_EVENT } from '../views/DashboardViews'
import { apiFetch } from '../lib/api'
import { type CodeLine, linesFromFrame } from './highlight'

/** One station's live code state, kept per employee so a WS event only touches its own station. */
export type StationCode = {
  path: string | null
  language: string
  lines: CodeLine[]
  action: 'write' | 'edit' | 'test' | 'lint' | 'review' | 'done' | string
  taskId?: string | null
  command?: string
  exitCode?: number
  ok?: boolean
  reviewEvent?: string
  truncated?: boolean
  updatedAt: number
}

export type StationMap = Record<string, StationCode>

const FLUSH_INTERVAL_MS = 100 // ≈10 applied updates / second, regardless of event volume
const MAX_QUEUE = 400
const MAX_LINES = 60
const MAX_FOCUS_LINES = 240

function applyFrame(previous: StationMap, frame: any, at: number): StationMap {
  const agentId = String(frame?.agent_id || '')
  if (!agentId) return previous
  const prior = previous[agentId]
  const action = String(frame?.action || 'write')
  const next: StationCode = {
    path: frame?.path ?? null,
    language: String(frame?.language || prior?.language || 'text'),
    lines: linesFromFrame(frame, prior?.lines),
    action,
    taskId: frame?.task_id ?? prior?.taskId ?? null,
    command: frame?.command ?? prior?.command,
    exitCode: typeof frame?.exit_code === 'number' ? frame.exit_code : undefined,
    ok: typeof frame?.ok === 'boolean' ? frame.ok : undefined,
    reviewEvent: frame?.review_event ?? prior?.reviewEvent,
    truncated: Boolean(frame?.truncated),
    updatedAt: at,
  }
  return { ...previous, [agentId]: next }
}

/**
 * Live code feed for the Bridge.
 *
 * WS events are queued and applied in one batched state update every 100 ms, so 4–20 employees
 * streaming code cannot trigger a render storm. When the tab is hidden the queue is left alone and
 * all CSS animations are paused by the root element class; the backlog is applied on return.
 */
export function useBridgeFeed(agentIds: string[]) {
  const [stations, setStations] = useState<StationMap>({})
  const queueRef = useRef<any[]>([])
  const hiddenRef = useRef(typeof document !== 'undefined' ? document.hidden : false)

  useEffect(() => {
    const onVisibility = () => {
      hiddenRef.current = document.hidden
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  useEffect(() => {
    const onEvent = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (!detail || detail.type !== 'agent.code') return
      const payload = detail.payload || {}
      const frames = Array.isArray(payload.frames) ? payload.frames : [payload]
      queueRef.current.push(...frames)
      if (queueRef.current.length > MAX_QUEUE) queueRef.current = queueRef.current.slice(-MAX_QUEUE)
    }
    window.addEventListener(LIVE_EVENT, onEvent)
    return () => window.removeEventListener(LIVE_EVENT, onEvent)
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (hiddenRef.current) return
      const frames = queueRef.current
      if (!frames.length) return
      queueRef.current = []
      const at = Date.now()
      setStations((previous) => frames.reduce((acc, frame) => applyFrame(acc, frame, at), previous))
    }, FLUSH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [])

  const hydrate = useCallback(async () => {
    try {
      const bulk = await apiFetch<any>('/code-stream?limit=12')
      const grouped: Record<string, any[]> = bulk?.agents || {}
      setStations((previous) => {
        let next = previous
        for (const [agentId, frames] of Object.entries(grouped)) {
          for (const frame of Array.isArray(frames) ? frames : []) {
            next = applyFrame(next, { ...frame, agent_id: agentId }, Date.now())
          }
        }
        return next
      })
    } catch {
      // The buffer endpoint is optional on first paint; the WS stream still fills the screens.
    }
  }, [])

  useEffect(() => {
    if (agentIds.length) void hydrate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentIds.join('|'), hydrate])

  return { stations, hydrate }
}

/** Cuts a station's code lines down to what fits on a screen (focus view gets more). */
export function visibleLines(station: StationCode | undefined, focus = false): CodeLine[] {
  const limit = focus ? MAX_FOCUS_LINES : MAX_LINES
  if (!station?.lines?.length) return []
  return station.lines.slice(-limit)
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false
    return window.matchMedia(query).matches
  })
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const media = window.matchMedia(query)
    const listener = () => setMatches(media.matches)
    listener()
    media.addEventListener?.('change', listener)
    return () => media.removeEventListener?.('change', listener)
  }, [query])
  return matches
}

export function useDocumentHidden(): boolean {
  const [hidden, setHidden] = useState(() => (typeof document !== 'undefined' ? document.hidden : false))
  useEffect(() => {
    const listener = () => setHidden(document.hidden)
    document.addEventListener('visibilitychange', listener)
    return () => document.removeEventListener('visibilitychange', listener)
  }, [])
  return hidden
}
