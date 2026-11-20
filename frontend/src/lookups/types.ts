/**
 * Typed mirror of the phone-number lookup API (`backend/app/schemas/lookups.py`).
 *
 * These shapes are the contract for `POST /api/v1/lookups` and
 * `GET /api/v1/lookups/{job_id}`; nothing here is invented — every optional
 * field is optional in the backend schema too.
 */

export type LookupJobStatus = 'queued' | 'running' | 'completed' | 'failed'
export type ProviderResultStatus = 'pending' | 'success' | 'error' | 'timeout'

export interface LookupCreateResponse {
  job_id: string
  status: LookupJobStatus
  phone_e164: string
  phone_raw: string
  created_at: string
}

export interface ProviderResult {
  provider: string
  status: ProviderResultStatus
  http_status: number | null
  error_message: string | null
  latency_ms: number | null
  payload: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface LookupAggregate {
  display_name: string | null
  country: string | null
  carrier: string | null
  line_type: string | null
  spam_score: number | null
  is_spam: boolean
  summary: string
  attributes: Record<string, unknown>
  updated_at: string
}

export interface LookupJobDetail {
  job_id: string
  status: LookupJobStatus
  phone_e164: string
  phone_raw: string
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  error_message: string | null
  result: LookupAggregate | null
  provider_results: ProviderResult[]
}

/**
 * The documented payload contract a provider worker writes into
 * `provider_results[].payload`. Unknown keys are preserved and shown in the
 * per-card attribute list, so no provider data is lost by the UI.
 */
export interface ProviderPayload {
  /** Social platform name; defaults to the provider name. */
  platform?: string
  /** Explicit negative answer: the platform was searched and had no match. */
  not_found?: boolean
  /** Positive answer; when omitted the provider status decides. */
  found?: boolean
  display_name?: string
  name?: string
  avatar_url?: string
  profile_url?: string
  /** Provider-reported confidence, either 0..1 or 0..100. */
  confidence?: number
  carrier?: string
  country?: string
  [key: string]: unknown
}
