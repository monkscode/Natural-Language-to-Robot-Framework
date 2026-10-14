/**
 * testLabels — the two strings the Tests page and Activity must agree on.
 *
 * Both surfaces name the same test and the same version, so the fallbacks are
 * pinned here once rather than asserted twice through two pages' renders.
 */
import { describe, expect, it } from 'vitest'

import { labelFrom, versionLabel } from './testLabels'

describe('labelFrom', () => {
  it('prefers the name when a test has one', () => {
    expect(labelFrom('Checkout smoke', 'buy a thing')).toBe('Checkout smoke')
  })

  it('falls back to the description, which is the case D2 made permanent', () => {
    expect(labelFrom(null, 'buy a thing')).toBe('buy a thing')
  })

  it('trims, so a whitespace-only name does not win over a real description', () => {
    expect(labelFrom('   ', '  buy a thing  ')).toBe('buy a thing')
  })

  it('names a test with neither as a pasted test, which is the only kind that has none', () => {
    // Paste-and-execute mints a test with a NULL description (spec case 9), and
    // the spec names it "Pasted test" to match Activity's "Pasted code run".
    expect(labelFrom(null, null)).toBe('Pasted test')
  })
})

describe('versionLabel', () => {
  it('is short on screen and spelled out in the title', () => {
    expect(versionLabel(2)).toEqual({ text: 'v2', title: 'Ran version 2' })
  })

  it('says so when a result recorded no version at all', () => {
    expect(versionLabel(null)).toEqual({
      text: 'no version', title: 'This result names no version',
    })
  })
})
