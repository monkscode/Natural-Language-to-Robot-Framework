import { describe, expect, it } from 'vitest'

import { formatReductionPct } from './format'

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
