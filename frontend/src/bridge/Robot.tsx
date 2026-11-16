import React from 'react'

export type RobotState = 'idle' | 'writing' | 'thinking' | 'needs_human' | 'done'

type RobotProps = {
  color: string
  size?: number
  state?: RobotState
  /** Manager variant: violet frame and a fully lit visor. */
  manager?: boolean
  title?: string
}

/**
 * The employee robot (original inline SVG from the Command Bridge specification §2.4).
 * Colour comes from the employee's seat colour; the manager keeps the violet frame.
 */
export function Robot({ color, size = 120, state = 'idle', manager = false, title }: RobotProps) {
  const stroke = manager ? '#A78BFA' : '#475569'
  const visorColor = manager ? '#F0ABFC' : color
  const visorOpacity = manager ? 1 : 0.45
  const classes = ['bridge-robot', `bridge-robot--${state}`].join(' ')
  return (
    <svg
      className={classes}
      data-testid="bridge-robot"
      data-state={state}
      width={size}
      height={(size * 118) / 120}
      viewBox="0 0 120 118"
      role="img"
      aria-label={title || 'robot'}
    >
      <line x1="60" y1="2" x2="60" y2="16" stroke={manager ? '#A78BFA' : '#94A3B8'} strokeWidth="2" />
      <circle className="bridge-robot__antenna" cx="60" cy="4" r="4" fill={manager ? '#F0ABFC' : color} />
      <rect x="34" y="16" width="52" height="40" rx="14" fill="#1E293B" stroke={stroke} strokeWidth="1.5" />
      <rect
        className="bridge-robot__visor"
        x="40"
        y="27"
        width="40"
        height="7"
        rx="3.5"
        fill={visorColor}
        opacity={visorOpacity}
      />
      <rect x="54" y="56" width="12" height="8" fill="#334155" />
      <path d="M14 118 C 14 78, 30 64, 60 64 C 90 64, 106 78, 106 118 Z" fill="#111827" stroke={stroke} strokeWidth="1.5" />
      <rect className="bridge-robot__chest-1" x="46" y="78" width="28" height="4" rx="2" fill={color} />
      <rect className="bridge-robot__chest-2" x="46" y="88" width="28" height="4" rx="2" fill={color} opacity="0.6" />
      <rect className="bridge-robot__chest-3" x="46" y="98" width="28" height="4" rx="2" fill={color} opacity="0.3" />
      <circle cx="22" cy="86" r="9" fill="#1F2937" stroke={stroke} />
      <circle cx="98" cy="86" r="9" fill="#1F2937" stroke={stroke} />
    </svg>
  )
}

export default Robot
