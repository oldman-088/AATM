import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useI18n } from '../i18n/I18nContext'
import { apiFetch } from '../lib/api'
import { useBridgeData } from './useBridgeData'
import { useBridgeFeed, useDocumentHidden, useMediaQuery } from './useBridgeLive'
import CommanderConsole from './CommanderConsole'
import FocusView from './FocusView'
import Robot from './Robot'
import StationScreen from './StationScreen'
import {
  formatClock,
  prForAgent,
  robotStateFor,
  roleLabel,
  screenStatus,
  seatColor,
  seatPosition,
  stageLights,
  stageName,
  stateLabel,
  stationState,
} from './station'
import './bridge.css'

const STARS = Array.from({ length: 18 }, (_, index) => ({
  left: `${((index * 37) % 90) + 4}%`,
  top: `${((index * 53) % 70) + 12}%`,
  size: 1 + (index % 3),
  opacity: 0.5 + (index % 5) / 10,
  delay: `-${index * 3.3}s`,
}))

type WsStatus = 'connecting' | 'live' | 'offline'

type WorkstationProps = {
  agent: any
  fullName: string
  roleText: string
  employee: any
  color: string
  seatLabel: string
  station: any
  pr: any | null
  position: { left: string; top: string; tilt: string }
  extra: boolean
  dimmed: boolean
  emptyLabel: string
  stateText: string
  statusText: string
  robotState: ReturnType<typeof robotStateFor>
  dangerous: boolean
  onOpen: () => void
}

const Workstation = React.memo(function Workstation({
  agent,
  fullName,
  roleText,
  employee,
  color,
  seatLabel,
  station,
  pr,
  position,
  extra,
  dimmed,
  emptyLabel,
  stateText,
  statusText,
  robotState,
  dangerous,
  onOpen,
}: WorkstationProps) {
  return (
    <div
      className={`bridge-station${extra ? ' bridge-station--extra' : ''}${dimmed ? ' bridge-station--dimmed' : ''}`}
      style={{ left: position.left, top: position.top, ['--seat' as any]: color, ['--tilt' as any]: position.tilt }}
      data-testid={`bridge-station-${agent.id}`}
      data-state={stateText}
    >
      <button
        type="button"
        className="bridge-station__hit"
        onClick={onOpen}
        aria-label={`${seatLabel} — ${stateText}`}
        title={fullName}
        data-testid={`bridge-station-open-${agent.id}`}
      >
        <StationScreen
          station={station}
          color={color}
          branch={pr?.branch_name || agent?.git_branch || null}
          emptyLabel={emptyLabel}
          statusLabel={statusText}
          testId={`bridge-screen-${agent.id}`}
        />
        <Robot color={color} size={120} state={robotState} title={seatLabel} />
        <span className={`bridge-tag${dangerous ? ' bridge-tag--danger' : ''}`} style={{ ['--seat' as any]: color }}>
          <span className="bridge-tag__name">{seatLabel}</span>
          <span className="bridge-tag__role" title={fullName}>
            {roleText}
          </span>
          <span className="bridge-tag__state">{stateText}</span>
        </span>
      </button>
    </div>
  )
})

export function CommandBridge({
  wsStatus = 'offline',
  onOpenAdvanced,
}: {
  wsStatus?: WsStatus
  onOpenAdvanced?: (tab: string) => void
}) {
  const { t } = useI18n()
  const data = useBridgeData()
  const isMobile = useMediaQuery('(max-width: 900px)')
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const hidden = useDocumentHidden()

  const workers = useMemo(() => data.agents.filter((agent) => agent?.role !== 'manager'), [data.agents])
  const manager = useMemo(() => data.agents.find((agent) => agent?.role === 'manager') || null, [data.agents])
  const workerIds = useMemo(() => workers.map((agent) => agent.id), [workers])
  const { stations } = useBridgeFeed(data.agents.map((agent) => agent.id))

  const [focusId, setFocusId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  // The bridge owns the whole viewport: locking document scrolling removes any chance of a
  // horizontal page overflow (which, in RTL, scrolls the viewport off the visible content).
  useEffect(() => {
    const root = document.documentElement
    root.classList.add('bridge-mounted')
    return () => root.classList.remove('bridge-mounted')
  }, [])

  useEffect(() => {
    if (!focusId) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setFocusId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [focusId])

  const focusAgent = data.agents.find((agent) => agent.id === focusId) || null

  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true)
      try {
        await action()
        await data.reload()
      } catch (error: any) {
        setChatError(error?.message || t.bridge.orderFailed)
      } finally {
        setBusy(false)
      }
    },
    [data.reload, t.bridge.orderFailed],
  )

  const submitDirective = useCallback(
    async (content: string) => {
      setSending(true)
      setChatError(null)
      try {
        await apiFetch('/chat', { method: 'POST', body: JSON.stringify({ project_id: data.projectId, content }) })
        await data.reload()
      } catch (error: any) {
        setChatError(error?.message || t.bridge.orderFailed)
        throw error
      } finally {
        setSending(false)
      }
    },
    [data.projectId, data.reload, t.bridge.orderFailed],
  )

  const decideApproval = useCallback(
    async (approvalId: string, decision: 'approved' | 'rejected') => {
      await runAction(() => apiFetch(`/approvals/${approvalId}/decide`, { method: 'POST', body: JSON.stringify({ decision }) }))
    },
    [runAction],
  )

  const anyStopped = data.agents.some((agent) => agent?.is_emergency_stopped)

  const toggleEmergencyStop = useCallback(async () => {
    await runAction(() =>
      anyStopped
        ? apiFetch('/emergency-stop/clear', { method: 'POST' })
        : apiFetch('/emergency-stop', {
            method: 'POST',
            body: JSON.stringify({ scope: 'global', reason: 'Emergency stop from the command bridge' }),
          }),
    )
  }, [anyStopped, runAction])

  const stopAgent = useCallback(
    async (agentId: string) => {
      await runAction(() =>
        apiFetch('/emergency-stop', {
          method: 'POST',
          body: JSON.stringify({ scope: 'employee', target_id: agentId, reason: 'Stopped from the command bridge' }),
        }),
      )
    },
    [runAction],
  )

  const seatLabel = useCallback((index: number) => `${t.bridge.robot}-${index + 1}`, [t.bridge.robot])

  const steps = Array.isArray(data.timeline?.steps) ? data.timeline.steps : []
  const pendingApprovals = data.approvals
  const startedAt = data.projects.find((project) => project.id === data.projectId)?.created_at
  const elapsed = startedAt ? Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1000)) : null

  const crewOnline = data.agents.filter((agent) => !agent?.is_emergency_stopped && agent?.is_enabled !== false).length
  const crewTotal = data.agents.length
  const engineConfigured = Boolean(data.health?.model_status?.configured)
  const engineLabel = engineConfigured
    ? `${t.bridge.engine}: ${(data.health?.model_status?.configured_providers || []).join(', ') || t.bridge.notAvailable}`
    : `${t.bridge.engine}: ${t.bridge.noEngine}`
  const arenaLabel =
    data.arenaLabel === 'approved'
      ? t.bridge.arenaApproved
      : data.arenaLabel === 'simulated'
        ? t.bridge.arenaSimulated
        : data.arenaLabel === 'unavailable'
          ? `${t.bridge.arenaSession}: ${t.bridge.notAvailable}`
          : t.bridge.notAvailable
  const missionLabel = data.missionName || t.bridge.missionUnavailable
  const wsLabel = wsStatus === 'live' ? t.bridge.live : wsStatus === 'connecting' ? t.bridge.sending : t.common.liveOffline

  const needsHumanAgents = data.agents.filter(
    (agent) => agent?.is_emergency_stopped || data.employees[agent.id]?.connector_status === 'needs_human',
  )
  const pendingPrs = data.prs.filter((pr) => (pr.merge_approvals || []).some((approval: any) => approval.status === 'pending'))
  const statusParts: string[] = []
  for (const pr of pendingPrs.slice(0, 2)) statusParts.push(`PR #${pr.pr_number} · ${t.bridge.approvals}`)
  for (const agent of needsHumanAgents.slice(0, 2)) {
    const index = workers.findIndex((worker) => worker.id === agent.id)
    statusParts.push(`${seatLabel(Math.max(0, index))} · ${t.bridge.needsHuman}`)
  }
  const statusLine = statusParts.length ? statusParts.join(' · ') : t.bridge.statusEmpty

  const emptyState = !data.hasEngine || !data.hasProject || !data.hasDirective

  const renderStation = (agent: any, index: number) => {
    const station = stations[agent.id]
    const employee = data.employees[agent.id]
    const state = stationState(agent, employee, station)
    const pr = prForAgent(data.prs, agent.id)
    const { position, extra } = seatPosition(index)
    const label = seatLabel(index)
    return (
      <Workstation
        key={agent.id}
        agent={agent}
        fullName={agent?.name || agent?.id}
        roleText={roleLabel(agent, t)}
        employee={employee}
        color={seatColor(index)}
        seatLabel={label}
        station={station}
        pr={pr}
        position={position}
        extra={extra}
        dimmed={Boolean(focusId && focusId !== agent.id)}
        emptyLabel={t.bridge.waitingTask}
        stateText={stateLabel(state, t)}
        statusText={screenStatus(station, state, pr?.pr_number)}
        robotState={robotStateFor(state)}
        dangerous={state === 'needs_human'}
        onOpen={() => setFocusId(agent.id)}
      />
    )
  }

  const managerState = manager ? stationState(manager, data.employees[manager.id], stations[manager.id]) : 'idle'

  const focusView = focusAgent ? (
    <FocusView
      agent={focusAgent}
      employee={data.employees[focusAgent.id]}
      station={stations[focusAgent.id]}
      color={seatColor(Math.max(0, data.agents.findIndex((agent) => agent.id === focusAgent.id)))}
      seatLabel={
        focusAgent.id === manager?.id
          ? `${t.bridge.managerTitle}`
          : seatLabel(Math.max(0, workers.findIndex((worker) => worker.id === focusAgent.id)))
      }
      state={stationState(focusAgent, data.employees[focusAgent.id], stations[focusAgent.id])}
      pr={prForAgent(data.prs, focusAgent.id)}
      arena={data.arena}
      arenaLabel={data.arenaLabel}
      profileId={data.arenaProfiles[focusAgent.id] || null}
      costUsd={data.employees[focusAgent.id]?.cost_usd || 0}
      busy={busy}
      onClose={() => setFocusId(null)}
      onTogglePause={() =>
        runAction(() =>
          apiFetch(`/agents/${focusAgent.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ is_enabled: focusAgent.is_enabled === false }),
          }),
        )
      }
      onReplace={() => runAction(() => apiFetch(`/employees/${focusAgent.id}/replace`, { method: 'POST' }))}
      onResumeConnector={() => {
        const profileId = data.arenaProfiles[focusAgent.id]
        if (profileId) void runAction(() => apiFetch(`/arena/profiles/${profileId}/resume`, { method: 'POST' }))
      }}
      onStop={() => void stopAgent(focusAgent.id)}
      mobile={isMobile}
    />
  ) : null

  // Slim onboarding strip that lives inside the commander console: it must never cover the
  // workstations or the manager, and it disappears while a station is focused.
  const onboardingBanner = emptyState ? (
    <div className="bridge-onboard" data-testid="bridge-onboarding">
      <span className="bridge-onboard__title">{t.bridge.onboardTitle}</span>
      <ol className="bridge-onboard__steps">
        {[
          { done: data.hasEngine, label: t.bridge.onboardEngine, tab: 'providers' },
          { done: data.hasProject, label: t.bridge.onboardRepo, tab: 'projects' },
          { done: data.hasDirective, label: t.bridge.onboardOrder, tab: 'chat' },
        ].map((step, index) => (
          <li key={step.label} className={`bridge-onboard__step${step.done ? ' bridge-onboard__step--done' : ''}`}>
            <span className="bridge-onboard__check" aria-hidden="true">
              {step.done ? '✓' : index + 1}
            </span>
            <span className="bridge-onboard__label">{step.label}</span>
            {!step.done && onOpenAdvanced && (
              <button type="button" className="bridge-adv-link bridge-onboard__link" onClick={() => onOpenAdvanced(step.tab)}>
                {t.bridge.openWizard}
              </button>
            )}
          </li>
        ))}
      </ol>
    </div>
  ) : null

  const consoleNode = (
    <CommanderConsole
      chats={data.chats}
      steps={steps}
      approvals={pendingApprovals}
      statusLine={statusLine}
      sending={sending}
      error={chatError}
      onSubmitDirective={submitDirective}
      onDecideApproval={decideApproval}
      onEmergencyStop={toggleEmergencyStop}
      anyEmergencyStopped={anyStopped}
      banner={focusId ? null : onboardingBanner}
      mobile={isMobile}
    />
  )

  return (
    <div
      className={`bridge-root${hidden ? ' is-hidden' : ''}`}
      data-testid="command-bridge"
      data-reduced-motion={reducedMotion ? 'true' : 'false'}
    >
      <div className="bridge-window" aria-hidden="true">
        <div className="bridge-window__ribs" />
        <div className="bridge-stars">
          {STARS.map((star, index) => (
            <span
              key={index}
              className="bridge-star"
              style={{
                left: star.left,
                top: star.top,
                width: star.size,
                height: star.size,
                opacity: star.opacity,
                animationDelay: star.delay,
              }}
            />
          ))}
        </div>
      </div>
      <div className="bridge-planet" aria-hidden="true" />
      <div className="bridge-floor" aria-hidden="true" />

      <header className="bridge-hud" data-testid="bridge-hud">
        <span className="bridge-logo">AATM // COMMAND BRIDGE</span>
        <span className="bridge-hud__mission">
          {t.bridge.mission}: {missionLabel}
        </span>
        <span className="bridge-hud__spacer" />
        <span className="bridge-chip bridge-chip--ok" data-testid="bridge-crew-chip">
          <span className="bridge-dot" aria-hidden="true" />
          {t.bridge.crew} {crewOnline}/{crewTotal} {t.bridge.connected}
        </span>
        <span className={`bridge-chip ${engineConfigured ? 'bridge-chip--muted' : 'bridge-chip--warn'}`} data-testid="bridge-engine-chip">
          {engineLabel}
        </span>
        <span className={`bridge-chip ${data.arenaLabel === 'approved' ? 'bridge-chip--ok' : 'bridge-chip--warn'}`} data-testid="bridge-arena-chip">
          {arenaLabel}
        </span>
        <span className="bridge-chip bridge-chip--muted bridge-mono bridge-timer" data-testid="bridge-timer">
          {`${t.bridge.missionTimer}${formatClock(elapsed)}`}
        </span>
        <span
          className={`bridge-chip ${wsStatus === 'live' ? 'bridge-chip--ok' : wsStatus === 'connecting' ? 'bridge-chip--warn' : 'bridge-chip--danger'}`}
          data-testid="bridge-ws-chip"
        >
          {wsLabel}
        </span>
        {onOpenAdvanced && (
          <button type="button" className="bridge-adv-link" onClick={() => onOpenAdvanced('dashboard')} data-testid="bridge-advanced">
            {t.bridge.advanced}
          </button>
        )}
      </header>

      {isMobile ? (
        <div className="bridge-mobile" data-testid="bridge-mobile">
          <div className="bridge-mobile__head">
            <span>{missionLabel}</span>
            <span className="bridge-mono" dir="ltr">
              {`${t.bridge.mobileStations} · ${crewOnline}/${crewTotal}`}
            </span>
          </div>
          {manager && (
            <button
              type="button"
              className="bridge-mobile__card"
              style={{ ['--seat' as any]: '#A78BFA' }}
              onClick={() => setFocusId(manager.id)}
              title={manager?.name || t.bridge.managerTitle}
              data-testid="bridge-mobile-manager"
            >
              <span className="bridge-mobile__row">
                <Robot color="#A78BFA" size={52} state={robotStateFor(managerState)} manager title={t.bridge.managerTitle} />
                <span>
                  <span className="bridge-mobile__name">
                    {t.bridge.managerTitle} · {t.bridge.managerTag}
                  </span>
                  <br />
                  <span className="bridge-mobile__state">{stateLabel(managerState, t)}</span>
                </span>
              </span>
            </button>
          )}
          <div style={{ fontSize: 11, letterSpacing: 1.5, color: 'var(--muted)' }}>{t.bridge.mobileStations}</div>
          {workers.map((agent, index) => {
            const station = stations[agent.id]
            const employee = data.employees[agent.id]
            const state = stationState(agent, employee, station)
            const pr = prForAgent(data.prs, agent.id)
            const color = seatColor(index)
            return (
              <button
                key={agent.id}
                type="button"
                className="bridge-mobile__card"
                style={{ ['--seat' as any]: color }}
                onClick={() => setFocusId(agent.id)}
                title={agent?.name || agent?.id}
                data-testid={`bridge-mobile-station-${agent.id}`}
              >
                <span className="bridge-mobile__row">
                  <Robot color={color} size={46} state={robotStateFor(state)} title={seatLabel(index)} />
                  <span className="bridge-mobile__text">
                    <span className="bridge-mobile__name">
                      {seatLabel(index)} · {roleLabel(agent, t)}
                    </span>
                    <br />
                    <span className="bridge-mobile__state">{stateLabel(state, t)}</span>
                  </span>
                </span>
                <span className="bridge-mobile__code bridge-mono">
                  {(station?.lines || []).slice(-9).map((line: any, lineIndex: number) => (
                    <span key={lineIndex} className="bridge-code-line">
                      <span className="bridge-code-line__text">{line.text || ' '}</span>
                    </span>
                  ))}
                  {(station?.lines || []).length === 0 && <span style={{ color: 'var(--dim)' }}>{t.bridge.waitingTask}</span>}
                </span>
                <span className="bridge-mono" style={{ fontSize: 10, color: 'var(--dim)' }}>
                  {screenStatus(station, state, pr?.pr_number)}
                </span>
              </button>
            )
          })}
          {focusView}
        </div>
      ) : (
        <>
          <div className="bridge-stage">
            {workers.map((agent, index) => renderStation(agent, index))}
          </div>

          {manager && (
            <div className={`bridge-manager${focusId && focusId !== manager.id ? ' bridge-manager--dimmed' : ''}`} data-testid="bridge-manager">
              <div className="bridge-holo" aria-hidden="true">
                <div className="bridge-holo__beam" />
                <div className="bridge-holo__base" />
                {stageLights(steps).map((light) => (
                  <div
                    key={`${light.left}-${light.bottom}`}
                    className={`bridge-stage-light bridge-stage-light--${light.state}`}
                    style={{ left: light.left, bottom: light.bottom }}
                    title={stageName(light.name, t)}
                    data-testid={`bridge-light-${light.name}`}
                    data-state={light.state}
                  />
                ))}
              </div>
              <button
                type="button"
                className="bridge-station__hit"
                onClick={() => setFocusId(manager.id)}
                aria-label={`${t.bridge.managerTitle} — ${stateLabel(managerState, t)}`}
                title={manager?.name || t.bridge.managerTitle}
              >
                <Robot color="#A78BFA" size={150} state={robotStateFor(managerState)} manager title={t.bridge.managerTitle} />
                <span className="bridge-tag bridge-manager__tag">
                  <span className="bridge-tag__name">{t.bridge.managerTitle}</span>
                  <span className="bridge-tag__role">{t.bridge.managerTag}</span>
                  <span className="bridge-tag__state">{stateLabel(managerState, t)}</span>
                </span>
              </button>
            </div>
          )}

          {focusView}
        </>
      )}

      {consoleNode}

      {data.error && (
        <div className="bridge-chip bridge-chip--danger" style={{ position: 'absolute', top: 70, insetInlineStart: 24, zIndex: 40 }}>
          {data.error}
        </div>
      )}
    </div>
  )
}

export default CommandBridge
