import React from 'react'
import { useI18n } from '../i18n/I18nContext'
import Robot from './Robot'
import StationScreen from './StationScreen'
import { robotStateFor, roleLabel, screenStatus, type StationState } from './station'
import type { StationCode } from './useBridgeLive'

type FocusViewProps = {
  agent: any
  employee: any
  station?: StationCode
  color: string
  seatLabel: string
  state: StationState
  pr: any | null
  arena: any
  arenaLabel?: string
  profileId?: string | null
  costUsd: number
  busy?: boolean
  onClose: () => void
  onTogglePause: () => void
  onReplace: () => void
  onResumeConnector: () => void
  onStop: () => void
  mobile?: boolean
}

/**
 * Zoomed station (spec §2.7): a large readable screen, the robot below it and a side panel with
 * only the controls the backend really supports. On mobile the same details render under the
 * screen so tapping a card still gives the full picture.
 */
export function FocusView({
  agent,
  employee,
  station,
  color,
  seatLabel,
  state,
  pr,
  arena,
  arenaLabel: arenaState,
  profileId,
  costUsd,
  busy = false,
  onClose,
  onTogglePause,
  onReplace,
  onResumeConnector,
  onStop,
  mobile = false,
}: FocusViewProps) {
  const { t } = useI18n()
  const task = employee?.current_task || null
  const isWorker = agent?.role !== 'manager'
  const connectorRaw = employee?.connector_status || null
  const connector =
    connectorRaw === 'needs_human'
      ? t.bridge.needsHuman
      : connectorRaw === 'busy'
        ? t.bridge.connectorBusy
        : connectorRaw === 'idle'
          ? t.bridge.connectorIdle
          : connectorRaw === 'offline'
            ? t.bridge.connectorOffline
            : connectorRaw
  const progress = typeof task?.progress === 'number' ? Math.max(0, Math.min(100, task.progress)) : null
  const lastTest = station && (station.action === 'test' || station.action === 'lint') ? station : null
  const arenaLabel =
    arenaState === 'approved'
      ? t.bridge.arenaApproved
      : arenaState === 'simulated'
        ? t.bridge.arenaSimulated
        : arenaState === 'unavailable'
          ? t.bridge.notAvailable
          : arena
            ? t.bridge.notAvailable
            : t.bridge.notAvailable

  const row = (label: string, value: React.ReactNode) => (
    <div className="bridge-side__row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )

  const details = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div className="bridge-side__title" style={{ fontSize: 18 }}>
        <span className="bridge-side__dot" style={{ background: color, color }} />
        {seatLabel}
        <span className="bridge-side__role" style={{ marginTop: 0, fontSize: 12 }}>
          {roleLabel(agent, t)}
        </span>
      </div>
      {/* the seeded agent names are long English strings — full name lives here and in the tooltip */}
      <div className="bridge-side__fullname" title={agent?.name || ''} data-testid="bridge-focus-full-name">
        {agent?.name || agent?.id}
      </div>
      <dl style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {row(
          t.bridge.engine,
          <span dir="ltr">{employee?.provider ? `${employee.provider} · ${employee.model || ''}` : agent?.model_name || t.bridge.notAvailable}</span>,
        )}
        {row(t.bridge.arenaSession, arenaLabel)}
        {row(t.bridge.tampermonkey, connector || t.bridge.notAvailable)}
        {row(t.bridge.task, task?.title || t.bridge.notAvailable)}
        {progress !== null && (
          <div className="bridge-side__row" style={{ display: 'block' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
              <span style={{ color: 'var(--dim)' }}>{t.bridge.progress}</span>
              <span dir="ltr">{progress}%</span>
            </div>
            <div className="bridge-progress">
              <div className="bridge-progress__fill" style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}
        {row(
          t.bridge.tests,
          lastTest ? (
            <span style={{ color: lastTest.ok ? 'var(--done)' : 'var(--danger-text)' }} dir="ltr">
              {lastTest.ok ? t.bridge.testPassed : t.bridge.testFailed}
              {typeof lastTest.exitCode === 'number' ? ` · ${lastTest.exitCode}` : ''}
            </span>
          ) : (
            t.bridge.notAvailable
          ),
        )}
        {row(t.bridge.cost, <span dir="ltr">{`$${Number(costUsd || 0).toFixed(4)}`}</span>)}
        {row(
          t.bridge.pullRequest,
          pr?.pr_number ? (
            pr.pr_url ? (
              <a href={pr.pr_url} target="_blank" rel="noreferrer" style={{ color: '#93c5fd' }} dir="ltr">
                #{pr.pr_number}
              </a>
            ) : (
              `#${pr.pr_number}`
            )
          ) : (
            t.bridge.notAvailable
          ),
        )}
      </dl>
    </div>
  )

  const controls = (
    <div className="bridge-side__controls">
      <button type="button" className="bridge-btn bridge-btn--violet" onClick={onTogglePause} disabled={busy} data-testid="bridge-control-pause">
        {agent?.is_enabled === false ? t.bridge.unpause : t.bridge.pause}
      </button>
      {isWorker && (
        <button type="button" className="bridge-btn bridge-btn--violet" onClick={onReplace} disabled={busy} data-testid="bridge-control-replace">
          {t.bridge.replace}
        </button>
      )}
      {state === 'needs_human' && profileId && (
        <button type="button" className="bridge-btn bridge-btn--green" onClick={onResumeConnector} disabled={busy} data-testid="bridge-control-resume-session">
          {t.bridge.resumeConnector}
        </button>
      )}
      <button type="button" className="bridge-btn bridge-btn--red" onClick={onStop} disabled={busy} data-testid="bridge-control-stop">
        {t.bridge.emergencyStop}
      </button>
      <button type="button" className="bridge-btn bridge-btn--cyan" onClick={onClose} data-testid="bridge-control-back" style={{ gridColumn: 'span 2' }}>
        {t.bridge.back}
      </button>
    </div>
  )

  return (
    <>
      <div
        className="bridge-focus"
        data-testid="bridge-focus"
        style={
          mobile
            ? { position: 'fixed', inset: '64px 8px 190px', left: 8, right: 8, top: 64, width: 'auto', maxWidth: 'none', overflowY: 'auto', zIndex: 70 }
            : undefined
        }
      >
        <StationScreen
          station={station}
          color={color}
          branch={pr?.branch_name || agent?.git_branch || null}
          emptyLabel={t.bridge.noFrames}
          statusLabel={screenStatus(station, state, pr?.pr_number)}
          focus
          testId="bridge-focus-screen"
        />
        <div className="bridge-focus__robot">
          <Robot color={color} size={96} state={robotStateFor(state)} title={seatLabel} />
        </div>
        {mobile && (
          <div className="bridge-panel" style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {details}
            {controls}
          </div>
        )}
      </div>

      {!mobile && (
        <aside className="bridge-side" data-testid="bridge-side-panel" style={{ ['--seat' as any]: color }}>
          {details}
          {controls}
        </aside>
      )}
    </>
  )
}

export default FocusView
