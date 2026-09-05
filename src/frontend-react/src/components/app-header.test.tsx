/**
 * AppHeader — the breadcrumb page label and the two theme controls
 * (light/dark/system mode, and the professional/neo visual toggle). Both
 * controls are built from a .map() over three/two near-identical buttons, so
 * the failure mode worth guarding is a button calling back with the WRONG
 * id/theme — a neighbour's instead of its own. The breadcrumb label is no
 * longer a hand-maintained table: it is derived from the nav tables, and the
 * drift guard below is what makes that derivation load-bearing.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/theme-provider', () => ({ useTheme: vi.fn() }))

import { useTheme } from '@/components/theme-provider'
import { SidebarProvider } from '@/components/ui/sidebar'
import { NAV_PLATFORM, NAV_WORKSPACE } from '@/components/app-sidebar'
import { AppHeader } from './app-header'

const mockUseTheme = vi.mocked(useTheme)

// SidebarProvider's useIsMobile() calls window.matchMedia via a plain
// useEffect. setup.ts installs a working stub once per FILE, but this file's
// own `afterEach(vi.resetAllMocks)` strips that stub's implementation after
// test 1 (resetAllMocks resets every vi.fn(), including that one), so every
// test after the first would otherwise call matchMedia() and get back
// undefined. theme-provider.test.tsx hits the same interaction and re-installs
// its own stub per test for the same reason — same fix here.
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }))
})
afterEach(() => vi.resetAllMocks())

function renderAt(path: string, mode: 'light' | 'dark' | 'system', theme: 'professional' | 'neo') {
  const setMode = vi.fn()
  const setTheme = vi.fn()
  mockUseTheme.mockReturnValue({ mode, setMode, theme, setTheme })
  render(
    <MemoryRouter initialEntries={[path]}>
      {/* AppHeader renders <SidebarTrigger>, which reads useSidebar() —
          a real (not mocked) SidebarProvider is the cheapest way to satisfy
          that without re-implementing the sidebar's own context. */}
      <SidebarProvider>
        <AppHeader />
      </SidebarProvider>
    </MemoryRouter>,
  )
  return { setMode, setTheme }
}

describe('AppHeader — breadcrumb page label', () => {
  it.each([
    ['/generate', 'Generate'],
    ['/history', 'Test Runs'],
    ['/metrics', 'Metrics'],
    ['/learning', 'Learning'],
    ['/access', 'Access'],
    ['/team', 'Team'],
    ['/settings', 'Settings'],
  ])('labels %s as "%s"', (path, label) => {
    renderAt(path, 'system', 'professional')
    expect(screen.getByText('Platform')).toBeInTheDocument()
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('falls back to Overview for a path that is not a page', () => {
    renderAt('/some-unmapped-path', 'system', 'professional')
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })

  /* The bug this replaced: /access and /team shipped as real gated routes and
   * the header's own path->label table was never extended, so both rendered
   * "Overview" on every load — while the table still carried a /docs entry for
   * a route that was never declared. A third hand-maintained table of the same
   * paths is what made that possible, so the fix removed the table rather than
   * adding two rows to it. This is the guard that keeps it removed: a nav entry
   * whose label the header cannot produce is a regression, whoever adds it. */
  it('labels every navigable page with the SAME words the sidebar uses for it', () => {
    for (const item of [...NAV_PLATFORM, ...NAV_WORKSPACE]) {
      cleanup()
      renderAt(item.url, 'system', 'professional')
      expect(screen.queryByText('Overview'), `"${item.title}" (${item.url}) has no header label`)
        .not.toBeInTheDocument()
      expect(screen.getByText(item.title)).toBeInTheDocument()
    }
  })

  it('no longer labels /docs, which is not a declared route', () => {
    renderAt('/docs', 'system', 'professional')
    expect(screen.getByText('Overview')).toBeInTheDocument()
    expect(screen.queryByText('Documentation')).not.toBeInTheDocument()
  })
})

describe('AppHeader — light/dark/system toggle', () => {
  it('calls setMode with the id of the button that was clicked, not a neighbour', () => {
    const { setMode } = renderAt('/generate', 'system', 'professional')

    fireEvent.click(screen.getByLabelText('Dark mode'))

    expect(setMode).toHaveBeenCalledWith('dark')
    expect(setMode).not.toHaveBeenCalledWith('light')
    expect(setMode).not.toHaveBeenCalledWith('system')
  })

  it('marks only the current mode button as active', () => {
    renderAt('/generate', 'dark', 'professional')

    expect(screen.getByLabelText('Dark mode').className).toContain('bg-background')
    expect(screen.getByLabelText('Light mode').className).not.toContain('bg-background')
    expect(screen.getByLabelText('System preference').className).not.toContain('bg-background')
  })
})

describe('AppHeader — professional/neo toggle', () => {
  it('offers Neo (the OTHER theme) while professional, and switches to it on click', () => {
    const { setTheme } = renderAt('/generate', 'system', 'professional')
    const btn = screen.getByLabelText('Toggle neobrutalism theme')

    expect(btn.textContent).toContain('Neo')
    fireEvent.click(btn)

    expect(setTheme).toHaveBeenCalledWith('neo')
  })

  it('offers Pro (the OTHER theme) while neo, and switches back to professional on click', () => {
    const { setTheme } = renderAt('/generate', 'system', 'neo')
    const btn = screen.getByLabelText('Toggle neobrutalism theme')

    expect(btn.textContent).toContain('Pro')
    fireEvent.click(btn)

    expect(setTheme).toHaveBeenCalledWith('professional')
  })
})
