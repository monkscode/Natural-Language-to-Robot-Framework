/**
 * Vitest setup — runs once per test file, before any test.
 *
 * Registers @testing-library/jest-dom's DOM matchers (toBeInTheDocument and
 * friends) on vitest's `expect`, and unmounts every rendered tree after each
 * test so one test's component cannot answer another test's query.
 */
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// jsdom implements no matchMedia at all, and reading it throws rather than
// returning undefined. useIsMobile and ThemeProvider both call it during their
// first effect, so without this any test that mounts the sidebar or the theme
// provider dies before its own assertions run. Defaults to "does not match",
// i.e. desktop width and light mode; a test that cares overrides it.
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

afterEach(() => cleanup())
