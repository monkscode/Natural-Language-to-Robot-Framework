/**
 * The two gate TABLES must agree, not just share the predicate.
 *
 * pageGates.ts said a future edit "can no longer change one and silently leave
 * the other stale". Sharing gateAllowed removes rule drift; it does nothing
 * about table drift. App.tsx's PAGES and app-sidebar.tsx's NAV_PLATFORM /
 * NAV_WORKSPACE are still two independent lists of flags for the same paths,
 * and flipping one of them — say PAGES./learning.admin = true while the nav
 * entry stays on viewLearning — leaves App.test.tsx and app-sidebar.test.tsx
 * both green while the sidebar shows a link to a page that immediately
 * redirects to /generate.
 *
 * So this compares the tables themselves, for every path present in both. It
 * is the claim made true rather than softened.
 *
 * Pure data, no render: this package tests no page components
 * (vite.config.ts).
 */
import { describe, expect, it } from 'vitest'
import { PAGES } from '../App'
import { NAV_PLATFORM, NAV_WORKSPACE } from '@/components/app-sidebar'
import { gateAllowed, type Gate, type GateFlags } from './pageGates'

/** Gate has optional booleans; NavItem's `admin` is required. Compare what
 *  gateAllowed reads, normalised, so `admin: false` and `admin: undefined`
 *  are not spuriously different. */
const shape = (g: Gate) => ({
  admin: !!g.admin,
  orgAdmin: !!g.orgAdmin,
  viewLearning: !!g.viewLearning,
})

const NAV = [...NAV_PLATFORM, ...NAV_WORKSPACE]

/** Every combination of the three flags — the gate objects must agree on all
 *  of them, not merely on the one the current roles happen to exercise. */
const ALL_FLAGS: GateFlags[] = [false, true].flatMap(isAdmin =>
  [false, true].flatMap(isOrgAdmin =>
    [false, true].map(canViewLearning => ({ isAdmin, isOrgAdmin, canViewLearning }))))

describe('PAGES and the nav tables', () => {
  it('every nav item points at a real page', () => {
    const paths = new Set(PAGES.map(p => p.path))
    for (const item of NAV) expect(paths).toContain(item.url)
  })

  it('carry the identical gate for every path they share', () => {
    for (const item of NAV) {
      const page = PAGES.find(p => p.path === item.url)!
      expect(shape(item), `nav "${item.title}" (${item.url}) disagrees with its PAGES entry`)
        .toEqual(shape(page))
    }
  })

  it('so no caller can see a link to a page that would bounce them', () => {
    for (const item of NAV) {
      const page = PAGES.find(p => p.path === item.url)!
      for (const flags of ALL_FLAGS) {
        expect(gateAllowed(item, flags), `${item.url} with ${JSON.stringify(flags)}`)
          .toBe(gateAllowed(page, flags))
      }
    }
  })
})

describe('the guard on this guard', () => {
  it('fails when a page gate is changed without its nav entry', () => {
    // The exact drift the claim above is about, forced by hand: this is what
    // the loops would catch, proved rather than asserted.
    const page: Gate = { admin: true }
    const nav: Gate = { viewLearning: true }
    expect(shape(page)).not.toEqual(shape(nav))
    expect(gateAllowed(page, { isAdmin: false, isOrgAdmin: false, canViewLearning: true }))
      .not.toBe(gateAllowed(nav, { isAdmin: false, isOrgAdmin: false, canViewLearning: true }))
  })
})
