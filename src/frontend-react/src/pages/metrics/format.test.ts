import { describe, expect, it } from 'vitest'

import { LEARNED_CONTEXT_BAR, formatContextTooltip, formatReductionPct } from './format'

describe('formatContextTooltip', () => {
  it.each([
    [1267, LEARNED_CONTEXT_BAR, -134.2, '1,267 (+134.2%)'],
    [728, LEARNED_CONTEXT_BAR, 25.69, '728 (−25.7%)'],
    [541, 'Baseline tokens', -134.2, '541'],
    [541, LEARNED_CONTEXT_BAR, 0, '541'],
    [541, LEARNED_CONTEXT_BAR, undefined, '541'],
    [undefined, LEARNED_CONTEXT_BAR, undefined, '0'],
  ])('prints %s / %s / %s as %s', (value, name, pct, expected) => {
    expect(formatContextTooltip(value, name, pct)).toBe(expected)
  })
})

describe('formatReductionPct', () => {
  it.each([
    [25.69, '−25.7%'],
    [-134.2, '+134.2%'],
    [-74.04, '+74.0%'],
    [0, '0.0%'],
    [-0.04, '0.0%'],
    [0.04, '0.0%'],
  ])('prints %s as %s', (pct, expected) => {
    expect(formatReductionPct(pct)).toBe(expected)
  })
})
