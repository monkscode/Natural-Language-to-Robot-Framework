/**
 * Context reduction as the Metrics page prints it. The stored value is
 * (baseline - optimized) / baseline * 100: positive = the learned context is
 * smaller (shown "−"), negative = larger (shown "+"). The sign is read from the
 * value rounded to one decimal, so a rounded-away change reads "0.0%".
 */
export function formatReductionPct(pct: number): string {
  const rounded = Number(pct.toFixed(1))
  if (rounded > 0) return `−${rounded.toFixed(1)}%`
  if (rounded < 0) return `+${Math.abs(rounded).toFixed(1)}%`
  return '0.0%'
}
