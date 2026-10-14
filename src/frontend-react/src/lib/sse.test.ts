/**
 * streamSSE — the POST+SSE reader behind GeneratePage's generate/execute flow.
 *
 * Everything here is invisible to a type check and silent when it breaks: a
 * dropped Authorization header still compiles, a 401 that forgets to clear
 * the token still compiles, and a chunk-boundary bug in the buffer only shows
 * up as an event that quietly never arrives — no throw, just a step the UI
 * never draws. This file pins:
 *   - the request shape (method, headers, the bearer token gated on its own
 *     existence, the abort signal forwarded),
 *   - the same 401-clears-and-redirects-before-throwing contract lib/api.ts
 *     implements (this module's own docstring says "same contract as
 *     lib/api"), including the already-on-/login case that must NOT redirect
 *     again,
 *   - non-401 failures turned into an ApiError carrying the server's own
 *     detail message, with a plain-status fallback when the error body isn't
 *     JSON,
 *   - the ok-but-bodyless response, which would otherwise hang forever on
 *     `resp.body.getReader()`,
 *   - and the actual SSE parsing: buffering a JSON event split across two
 *     network reads, flushing a final event with no trailing blank line,
 *     ignoring non-"data:" blocks and empty "data:" lines, and swallowing one
 *     malformed event without failing every event after it.
 *
 * ./api is mocked wholesale: getToken/clearToken/extractDetail are the only
 * things streamSSE asks of it, and ApiError is a real minimal stand-in (not
 * the actual export) so the `instanceof` checks below see the exact class
 * streamSSE itself throws.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
      this.name = 'ApiError'
    }
  }
  return {
    ApiError,
    getToken: vi.fn(),
    clearToken: vi.fn(),
    extractDetail: vi.fn(),
  }
})

import { ApiError, clearToken, extractDetail, getToken } from './api'
import { streamSSE } from './sse'

const mockGetToken = vi.mocked(getToken)
const mockClearToken = vi.mocked(clearToken)
const mockExtractDetail = vi.mocked(extractDetail)

function stubFetch(impl: () => Promise<unknown>) {
  const fn = vi.fn().mockImplementation(impl)
  vi.stubGlobal('fetch', fn)
  return fn
}

/** Feeds `chunks` out of resp.body.getReader().read(), one per call, then signals done. */
function bodyOf(chunks: string[]) {
  const encoder = new TextEncoder()
  let i = 0
  return {
    getReader: () => ({
      read: vi.fn().mockImplementation(async () => {
        if (i < chunks.length) {
          const value = encoder.encode(chunks[i])
          i += 1
          return { done: false, value }
        }
        return { done: true, value: undefined }
      }),
    }),
  }
}

function bodyThatRejects(err: unknown) {
  return { getReader: () => ({ read: vi.fn().mockRejectedValue(err) }) }
}

function okResponse(chunks: string[], status = 200) {
  return { status, ok: true, body: bodyOf(chunks) }
}

function stubLocation(pathname: string, href: string) {
  const original = window.location
  Object.defineProperty(window, 'location', { writable: true, value: { pathname, href } })
  return () => Object.defineProperty(window, 'location', { writable: true, value: original })
}

/** Resolves to the rejection reason, or fails the test if the promise resolved. */
async function reasonOf(p: Promise<unknown>): Promise<unknown> {
  return p.then(
    () => { throw new Error('expected streamSSE(...) to reject, but it resolved') },
    (err: unknown) => err,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

describe('streamSSE — the request it sends', () => {
  it('posts JSON with an SSE accept header', async () => {
    const fetchMock = stubFetch(async () => okResponse([]))
    mockGetToken.mockReturnValue(null)

    await streamSSE('/generate-and-run', { query: 'open flipkart' }, vi.fn())

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(path).toBe('/generate-and-run')
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ query: 'open flipkart' }))
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
    expect((init.headers as Record<string, string>).Accept).toBe('text/event-stream')
  })

  it('attaches the bearer token when one exists', async () => {
    const fetchMock = stubFetch(async () => okResponse([]))
    mockGetToken.mockReturnValue('tok-123')

    await streamSSE('/x', {}, vi.fn())

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok-123')
  })

  it('sends no Authorization header at all when there is no token', async () => {
    // Not "empty string" - genuinely absent, so a caller can't tell a missing
    // token from an empty one by inspecting the header.
    const fetchMock = stubFetch(async () => okResponse([]))
    mockGetToken.mockReturnValue(null)

    await streamSSE('/x', {}, vi.fn())

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect('Authorization' in (init.headers as Record<string, string>)).toBe(false)
  })

  it('forwards the caller’s AbortSignal to fetch', async () => {
    const fetchMock = stubFetch(async () => okResponse([]))
    mockGetToken.mockReturnValue(null)
    const controller = new AbortController()

    await streamSSE('/x', {}, vi.fn(), controller.signal)

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.signal).toBe(controller.signal)
  })
})

describe('streamSSE — the 401 contract (same as lib/api)', () => {
  it('clears the token and redirects to /login before rejecting', async () => {
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({}) }))
    mockGetToken.mockReturnValue('tok')
    const restore = stubLocation('/generate', '')

    const err = await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect((err as Error).message).toBe('Session expired. Please sign in again.')
    expect(mockClearToken).toHaveBeenCalledTimes(1)
    expect((window.location as unknown as { href: string }).href).toBe('/login')
    restore()
  })

  it('still clears the token but does not redirect again when already on /login', async () => {
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({}) }))
    mockGetToken.mockReturnValue('tok')
    const restore = stubLocation('/login', 'unchanged')

    await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect(mockClearToken).toHaveBeenCalledTimes(1)
    expect((window.location as unknown as { href: string }).href).toBe('unchanged')
    restore()
  })

  it('never calls onEvent on a 401', async () => {
    stubFetch(async () => ({ status: 401, ok: false, json: async () => ({}) }))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()
    const restore = stubLocation('/x', '')

    await reasonOf(streamSSE('/x', {}, onEvent))

    expect(onEvent).not.toHaveBeenCalled()
    restore()
  })
})

describe('streamSSE — non-401 error responses', () => {
  it('throws an ApiError built from the server’s own detail message', async () => {
    const body = { detail: 'Run not found' }
    stubFetch(async () => ({ status: 404, ok: false, json: async () => body }))
    mockGetToken.mockReturnValue(null)
    mockExtractDetail.mockReturnValue('Run not found')

    const err = await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect(err).toBeInstanceOf(ApiError)
    expect((err as InstanceType<typeof ApiError>).status).toBe(404)
    expect((err as Error).message).toBe('Run not found')
    expect(mockExtractDetail).toHaveBeenCalledWith(body, 'Request failed (404)')
  })

  it('falls back to a generic message when the error body is not JSON', async () => {
    stubFetch(async () => ({
      status: 500, ok: false, json: async () => { throw new Error('not json') },
    }))
    mockGetToken.mockReturnValue(null)

    const err = await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect((err as InstanceType<typeof ApiError>).status).toBe(500)
    expect((err as Error).message).toBe('Request failed (500)')
    // The catch is around `await resp.json()` itself - a body that fails to
    // parse must never reach extractDetail at all.
    expect(mockExtractDetail).not.toHaveBeenCalled()
  })
})

describe('streamSSE — a response with no readable body', () => {
  it('throws instead of hanging when resp.body is missing on an ok response', async () => {
    stubFetch(async () => ({ status: 200, ok: true, body: null }))
    mockGetToken.mockReturnValue(null)

    const err = await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect((err as InstanceType<typeof ApiError>).status).toBe(200)
    expect((err as Error).message).toBe('The server sent no event stream')
  })
})

describe('streamSSE — parsing the event stream', () => {
  it('parses a single data: event and hands the parsed JSON to onEvent', async () => {
    stubFetch(async () => okResponse(['data: {"step":"start"}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ step: 'start' })
  })

  it('parses multiple events delivered in the same chunk, in order', async () => {
    stubFetch(async () => okResponse(['data: {"i":1}\n\ndata: {"i":2}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent.mock.calls).toEqual([[{ i: 1 }], [{ i: 2 }]])
  })

  it('reassembles one event whose JSON is split across two network reads', async () => {
    // The exact case the module docstring calls out. Without the buffer
    // carrying `{"a"` forward, that fragment has no blank-line terminator on
    // its own read and would be dropped rather than joined with `:1}`.
    stubFetch(async () => okResponse(['data: {"a"', ':1}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ a: 1 })
  })

  it('still emits the last event when the stream ends with no trailing blank line', async () => {
    stubFetch(async () => okResponse(['data: {"done":true}']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ done: true })
  })

  it('ignores a non-data block (a comment/keep-alive line) without losing the event after it', async () => {
    stubFetch(async () => okResponse([': keep-alive\n\ndata: {"x":1}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ x: 1 })
  })

  it('ignores an empty "data:" line', async () => {
    stubFetch(async () => okResponse(['data:\n\ndata: {"x":1}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await streamSSE('/x', {}, onEvent)

    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ x: 1 })
  })

  it('swallows one malformed JSON event without failing the whole stream', async () => {
    stubFetch(async () => okResponse(['data: {not valid json\n\ndata: {"ok":true}\n\n']))
    mockGetToken.mockReturnValue(null)
    const onEvent = vi.fn()

    await expect(streamSSE('/x', {}, onEvent)).resolves.toBeUndefined()
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent).toHaveBeenCalledWith({ ok: true })
  })

  it('propagates a read() rejection instead of hanging forever', async () => {
    stubFetch(async () => ({ status: 200, ok: true, body: bodyThatRejects(new Error('connection reset')) }))
    mockGetToken.mockReturnValue(null)

    const err = await reasonOf(streamSSE('/x', {}, vi.fn()))

    expect((err as Error).message).toBe('connection reset')
  })
})
