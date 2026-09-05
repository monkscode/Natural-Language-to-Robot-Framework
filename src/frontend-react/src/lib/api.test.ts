/**
 * lib/api — the fetch wrapper every authenticated call in the SPA goes
 * through, plus isAccessLoss, which decides whether a failed request means a
 * row is gone for good.
 *
 * isAccessLoss: the Test Runs page drops a row from its loaded list on the
 * strength of this answer, so a false positive DELETES something the user
 * can still open. Every status below is one the re-run endpoint can actually
 * return.
 *
 * api(): a 401 must clear the stored token and redirect to /login BEFORE the
 * caller ever sees a rejected promise - a caller racing that redirect would
 * otherwise flash its own error UI for a heartbeat before the browser
 * navigates away. That clear-and-redirect is gated on the same `auth` flag
 * that decides whether the bearer header is attached at all, so a request
 * made with `auth: false` (login, register) gets neither - its own 401 is a
 * refused login, not a session that needs clearing.
 *
 * extractDetail(): FastAPI answers errors in three different shapes across
 * its own routes (a plain string `detail`, a 422 validation `detail` array,
 * or a bare `message`), and every one of them must turn into one human
 * sentence instead of "Request failed (422)".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, clearToken, extractDetail, getToken, isAccessLoss, setToken } from './api'

function stubFetch(impl: () => Promise<unknown>) {
  const fn = vi.fn().mockImplementation(impl)
  vi.stubGlobal('fetch', fn)
  return fn
}

function stubLocation(pathname: string, href: string) {
  const original = window.location
  Object.defineProperty(window, 'location', { writable: true, value: { pathname, href } })
  return () => Object.defineProperty(window, 'location', { writable: true, value: original })
}

/** Resolves to the rejection reason, or fails the test if the promise resolved. */
async function reasonOf(p: Promise<unknown>): Promise<unknown> {
  return p.then(
    () => { throw new Error('expected api(...) to reject, but it resolved') },
    (err: unknown) => err,
  )
}

afterEach(() => {
  clearToken()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

describe('isAccessLoss', () => {
  it('is true when the resource is gone or was never ours', () => {
    expect(isAccessLoss(new ApiError(404, 'Run not found'))).toBe(true)
    // Not reachable on the re-run route today — it answers 404 so it cannot
    // leak existence — but the rule must already be right if that changes.
    expect(isAccessLoss(new ApiError(403, 'Forbidden'))).toBe(true)
  })

  it('is FALSE for a 409, which says the run has no stored code', () => {
    // The endpoint reaches its 409 only after the access check passed, so
    // the caller can still open the row and Regenerate from it. Treating
    // this as access loss was a real defect: the row-level Run again button
    // fires blind on exactly these runs, because the history payload
    // carries no robot_code for the row to check.
    const e = new ApiError(
      409,
      'No stored code for this run — it predates code persistence. Use Regenerate instead.',
    )
    expect(isAccessLoss(e)).toBe(false)
  })

  it('is false for client mistakes, throttling and server faults', () => {
    for (const status of [400, 422, 429, 500, 502, 503]) {
      expect(isAccessLoss(new ApiError(status, 'x'))).toBe(false)
    }
  })

  it('is false for anything that is not an ApiError', () => {
    // A dropped connection arrives as a bare TypeError, and lib/sse throws a
    // plain Error on 401 before redirecting to /login. Neither says the run
    // is gone, and an unknown shape must never evict.
    expect(isAccessLoss(new TypeError('Failed to fetch'))).toBe(false)
    expect(isAccessLoss(new Error('Session expired. Please sign in again.'))).toBe(false)
    expect(isAccessLoss({ status: 404 })).toBe(false)
    expect(isAccessLoss(undefined)).toBe(false)
    expect(isAccessLoss(null)).toBe(false)
  })
})

describe('api — the happy path', () => {
  it('returns the parsed JSON body on success', async () => {
    stubFetch(async () => ({ status: 200, ok: true, json: async () => ({ total: 3, rows: ['a', 'b', 'c'] }) }))

    const result = await api<{ total: number; rows: string[] }>('/runs')

    expect(result).toEqual({ total: 3, rows: ['a', 'b', 'c'] })
  })

  it('returns undefined on 204 without ever trying to parse a body', async () => {
    // If the 204 short-circuit were ever dropped, this would call .json() on
    // a body FastAPI never sent for a 204 - the spy below is what proves the
    // early return actually fires, not just that the result happens to be
    // undefined.
    const jsonSpy = vi.fn().mockResolvedValue({ should: 'never be read' })
    stubFetch(async () => ({ status: 204, ok: true, json: jsonSpy }))

    const result = await api('/runs/1')

    expect(result).toBeUndefined()
    expect(jsonSpy).not.toHaveBeenCalled()
  })

  it('passes method and body straight through to fetch', async () => {
    const fetchMock = stubFetch(async () => ({ status: 200, ok: true, json: async () => ({}) }))

    await api('/runs/1/rerun', { method: 'POST', body: JSON.stringify({ force: true }) })

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/runs/1/rerun')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ force: true }))
  })
})

describe('api — the bearer token', () => {
  it('attaches it when auth defaults true and a token exists', async () => {
    setToken('secret-token')
    const fetchMock = stubFetch(async () => ({ status: 200, ok: true, json: async () => ({}) }))

    await api('/runs')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer secret-token')
  })

  it('omits it when there is no token, even though auth is true', async () => {
    const fetchMock = stubFetch(async () => ({ status: 200, ok: true, json: async () => ({}) }))

    await api('/runs')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect('Authorization' in (init.headers as Record<string, string>)).toBe(false)
  })

  it('omits it when auth is false, even though a token exists', async () => {
    // The login/register contract: those calls must not present whatever
    // token is left over from a previous session.
    setToken('secret-token')
    const fetchMock = stubFetch(async () => ({ status: 200, ok: true, json: async () => ({}) }))

    await api('/auth/login', { auth: false })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect('Authorization' in (init.headers as Record<string, string>)).toBe(false)
  })

  it('still sends the JSON content type when auth is false', async () => {
    const fetchMock = stubFetch(async () => ({ status: 200, ok: true, json: async () => ({}) }))

    await api('/auth/login', { auth: false })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })
})

describe('api — the 401 contract', () => {
  it('clears the token and redirects to /login before the promise rejects', async () => {
    setToken('secret-token')
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({}) }))
    const restore = stubLocation('/generate', '')

    const err = await reasonOf(api('/runs'))

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(401)
    expect((err as Error).message).toBe('Session expired. Please sign in again.')
    expect(getToken()).toBeNull()
    expect((window.location as unknown as { href: string }).href).toBe('/login')
    restore()
  })

  it('does not redirect again when already on the login page', async () => {
    setToken('secret-token')
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({}) }))
    const restore = stubLocation('/login', 'unchanged')

    await reasonOf(api('/runs'))

    expect((window.location as unknown as { href: string }).href).toBe('unchanged')
    restore()
  })

  it('does NOT clear the token or redirect on a 401 when auth is false', async () => {
    // Surprising at first read: `resp.status === 401 && auth` gates the
    // special-case entirely on the same flag that gates the header. A failed
    // login legitimately answers 401 (bad credentials) - that is not a
    // session expiring, and must not evict whatever token another tab is
    // using. It falls through to the plain error branch below instead.
    setToken('someone-elses-still-valid-token')
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({ detail: 'Incorrect email or password' }) }))
    const restore = stubLocation('/login', 'unchanged')

    const err = await reasonOf(api('/auth/login', { auth: false }))

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(401)
    expect((err as Error).message).toBe('Incorrect email or password')
    expect(getToken()).toBe('someone-elses-still-valid-token')
    expect((window.location as unknown as { href: string }).href).toBe('unchanged')
    restore()
  })
})

describe('api — error responses', () => {
  it('throws an ApiError built from the response’s own detail message', async () => {
    stubFetch(async () => ({ status: 404, ok: false, json: async () => ({ detail: 'Run not found' }) }))

    const err = await reasonOf(api('/runs/missing'))

    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect((err as Error).message).toBe('Run not found')
  })

  it('falls back to a generic message when the error body is not JSON', async () => {
    stubFetch(async () => ({ status: 500, ok: false, json: async () => { throw new Error('not json') } }))

    const err = await reasonOf(api('/runs'))

    expect((err as ApiError).status).toBe(500)
    expect((err as Error).message).toBe('Request failed (500)')
  })
})

describe('extractDetail — normalizing FastAPI’s error body shapes', () => {
  it('returns a plain string detail as-is', () => {
    expect(extractDetail({ detail: 'Run not found' }, 'fallback')).toBe('Run not found')
  })

  it('pulls msg out of a 422 validation array', () => {
    const body = { detail: [{ loc: ['body', 'query'], msg: 'field required', type: 'missing' }] }
    expect(extractDetail(body, 'fallback')).toBe('field required')
  })

  it('falls back to {message} when there is no detail at all', () => {
    expect(extractDetail({ message: 'Incorrect email or password' }, 'fallback')).toBe('Incorrect email or password')
  })

  it('prefers a string detail over message when both are present', () => {
    expect(extractDetail({ detail: 'from detail', message: 'from message' }, 'fallback')).toBe('from detail')
  })

  it('falls through to message when the detail array has no usable msg', () => {
    const body = { detail: [{ loc: ['body'] }], message: 'from message' }
    expect(extractDetail(body, 'fallback')).toBe('from message')
  })

  it('returns the fallback for shapes with nothing usable', () => {
    expect(extractDetail({}, 'fallback')).toBe('fallback')
    expect(extractDetail({ detail: [] }, 'fallback')).toBe('fallback')
    expect(extractDetail({ detail: 42 }, 'fallback')).toBe('fallback')
    expect(extractDetail('a bare string body', 'fallback')).toBe('fallback')
    expect(extractDetail(null, 'fallback')).toBe('fallback')
    expect(extractDetail(undefined, 'fallback')).toBe('fallback')
  })
})
