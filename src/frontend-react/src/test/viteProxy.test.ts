/**
 * The dev server's proxy table must never swallow an SPA page.
 *
 * Vite matches a proxy key that does not start with `^` by
 * `url.startsWith(key)`, so a bare `'/test'` key sent a reload of the Tests
 * page (`/tests`) to FastAPI instead of the SPA. Every route path App.tsx
 * declares is read from its source (the `path="…"` of each <Route> and the
 * `path: '…'` of each PAGES entry), so a new page or auth route is checked
 * without anyone remembering to list it here.
 */
import { describe, expect, it } from 'vitest'

import appText from '../App.tsx?raw'
import viteConfigText from '../../vite.config.ts?raw'

// Read as text (?raw): importing vite.config.ts into the jsdom environment trips
// esbuild's TextEncoder check, and the keys are plain string literals.
const proxyKeys = [...viteConfigText.matchAll(/^\s*'([^']+)':\s*\{ target:/gm)].map(m => m[1])

const routePaths = [
  ...new Set(
    [...appText.matchAll(/\bpath(?:=|:\s*)["']([^"']+)["']/g)].map(m => m[1]).filter(p => p !== '*'),
  ),
]

describe('vite dev proxy', () => {
  it('lists proxy keys at all (the table is read, not empty)', () => {
    expect(proxyKeys).toContain('/api')
  })

  it('reads the route paths from App.tsx (a pattern that matches nothing must fail)', () => {
    expect(routePaths).toContain('/tests')
    expect(routePaths).toContain('/login')
  })

  it.each(routePaths)('does not capture the SPA page %s', pagePath => {
    const captured = proxyKeys.filter(key => !key.startsWith('^') && pagePath.startsWith(key))
    expect(captured).toEqual([])
  })
})
