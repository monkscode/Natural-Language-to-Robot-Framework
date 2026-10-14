/**
 * Stream a POST Server-Sent-Events response, invoking `onEvent` per parsed
 * `data:` JSON event. Sends the JWT bearer token; on 401 it clears the token
 * and redirects to /login (same contract as lib/api).
 *
 * The backend emits events as `data: {json}\n\n`. We buffer across network
 * chunks so an event split across reads is still parsed correctly.
 */

import { ApiError, clearToken, extractDetail, getToken } from './api'

export async function streamSSE(
  path: string,
  body: unknown,
  onEvent: (data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  const resp = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body), signal })

  if (resp.status === 401) {
    clearToken()
    if (!window.location.pathname.startsWith('/login')) window.location.href = '/login'
    throw new Error('Session expired. Please sign in again.')
  }
  // The server's own words, not the status code. A refused re-run answers
  // "Run not found" or "No stored code for this run — it predates code
  // persistence. Use Regenerate instead."; discarding the body left the
  // caller showing a bare "Request failed (404)" while the real sentence sat
  // unread in the response.
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`
    try {
      detail = extractDetail(await resp.json(), detail)
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail)
  }
  if (!resp.body) {
    throw new ApiError(resp.status, 'The server sent no event stream')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const emit = (block: string) => {
    const line = block.trim()
    if (!line.startsWith('data:')) return
    const json = line.slice(5).trim()
    if (!json) return
    try {
      onEvent(JSON.parse(json))
    } catch {
      /* ignore a malformed event line */
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? '' // keep the trailing (possibly incomplete) block
    for (const part of parts) emit(part)
  }
  if (buffer) emit(buffer)
}
