import { useI18n } from '../i18n/I18nContext'
import { cn } from './ui'

type EmptyStateVariant = 'empty' | 'error' | 'loading'

/**
 * Shared empty / error / loading state used by every data-driven view.
 *
 * - `empty`:  no records exist for the current query (clear Arabic-first copy).
 * - `error`:  the API request failed; shows a retry affordance when provided.
 * - `loading`: the request is still in flight.
 *
 * Always renders RTL-safe text (the app is Arabic-first) and never invents
 * numbers or decorative graphics — only real status copy.
 */
export function EmptyState({
  variant = 'empty',
  title,
  hint,
  retryLabel,
  onRetry,
  className,
  testId = 'empty-state',
}: {
  variant?: EmptyStateVariant
  title?: string
  hint?: string
  retryLabel?: string
  onRetry?: () => void
  className?: string
  testId?: string
}) {
  const { t } = useI18n()

  if (variant === 'loading') {
    return (
      <div
        data-testid={testId}
        data-variant="loading"
        role="status"
        aria-live="polite"
        className={cn(
          'flex flex-col items-center justify-center gap-2 rounded-xl border border-slate-800 bg-slate-900/50 px-6 py-10 text-center text-slate-400',
          className
        )}
      >
        <div className="text-sm">{title ?? t.common.loading}</div>
      </div>
    )
  }

  const isError = variant === 'error'
  return (
    <div
      data-testid={testId}
      data-variant={variant}
      role={isError ? 'alert' : 'status'}
      aria-live={isError ? 'assertive' : 'polite'}
      className={cn(
        'flex flex-col items-center justify-center gap-2 rounded-xl border px-6 py-10 text-center',
        isError
          ? 'border-rose-500/40 bg-rose-500/5 text-rose-200'
          : 'border-slate-800 bg-slate-900/50 text-slate-300',
        className
      )}
    >
      <div className="text-base font-medium">{title ?? (isError ? t.common.errorTitle : t.common.emptyTitle)}</div>
      <div className="max-w-md text-sm text-slate-400">
        {hint ?? (isError ? t.common.errorHint : t.common.emptyHint)}
      </div>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 rounded-lg border border-sky-500 bg-sky-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-sky-500"
        >
          {retryLabel ?? t.common.retry}
        </button>
      ) : null}
    </div>
  )
}
