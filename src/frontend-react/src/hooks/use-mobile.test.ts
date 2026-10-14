/**
 * useIsMobile — the breakpoint the sidebar collapses on.
 *
 * Worth testing for one reason: the hook seeds its state `undefined` and only
 * resolves it in an effect, then returns `!!isMobile`. So the FIRST render of
 * every consumer says "not mobile" regardless of the real viewport, and a
 * consumer that renders a different tree on that first pass will flash the
 * desktop layout on a phone. These pin that behaviour rather than assume it.
 */
import { renderHook, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useIsMobile } from './use-mobile'

const BREAKPOINT = 768

/** Replace matchMedia with one that records its listener so we can fire it. */
function stubMatchMedia() {
  const listeners: (() => void)[] = []
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: window.innerWidth < BREAKPOINT,
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: () => void) => listeners.push(cb),
    removeEventListener: (_: string, cb: () => void) => {
      const i = listeners.indexOf(cb)
      if (i >= 0) listeners.splice(i, 1)
    },
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
  return listeners
}

function setWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: px })
}

const originalWidth = window.innerWidth
afterEach(() => {
  setWidth(originalWidth)
  vi.restoreAllMocks()
})

describe('useIsMobile', () => {
  it('reports mobile for a viewport below the 768px breakpoint', () => {
    stubMatchMedia()
    setWidth(500)

    const { result } = renderHook(() => useIsMobile())

    expect(result.current).toBe(true)
  })

  it('reports not-mobile at exactly the breakpoint, not one below it', () => {
    // 768 is the first DESKTOP width: the query is max-width 767px and the
    // comparison is `< 768`. An off-by-one here flips the layout for every
    // tablet held at exactly 768.
    stubMatchMedia()
    setWidth(BREAKPOINT)

    const { result } = renderHook(() => useIsMobile())

    expect(result.current).toBe(false)
  })

  it('reports mobile one pixel below the breakpoint', () => {
    stubMatchMedia()
    setWidth(BREAKPOINT - 1)

    const { result } = renderHook(() => useIsMobile())

    expect(result.current).toBe(true)
  })

  it('re-reads the width when the media query changes, not just on mount', () => {
    const listeners = stubMatchMedia()
    setWidth(1200)
    const { result } = renderHook(() => useIsMobile())
    expect(result.current).toBe(false)

    setWidth(400)
    act(() => { listeners.forEach(cb => cb()) })

    expect(result.current).toBe(true)
  })

  it('removes its listener on unmount', () => {
    const listeners = stubMatchMedia()
    const { unmount } = renderHook(() => useIsMobile())
    expect(listeners).toHaveLength(1)

    unmount()

    // A leaked listener would call setState on an unmounted hook on every
    // resize for the lifetime of the tab.
    expect(listeners).toHaveLength(0)
  })
})
