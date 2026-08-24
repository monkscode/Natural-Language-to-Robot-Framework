/**
 * Tiny fetch wrapper for the FastAPI backend.
 *
 * - Attaches the JWT bearer token (from localStorage) when `auth` is true.
 * - On 401 it clears the token and redirects to /login (session expired).
 * - Normalizes FastAPI error bodies (string detail, 422 validation arrays,
 *   or {message}) into a thrown ApiError with a human-readable message.
 *
 * Paths are same-origin and relative (e.g. "/auth/login"): the Vite dev server
 * and the nginx container both proxy /api,/auth,/reports,/health to FastAPI.
 */

const TOKEN_KEY = 'mark1_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

/**
 * Did this failure mean the caller LOST ACCESS to the thing they acted on —
 * as opposed to the request merely failing?
 *
 * Callers use it to decide whether a row may be dropped from a list. 404 is
 * the real case: the resource is gone, or was never theirs. 403 is included
 * so the rule is already right if a route ever adopts it; several of ours
 * deliberately answer 404 instead, so as not to leak existence.
 *
 * Everything else is false, and the distinctions matter:
 *   - 409 on a re-run means the run predates code persistence. The endpoint
 *     reaches that branch only AFTER the access check passed, so the caller
 *     can still open the row and Regenerate from it.
 *   - 400 / 422 are client-side mistakes; 429 is transient; 5xx is ours.
 *     None of them says the resource is gone.
 *   - 401 never arrives here — lib/api and lib/sse redirect to /login.
 *
 * An ALLOWLIST on purpose. An unknown status must default to KEEPING the
 * row: a phantom row is recoverable with a refresh, a wrongly deleted one
 * is not.
 */
export function isAccessLoss(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404)
}

interface ApiOptions extends RequestInit {
  /** Attach the bearer token. Default true; set false for login/register. */
  auth?: boolean
}

/**
 * Pull the human-readable message out of a FastAPI error body.
 *
 * Exported for lib/sse.ts, which reads its own error responses and would
 * otherwise show a bare status code where the server sent a sentence.
 */
export function extractDetail(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    const d = data as Record<string, unknown>
    if (typeof d.detail === 'string') return d.detail
    if (Array.isArray(d.detail) && d.detail[0] && typeof d.detail[0] === 'object') {
      const msg = (d.detail[0] as Record<string, unknown>).msg
      if (typeof msg === 'string') return msg
    }
    if (typeof d.message === 'string') return d.message
  }
  return fallback
}

export async function api<T = unknown>(path: string, options: ApiOptions = {}): Promise<T> {
  const { auth = true, headers, ...rest } = options
  const finalHeaders: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(headers as Record<string, string> | undefined),
  }
  const token = getToken()
  if (auth && token) finalHeaders.Authorization = `Bearer ${token}`

  const resp = await fetch(path, { ...rest, headers: finalHeaders })

  if (resp.status === 401 && auth) {
    clearToken()
    if (!window.location.pathname.startsWith('/login')) {
      window.location.href = '/login'
    }
    throw new ApiError(401, 'Session expired. Please sign in again.')
  }

  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`
    try {
      detail = extractDetail(await resp.json(), detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail)
  }

  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}
