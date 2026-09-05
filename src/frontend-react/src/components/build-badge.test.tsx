/**
 * BuildBadge — the commit stamp in the sidebar footer.
 *
 * Its whole reason to exist is that `-develop` and `-latest` are moving tags,
 * so the image tag a container was pulled under says nothing about the code
 * inside it. That makes two of its properties load-bearing rather than
 * cosmetic: it must show the FULL commit on copy (the 7-char label is only a
 * label), and it must fail silently — a diagnostic string that can throw, or
 * sign a user out, is worse than no diagnostic string.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BuildBadge } from './build-badge'

const FULL = '9b9836a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7'

function stubFetch(impl: () => Promise<unknown>) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation(impl))
}

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText }, configurable: true, writable: true,
  })
  return writeText
}

beforeEach(() => vi.useRealTimers())
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('BuildBadge', () => {
  it('shows git’s own 7-character short form of the commit', async () => {
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: FULL } }) }))

    render(<BuildBadge />)

    expect(await screen.findByText('build 9b9836a')).toBeInTheDocument()
  })

  it('copies the FULL commit, not the abbreviation it displays', async () => {
    // The abbreviation is a label. Pasting 7 characters into a bug report
    // loses the thing the badge exists to communicate.
    const writeText = stubClipboard()
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: FULL } }) }))
    render(<BuildBadge />)
    const btn = await screen.findByText('build 9b9836a')

    btn.click()

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(FULL))
    expect(writeText).not.toHaveBeenCalledWith('9b9836a')
  })

  it('confirms the copy, then goes back to showing the build', async () => {
    stubClipboard()
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: FULL } }) }))
    render(<BuildBadge />)
    const btn = await screen.findByText('build 9b9836a')

    btn.click()

    expect(await screen.findByText('copied')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('build 9b9836a')).toBeInTheDocument(), { timeout: 3000 })
  })

  it('renders an unstamped local build as "unknown" rather than slicing it', async () => {
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: 'unknown' } }) }))

    render(<BuildBadge />)

    const el = await screen.findByText('build unknown')
    expect(el).toBeInTheDocument()
    expect(el.getAttribute('title')).toMatch(/not stamped with a commit/i)
  })

  it('titles a real commit with the full value and the copy hint', async () => {
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: FULL } }) }))

    render(<BuildBadge />)

    const el = await screen.findByText('build 9b9836a')
    expect(el.getAttribute('title')).toBe(`Build ${FULL} — click to copy`)
  })

  it('renders nothing when /api/health does not answer ok', async () => {
    stubFetch(async () => ({ ok: false, json: async () => ({}) }))

    const { container } = render(<BuildBadge />)

    await waitFor(() => expect(container.querySelector('button')).toBeNull())
  })

  it('renders nothing, and does not throw, when the fetch itself rejects', async () => {
    // Silent by design: a version string must never put an error on a page
    // that is otherwise working.
    stubFetch(async () => { throw new Error('network down') })

    const { container } = render(<BuildBadge />)

    await waitFor(() => expect(container.querySelector('button')).toBeNull())
  })

  it('renders nothing when the payload carries no commit', async () => {
    stubFetch(async () => ({ ok: true, json: async () => ({ build: {} }) }))

    const { container } = render(<BuildBadge />)

    await waitFor(() => expect(container.querySelector('button')).toBeNull())
  })

  it('survives a clipboard that refuses (non-https origin) without crashing', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('blocked')) },
      configurable: true, writable: true,
    })
    stubFetch(async () => ({ ok: true, json: async () => ({ build: { commit: FULL } }) }))
    render(<BuildBadge />)
    const btn = await screen.findByText('build 9b9836a')

    btn.click()

    // Still the build label, never "copied", and no unhandled rejection.
    await waitFor(() => expect(screen.getByText('build 9b9836a')).toBeInTheDocument())
  })

  it('does not set state after unmount when the response lands late', async () => {
    let resolve: (v: unknown) => void = () => {}
    stubFetch(() => new Promise(r => { resolve = r }))
    const { unmount } = render(<BuildBadge />)

    unmount()
    resolve({ ok: true, json: async () => ({ build: { commit: FULL } }) })

    // The `alive` flag guards this; reaching here without a warning is the point.
    await waitFor(() => expect(screen.queryByText('build 9b9836a')).toBeNull())
  })
})
