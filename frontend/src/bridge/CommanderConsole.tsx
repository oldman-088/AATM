import React, { useState } from 'react'
import { useI18n } from '../i18n/I18nContext'
import { messageText } from '../lib/chat'
import { STAGE_IDS, stageName } from './station'

type CommanderConsoleProps = {
  chats: any[]
  steps: any[]
  approvals: any[]
  statusLine: string
  sending: boolean
  error?: string | null
  onSubmitDirective: (content: string) => Promise<void> | void
  onDecideApproval: (approvalId: string, decision: 'approved' | 'rejected') => Promise<void> | void
  onEmergencyStop: () => Promise<void> | void
  onClearChat?: () => Promise<void> | void
  onNewConversation?: () => Promise<void> | void
  anyEmergencyStopped: boolean
  banner?: React.ReactNode
  mobile?: boolean
}

const STAGE_INDEX = STAGE_IDS.length

/**
 * The commander console fixed at the bottom of the bridge (spec §2.6): a real chat with the
 * manager, 8 compact numbered mission-stage cells (state colour + tooltip), and the real approval /
 * emergency controls. Nothing here is simulated — the send button posts a real directive.
 */
export function CommanderConsole({
  chats,
  steps,
  approvals,
  statusLine,
  sending,
  error,
  onSubmitDirective,
  onDecideApproval,
  onEmergencyStop,
  onClearChat,
  onNewConversation,
  anyEmergencyStopped,
  banner,
  mobile = false,
}: CommanderConsoleProps) {
  const { t } = useI18n()
  const [directive, setDirective] = useState('')
  const [approvalsOpen, setApprovalsOpen] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const content = directive.trim()
    if (!content || sending) return
    setDirective('')
    await onSubmitDirective(content)
  }

  const cells = Array.from({ length: STAGE_INDEX }, (_, index) => ({
    id: steps?.[index]?.id || STAGE_IDS[index],
    state: steps?.[index]?.state || 'pending',
  }))

  return (
    <div className={`bridge-console${mobile ? ' bridge-console--mobile' : ''}`} data-testid="bridge-console">
      <div className="bridge-console__chat">
        {banner}
        <div className="bridge-console__messages" role="log" aria-live="polite" data-testid="bridge-chat">
          {chats.length === 0 && <div style={{ color: 'var(--dim)' }}>{t.bridge.chatEmpty}</div>}
          {chats.slice(-40).map((message) => {
            const isUser = message?.sender_type === 'user'
            return (
              <div
                key={message?.id || `${message?.created_at}-${String(message?.content || '').slice(0, 8)}`}
                className={`bridge-msg bridge-msg--${isUser ? 'user' : 'manager'}`}
              >
                <span className="bridge-msg__who">{isUser ? `${t.bridge.you}:` : `${t.bridge.manager}:`}</span>{' '}
                <span>{messageText(message, t).slice(0, 600)}</span>
              </div>
            )
          })}
        </div>
        {error && <div style={{ fontSize: 11, color: 'var(--danger-text)' }}>{error}</div>}
        <form className="bridge-console__form" onSubmit={submit}>
          <input
            className="bridge-input"
            value={directive}
            onChange={(event) => setDirective(event.target.value)}
            placeholder={t.bridge.directivePlaceholder}
            aria-label={t.bridge.directivePlaceholder}
            data-testid="bridge-directive-input"
          />
          <button className="bridge-send" type="submit" disabled={sending || !directive.trim()} data-testid="bridge-send">
            {sending ? t.bridge.sending : t.bridge.send}
          </button>
          {onNewConversation && (
            <button type="button" className="bridge-btn" onClick={() => onNewConversation()} disabled={sending} data-testid="manager-new-conversation">
              {t.bridge.newConversation}
            </button>
          )}
          {onClearChat && (
            <button type="button" className="bridge-btn bridge-btn--danger" onClick={() => onClearChat()} disabled={sending} data-testid="manager-clear-chat">
              {t.bridge.clearChat}
            </button>
          )}
        </form>
      </div>

      <div className="bridge-console__side">
        <div className="bridge-console__label">
          <span>{t.bridge.stages}</span>
          <span className="bridge-mono" style={{ color: 'var(--dim)' }}>
            {`${steps.filter((step: any) => step?.state === 'done').length}/${STAGE_INDEX}`}
          </span>
        </div>
        <div className="bridge-stages" data-testid="console-stages">
          {cells.map((cell, index) => (
            <div
              key={cell.id}
              className={`bridge-stage-cell bridge-stage-cell--${cell.state}`}
              data-state={cell.state}
              title={stageName(cell.id, t)}
              aria-label={`${index + 1}. ${stageName(cell.id, t)} — ${cell.state}`}
              data-testid={`bridge-stage-cell-${cell.id}`}
            >
              <span className="bridge-mono">{index + 1}</span>
            </div>
          ))}
        </div>
        <div className="bridge-console__status" data-testid="bridge-status-line">
          {statusLine}
        </div>
        <div className="bridge-console__buttons">
          <button
            type="button"
            className="bridge-btn bridge-btn--green"
            onClick={() => setApprovalsOpen((open) => !open)}
            aria-expanded={approvalsOpen}
            data-testid="bridge-approvals-button"
          >
            {t.bridge.approvals} ({approvals.length})
          </button>
          <button type="button" className="bridge-btn bridge-btn--red" onClick={() => void onEmergencyStop()} data-testid="bridge-emergency-stop">
            {anyEmergencyStopped ? t.bridge.resumeSystem : t.bridge.emergencyStop}
          </button>
        </div>
        {approvalsOpen && (
          <div className="bridge-approvals" data-testid="bridge-approvals-panel">
            <div style={{ fontSize: 12, color: '#a7f3d0', marginBottom: 6 }}>{t.bridge.approvals}</div>
            {approvals.length === 0 && <div style={{ fontSize: 12, color: 'var(--dim)' }}>{t.bridge.approvalsEmpty}</div>}
            {approvals.map((approval) => (
              <div key={approval.id} className="bridge-approval-row">
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  <span className="bridge-mono" style={{ color: 'var(--muted)' }}>{approval.action_type}</span> · {approval.title}
                </span>
                <span style={{ display: 'flex', gap: 6, flex: '0 0 auto' }}>
                  <button
                    type="button"
                    className="bridge-btn bridge-btn--green"
                    style={{ height: 26, flex: '0 0 auto', padding: '0 10px' }}
                    onClick={() => void onDecideApproval(approval.id, 'approved')}
                  >
                    {t.bridge.approve}
                  </button>
                  <button
                    type="button"
                    className="bridge-btn bridge-btn--red"
                    style={{ height: 26, flex: '0 0 auto', padding: '0 10px' }}
                    onClick={() => void onDecideApproval(approval.id, 'rejected')}
                  >
                    {t.bridge.reject}
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default CommanderConsole
