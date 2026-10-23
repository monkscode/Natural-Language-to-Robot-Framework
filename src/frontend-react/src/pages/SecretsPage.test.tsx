/**
 * SecretsPage — org secrets: names listed, values write-only.
 *
 * Runs through the real api() wrapper against a mocked fetch, so the 204 of
 * DELETE and the string `detail` of an error are exercised, not assumed. The
 * marker value must never reach the DOM after a save, and a file over the
 * server's 64 KB cap must be refused here with no request sent.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SecretsPage from './SecretsPage'

const MARKER = 'pw_marker_131'

// This jsdom has no Blob.text(); every real browser does. Stand-in via FileReader.
if (typeof File.prototype.text !== 'function') {
  Object.defineProperty(File.prototype, 'text', {
    value(this: Blob) {
      return new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result))
        reader.onerror = () => reject(reader.error)
        reader.readAsText(this)
      })
    },
  })
}

interface Row { name: string; updated_at: string }
let rows: Row[]
let fetchMock: ReturnType<typeof vi.fn>

function json(status: number, body: unknown) {
  return { ok: status < 400, status, json: async () => body } as Response
}

/** Routes by method + path; `override` can answer one call differently. */
function setup(initial: Row[] = [], override?: (m: string, p: string) => Response | undefined) {
  rows = [...initial]
  fetchMock = vi.fn(async (path: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const custom = override?.(method, path)
    if (custom) return custom
    if (method === 'GET' && path === '/api/secrets') return json(200, rows)
    if (method === 'PUT' && path.startsWith('/api/secrets/')) {
      const name = decodeURIComponent(path.slice('/api/secrets/'.length)).toUpperCase()
      const replaced = rows.some(r => r.name === name)
      if (!replaced) rows.push({ name, updated_at: '2026-10-23T10:00:00Z' })
      return json(200, { name, replaced })
    }
    if (method === 'POST' && path === '/api/secrets/upload') {
      return json(200, { added: ['A', 'B'], replaced: ['C'] })
    }
    if (method === 'DELETE' && path.startsWith('/api/secrets/')) {
      const name = decodeURIComponent(path.slice('/api/secrets/'.length))
      rows = rows.filter(r => r.name !== name)
      return { ok: true, status: 204, json: async () => { throw new Error('no body') } } as unknown as Response
    }
    throw new Error(`unexpected call: ${method} ${path}`)
  })
  vi.stubGlobal('fetch', fetchMock)
}

const calls = (method: string) =>
  fetchMock.mock.calls.filter(([, init]) => (init?.method ?? 'GET') === method)

beforeEach(() => localStorage.clear())
afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })

describe('SecretsPage — list and copy', () => {
  it('lists the names from GET and shows the exact copy', async () => {
    setup([{ name: 'SAUCE_PASSWORD', updated_at: '2026-10-22T09:00:00Z' }])
    render(<SecretsPage />)
    expect(await screen.findByText('SAUCE_PASSWORD')).toBeInTheDocument()
    expect(screen.getByText('Secrets')).toBeInTheDocument()
    expect(screen.getByText(
      'Passwords and keys your tests type. Values are encrypted and never shown again.',
    )).toBeInTheDocument()
    expect(screen.getByText(/Write \{\{NAME\}\} in a test request/)).toBeInTheDocument()
    expect(screen.getByText(/The AI never sees the value; the test reads it when it runs\./)).toBeInTheDocument()
    expect(screen.getByText('Every member of your organization can use, replace and delete these.'))
      .toBeInTheDocument()
    expect(screen.getByText(
      'Upload a .env file — one NAME=value per line. Names you already have are replaced.',
    )).toBeInTheDocument()
  })

  it('says so when there are none', async () => {
    setup([])
    render(<SecretsPage />)
    expect(await screen.findByText('No secrets yet.')).toBeInTheDocument()
  })

  it('shows the server sentence when the list cannot load', async () => {
    setup([], (m) => (m === 'GET'
      ? json(503, { detail: 'Secrets are not set up on this server (SECRETS_ENCRYPTION_KEY)' })
      : undefined))
    render(<SecretsPage />)
    expect(await screen.findByText(
      'Secrets are not set up on this server (SECRETS_ENCRYPTION_KEY)',
    )).toBeInTheDocument()
  })

  it('shows a plain sentence when the request itself fails', async () => {
    setup()
    fetchMock.mockRejectedValue(new Error('network down'))
    render(<SecretsPage />)
    expect(await screen.findByText('Failed to load secrets')).toBeInTheDocument()
  })
})

describe('SecretsPage — add or replace one', () => {
  it('has a password-typed, autocomplete-off value input', async () => {
    setup()
    render(<SecretsPage />)
    const value = await screen.findByLabelText('Value')
    expect(value).toHaveAttribute('type', 'password')
    expect(value).toHaveAttribute('autocomplete', 'new-password')
  })

  it('PUTs {"value"} to /api/secrets/<NAME>, clears the input and never renders the value', async () => {
    setup()
    render(<SecretsPage />)
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'sauce_password' } })
    const value = screen.getByLabelText('Value') as HTMLInputElement
    fireEvent.change(value, { target: { value: MARKER } })
    fireEvent.click(screen.getByRole('button', { name: 'Save secret' }))

    expect(await screen.findByText('SAUCE_PASSWORD')).toBeInTheDocument()
    const [path, init] = calls('PUT')[0]
    expect(path).toBe('/api/secrets/sauce_password')
    expect(JSON.parse(init.body)).toEqual({ value: MARKER })
    expect(value.value).toBe('')
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain(MARKER)
  })

  it('says Replaced when the name already existed', async () => {
    setup([{ name: 'K', updated_at: '2026-10-22T09:00:00Z' }])
    render(<SecretsPage />)
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'k' } })
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: MARKER } })
    fireEvent.click(screen.getByRole('button', { name: 'Save secret' }))
    expect(await screen.findByText('Replaced K.')).toBeInTheDocument()
  })

  it('does not send until a name and a value are both typed', async () => {
    setup()
    render(<SecretsPage />)
    await screen.findByText('No secrets yet.')
    expect(screen.getByRole('button', { name: 'Save secret' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'K' } })
    expect(screen.getByRole('button', { name: 'Save secret' })).toBeDisabled()
    expect(calls('PUT')).toHaveLength(0)
  })

  it('shows the server sentence on a 400 and keeps what was typed', async () => {
    const sentence = 'Names start with a letter and use only letters, digits and underscores (up to 64).'
    setup([], (m) => (m === 'PUT' ? json(400, { detail: sentence }) : undefined))
    render(<SecretsPage />)
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: '1bad' } })
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: MARKER } })
    fireEvent.click(screen.getByRole('button', { name: 'Save secret' }))
    expect(await screen.findByText(sentence)).toBeInTheDocument()
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('1bad')
    expect(document.body.textContent).not.toContain(MARKER)
  })
})

describe('SecretsPage — upload a .env file', () => {
  const fileInput = () => screen.getByLabelText('Upload a .env file') as HTMLInputElement

  it('POSTs {"content": <file text>} and lists what was added and replaced', async () => {
    setup()
    render(<SecretsPage />)
    await screen.findByText('No secrets yet.')
    const text = `A=${MARKER}\nB=x\nC=y\n`
    fireEvent.change(fileInput(), { target: { files: [new File([text], '.env')] } })

    expect(await screen.findByText('Added: A, B. Replaced: C.')).toBeInTheDocument()
    const [path, init] = calls('POST')[0]
    expect(path).toBe('/api/secrets/upload')
    expect(JSON.parse(init.body)).toEqual({ content: text })
    expect(document.body.innerHTML).not.toContain(MARKER)
  })

  it('refuses a 70 KB file in the page with no request sent', async () => {
    setup()
    render(<SecretsPage />)
    await screen.findByText('No secrets yet.')
    const big = new File(['a'.repeat(70 * 1024)], '.env')
    fireEvent.change(fileInput(), { target: { files: [big] } })

    expect(await screen.findByText('That file is over 64 KB. Split it and upload the parts.'))
      .toBeInTheDocument()
    expect(calls('POST')).toHaveLength(0)
  })

  it('shows the server sentence when the file is refused', async () => {
    setup([], (m) => (m === 'POST'
      ? json(400, { detail: 'Line 3 is not NAME=value. Nothing was saved.' })
      : undefined))
    render(<SecretsPage />)
    await screen.findByText('No secrets yet.')
    fireEvent.change(fileInput(), { target: { files: [new File(['x'], '.env')] } })
    expect(await screen.findByText('Line 3 is not NAME=value. Nothing was saved.')).toBeInTheDocument()
  })
})

describe('SecretsPage — delete', () => {
  it('DELETEs the name and it disappears from the list', async () => {
    setup([
      { name: 'KEEP', updated_at: '2026-10-22T09:00:00Z' },
      { name: 'GONE', updated_at: '2026-10-22T09:00:00Z' },
    ])
    render(<SecretsPage />)
    await screen.findByText('GONE')
    fireEvent.click(screen.getByRole('button', { name: 'Delete GONE' }))

    await waitFor(() => expect(screen.queryByText('GONE')).not.toBeInTheDocument())
    expect(calls('DELETE')[0][0]).toBe('/api/secrets/GONE')
    expect(screen.getByText('KEEP')).toBeInTheDocument()
  })

  it('shows the server sentence when delete fails', async () => {
    setup([{ name: 'X', updated_at: '2026-10-22T09:00:00Z' }],
      (m) => (m === 'DELETE' ? json(404, { detail: 'No such secret.' }) : undefined))
    render(<SecretsPage />)
    await screen.findByText('X')
    fireEvent.click(screen.getByRole('button', { name: 'Delete X' }))
    expect(await screen.findByText('No such secret.')).toBeInTheDocument()
  })
})
