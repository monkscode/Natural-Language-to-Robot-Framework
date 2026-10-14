/**
 * The dev server's proxy table must never swallow an SPA page.
 *
 * Vite matches a proxy key that does not start with `^` by
 * `url.startsWith(key)`, so a bare `'/test'` key sent a reload of the Tests
 * page (`/tests`) to FastAPI instead of the SPA. Pinned against the pages
 * App.tsx lists, so a new page or a new broad key is caught here, not by a
 * developer reloading the browser.
 */
import { describe, expect, it } from 'vitest'

import { PAGES } from '@/App'
import viteConfigText from '../../vite.config.ts?raw'

// Read as text (?raw): importing vite.config.ts into the jsdom environment trips
// esbuild's TextEncoder check, and the keys are plain string literals.
const proxyKeys = [...viteConfigText.matchAll(/^\s*'([^']+)':\s*\{ target:/gm)].map(m => m[1])

// Auth routes App.tsx declares outside PAGES.
const AUTH_PATHS = ['/login', '/signup', '/forgot-password', '/oauth/callback']

describe('vite dev proxy', () => {
  it('lists proxy keys at all (the table is read, not empty)', () => {
    expect(proxyKeys).toContain('/api')
  })

  it.each([...PAGES.map(p => p.path), ...AUTH_PATHS])('does not capture the SPA page %s', pagePath => {
    const captured = proxyKeys.filter(key => !key.startsWith('^') && pagePath.startsWith(key))
    expect(captured).toEqual([])
  })
})
