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
 * Pure data, no render: comparing two constant tables needs neither.
 */
import { describe, expect, it } from 'vitest'
import { PAGES } from '../App'
import { NAV_PLATFORM, NAV_WORKSPACE } from '@/components/app-sidebar'
import { type Gate } from './pageGates'

/** Gate has optional booleans; NavItem's `admin` is required. Compare what
 *  gateAllowed reads, normalised, so `admin: false` and `admin: undefined`
 *  are not spuriously different. */
const shape = (g: Gate) => ({
  admin: !!g.admin,
  orgAdmin: !!g.orgAdmin,
  viewLearning: !!g.viewLearning,
})

const NAV = [...NAV_PLATFORM, ...NAV_WORKSPACE]

describe('PAGES and the nav tables', () => {
  it('every nav item points at a real page', () => {
    const paths = new Set(PAGES.map(p => p.path))
    for (const item of NAV) expect(paths).toContain(item.url)
  })

  // Equal shapes are the whole claim: shape() normalises exactly the three
  // flags gateAllowed reads, so two entries with equal shapes give equal
  // answers for every possible caller. Feeding both through gateAllowed for
  // all eight flag combinations was a second assertion that could not fail
  // while this one passed.
  it('carry the identical gate for every path they share, so no caller can see a link to a page that would bounce them', () => {
    for (const item of NAV) {
      const page = PAGES.find(p => p.path === item.url)!
      expect(shape(item), `nav "${item.title}" (${item.url}) disagrees with its PAGES entry`)
        .toEqual(shape(page))
    }
  })
})
