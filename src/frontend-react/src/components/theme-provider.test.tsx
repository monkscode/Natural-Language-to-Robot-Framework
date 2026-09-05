/**
 * ThemeProvider — colour mode and visual theme, both persisted to
 * localStorage and both applied as classes on <html>.
 *
 * The interesting part is that `mode: 'system'` is not a stored preference but
 * a live subscription: it has to follow the OS, and it has to stop following
 * it the moment the user picks light or dark explicitly. A provider that
 * leaves the listener attached keeps overriding the user's own choice the next
 * time the OS flips.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ThemeProvider, useTheme } from './theme-provider'

/** matchMedia whose match state we control, recording listeners so we can fire them. */
function stubMatchMedia(matches: boolean) {
  const listeners: ((e: { matches: boolean }) => void)[] = []
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches, media: query, onchange: null,
    addEventListener: (_: string, cb: (e: { matches: boolean }) => void) => listeners.push(cb),
    removeEventListener: (_: string, cb: (e: { matches: boolean }) => void) => {
      const i = listeners.indexOf(cb); if (i >= 0) listeners.splice(i, 1)
    },
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }))
  return listeners
}

function Probe() {
  const { mode, theme, setMode, setTheme } = useTheme()
  return (
    <div>
      <span data-testid="mode">{mode}</span>
      <span data-testid="theme">{theme}</span>
      <button onClick={() => setMode('dark')}>go dark</button>
      <button onClick={() => setMode('light')}>go light</button>
      <button onClick={() => setTheme('neo')}>go neo</button>
      <button onClick={() => setTheme('professional')}>go professional</button>
    </div>
  )
}

const html = () => document.documentElement

beforeEach(() => {
  localStorage.clear()
  html().className = ''
})
afterEach(() => {
  vi.restoreAllMocks()
  html().className = ''
})

describe('ThemeProvider', () => {
  it('defaults to system mode and the professional theme with nothing stored', () => {
    stubMatchMedia(false)

    render(<ThemeProvider><Probe /></ThemeProvider>)

    expect(screen.getByTestId('mode')).toHaveTextContent('system')
    expect(screen.getByTestId('theme')).toHaveTextContent('professional')
  })

  it('seeds both values from localStorage on mount', () => {
    localStorage.setItem('m1-mode', 'dark')
    localStorage.setItem('m1-theme', 'neo')
    stubMatchMedia(false)

    render(<ThemeProvider><Probe /></ThemeProvider>)

    expect(screen.getByTestId('mode')).toHaveTextContent('dark')
    expect(screen.getByTestId('theme')).toHaveTextContent('neo')
    expect(html()).toHaveClass('dark')
    expect(html()).toHaveClass('neo')
  })

  it('follows the OS when mode is system', () => {
    stubMatchMedia(true)   // OS says dark

    render(<ThemeProvider><Probe /></ThemeProvider>)

    expect(html()).toHaveClass('dark')
  })

  it('keeps following the OS while it stays on system', () => {
    const listeners = stubMatchMedia(false)
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(html()).not.toHaveClass('dark')

    act(() => { listeners.forEach(cb => cb({ matches: true })) })

    expect(html()).toHaveClass('dark')
  })

  it('stops following the OS once the user picks a mode explicitly', () => {
    // The defect this guards: an explicit "light" that the next OS flip
    // silently overrides.
    const listeners = stubMatchMedia(false)
    render(<ThemeProvider><Probe /></ThemeProvider>)

    act(() => { screen.getByText('go light').click() })
    expect(html()).not.toHaveClass('dark')

    // The system listener is torn down when mode leaves 'system', so any
    // surviving callback must no longer be able to reach the DOM class.
    act(() => { listeners.forEach(cb => cb({ matches: true })) })

    expect(html()).not.toHaveClass('dark')
    expect(screen.getByTestId('mode')).toHaveTextContent('light')
  })

  it('persists an explicit mode so a reload keeps it', () => {
    stubMatchMedia(false)
    render(<ThemeProvider><Probe /></ThemeProvider>)

    act(() => { screen.getByText('go dark').click() })

    expect(localStorage.getItem('m1-mode')).toBe('dark')
    expect(html()).toHaveClass('dark')
  })

  it('toggles the neo class on and off, and persists the choice', () => {
    stubMatchMedia(false)
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(html()).not.toHaveClass('neo')

    act(() => { screen.getByText('go neo').click() })
    expect(html()).toHaveClass('neo')
    expect(localStorage.getItem('m1-theme')).toBe('neo')

    act(() => { screen.getByText('go professional').click() })
    expect(html()).not.toHaveClass('neo')
    expect(localStorage.getItem('m1-theme')).toBe('professional')
  })

  it('keeps colour mode and visual theme independent', () => {
    // Two separate classes and two separate keys: picking neo must not
    // disturb dark, and vice versa.
    stubMatchMedia(false)
    render(<ThemeProvider><Probe /></ThemeProvider>)

    act(() => { screen.getByText('go dark').click() })
    act(() => { screen.getByText('go neo').click() })

    expect(html()).toHaveClass('dark')
    expect(html()).toHaveClass('neo')
    expect(localStorage.getItem('m1-mode')).toBe('dark')
    expect(localStorage.getItem('m1-theme')).toBe('neo')
  })
})
