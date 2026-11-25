import React from 'react'
import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: Array<string | undefined | null | false>) {
  return twMerge(clsx(inputs))
}

export function Card({
  className,
  children,
}: {
  className?: string
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'rounded-xl border border-nexus-border/80 bg-nexus-card p-5 shadow-card',
        className
      )}
    >
      {children}
    </div>
  )
}

export function Badge({
  variant = 'neutral',
  className,
  children,
}: {
  variant?: 'success' | 'warning' | 'danger' | 'info' | 'neutral'
  className?: string
  children: React.ReactNode
}) {
  const styles = {
    success: 'bg-nexus-success/10 text-nexus-success border-nexus-success/35',
    warning: 'bg-nexus-warning/10 text-nexus-warning border-nexus-warning/35',
    danger: 'bg-nexus-error/10 text-nexus-error border-nexus-error/35',
    info: 'bg-nexus-cyan/10 text-nexus-cyan border-nexus-cyan/35',
    neutral: 'bg-nexus-raised text-nexus-muted border-nexus-border',
  }[variant]

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium',
        styles,
        className
      )}
    >
      {children}
    </span>
  )
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  size?: 'sm' | 'md'
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', className, children, ...props },
  ref,
) {
  const vClass = {
    primary: 'bg-nexus-cyan hover:bg-nexus-cyan/85 active:bg-nexus-cyan/75 text-nexus-on-accent border-nexus-cyan shadow-sm shadow-nexus-cyan/20',
    secondary: 'bg-nexus-raised hover:bg-nexus-border active:bg-nexus-raised text-nexus-text border-nexus-border',
    danger: 'bg-nexus-error/90 hover:bg-nexus-error active:bg-nexus-error/80 text-white border-nexus-error',
    ghost: 'bg-transparent hover:bg-nexus-raised text-nexus-muted hover:text-nexus-text border-transparent',
  }[variant]
  const sClass = size === 'sm' ? 'px-2.5 py-1.5 text-xs' : 'px-4 py-2 text-sm'
  return (
    <button
      ref={ref}
      {...props}
      className={cn(
        'inline-flex items-center justify-center gap-1.5 rounded-lg border font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nexus-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-nexus-bg disabled:cursor-not-allowed disabled:opacity-50',
        vClass,
        sClass,
        className
      )}
    >
      {children}
    </button>
  )
})

export function Input({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={cn(
        'w-full rounded-lg border border-nexus-border bg-nexus-bg/70 px-3 py-2 text-sm text-nexus-text placeholder:text-nexus-dim transition-colors focus:border-nexus-cyan focus:outline-none focus:ring-1 focus:ring-nexus-cyan/40',
        className
      )}
    />
  )
}

export function Textarea({
  className,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={cn(
        'w-full rounded-lg border border-nexus-border bg-nexus-bg/70 p-3 text-xs text-nexus-text placeholder:text-nexus-dim transition-colors focus:border-nexus-cyan focus:outline-none focus:ring-1 focus:ring-nexus-cyan/40',
        className
      )}
    />
  )
}

export function Select({
  className,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={cn(
        'rounded-lg border border-nexus-border bg-nexus-bg/70 px-3 py-2 text-sm text-nexus-text transition-colors focus:border-nexus-cyan focus:outline-none focus:ring-1 focus:ring-nexus-cyan/40',
        className
      )}
    >
      {children}
    </select>
  )
}

export function Progress({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, value))
  return (
    <div className={cn('h-2 w-full overflow-hidden rounded-full bg-nexus-raised', className)}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-nexus-cyan to-nexus-violet rtl:bg-gradient-to-l transition-all duration-300"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
