import type { Translations } from '../i18n/types'

/**
 * The manager's seeded greeting is stored once (English) by `backend/app/db/seed.py` with an
 * `i18n_key` marker, so the UI can show it in the operator's language without inventing content.
 * Every other message is real text and is shown verbatim.
 */
export function messageText(message: any, t: Translations): string {
  const raw = String(message?.content || '')
  const key = message?.metadata_json?.i18n_key || message?.metadata?.i18n_key
  if (key === 'managerWelcome') return t.bridge.managerWelcome
  return raw
}
