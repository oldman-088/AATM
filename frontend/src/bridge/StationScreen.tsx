import React from 'react'
import { highlightLine } from './highlight'
import type { StationCode } from './useBridgeLive'
import { visibleLines } from './useBridgeLive'

type StationScreenProps = {
  station?: StationCode
  color: string
  branch?: string | null
  emptyLabel: string
  statusLabel: string
  focus?: boolean
  testId?: string
  onClick?: () => void
}

function lineKindClass(kind?: string): string {
  if (kind === 'add') return ' bridge-code-line--add'
  if (kind === 'del') return ' bridge-code-line--del'
  if (kind === 'gap') return ' bridge-code-line--gap'
  return ''
}

/** One holo-screen: file header, streamed code with line numbers, footer status and cursor. */
export function StationScreen({
  station,
  color,
  branch,
  emptyLabel,
  statusLabel,
  focus = false,
  testId,
  onClick,
}: StationScreenProps) {
  const lines = visibleLines(station, focus)
  const showCursor = station?.action !== 'review'
  const headerLabel = station?.path || (station?.command ? station.command : '')
  return (
    <div
      className={`bridge-screen${lines.length ? '' : ' bridge-screen--dead'}${focus ? ' bridge-screen--focus' : ''}`}
      style={{ ['--seat' as any]: color, ['--tilt' as any]: '0deg' }}
      data-testid={testId || 'bridge-screen'}
      data-action={station?.action || 'idle'}
      onClick={onClick}
      role={onClick ? 'presentation' : undefined}
    >
      <div className="bridge-screen__head bridge-mono">
        <span className="bridge-screen__file">
          <span className="bridge-dot" aria-hidden="true" />
          {headerLabel || '—'}
        </span>
        <span className="bridge-screen__branch">
          {focus ? 'LIVE · ' : ''}
          {branch || '—'}
        </span>
      </div>
      <div className="bridge-screen__code bridge-mono bridge-code-scroll">
        {lines.length === 0 && <div className="bridge-code-empty">{emptyLabel}</div>}
        {lines.map((line, index) => (
          <div
            key={`${index}-${line.text.slice(0, 24)}`}
            className={`bridge-code-line${lineKindClass(line.kind)}${line.fresh ? ' bridge-code-line--new' : ''}`}
          >
            <span className="bridge-code-line__no">{index + 1}</span>
            <span className="bridge-code-line__text">
              {highlightLine(line.text, station?.language || 'text').map((token, tokenIndex) => (
                <span key={tokenIndex} className={token.cls}>
                  {token.text}
                </span>
              ))}
            </span>
          </div>
        ))}
      </div>
      <div className="bridge-screen__foot bridge-mono">
        <span>{statusLabel}</span>
        {showCursor && <span className="bridge-cursor" aria-hidden="true" style={{ background: color }} />}
      </div>
    </div>
  )
}

export default StationScreen
