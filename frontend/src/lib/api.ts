const TOKEN_KEY = 'aatm_jwt_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

/**
 * A failed API call with its HTTP status and FastAPI's `detail` payload.
 *
 * FastAPI answers with `{"detail": "text"}` or `{"detail": {"code", "message"}}`; both are
 * flattened into `message` so a screen can show a sentence instead of `[object Object]`, and
 * `status` lets it tell 403 (needs the admin role) from 409 (the record changed) and
 * 422 (the form is wrong) — the E28 controls rely on that.
 */
export class ApiError extends Error {
  status: number
  code: string | null
  detail: any

  constructor(message: string, status: number, detail: any = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = detail && typeof detail === 'object' && typeof detail.code === 'string' ? detail.code : null
  }
}

/** Turn a FastAPI error body into one readable sentence (never `[object Object]`). */
export function apiErrorMessage(payload: any, fallback: string): string {
  const detail = payload?.detail ?? payload?.error?.message ?? payload
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length) {
    // FastAPI/pydantic validation errors: [{"loc": [...], "msg": "..."}]
    return detail.map((item: any) => (typeof item === 'string' ? item : item?.msg)).filter(Boolean).join('; ') || fallback
  }
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
    if (typeof detail.error === 'string' && detail.error.trim()) return detail.error
  }
  return fallback
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }

  const res = await fetch(path.startsWith('/api') ? path : `/api/v1${path}`, {
    ...init,
    headers,
  })
  if (!res.ok) {
    let payload: any = null
    try {
      payload = await res.json()
    } catch {
      // ignore json parse error
    }
    throw new ApiError(apiErrorMessage(payload, `HTTP ${res.status}`), res.status, payload?.detail ?? payload)
  }
  return res.json() as Promise<T>
}
