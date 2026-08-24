/**
 * isAccessLoss — which failures mean "this is no longer yours to read".
 *
 * The Test Runs page drops a row from its loaded list on the strength of
 * this answer, so a false positive DELETES something the user can still
 * open. Every status below is one the re-run endpoint can actually return.
 */
import { describe, expect, it } from 'vitest'

import { ApiError, isAccessLoss } from './api'

describe('isAccessLoss', () => {
  it('is true when the resource is gone or was never ours', () => {
    expect(isAccessLoss(new ApiError(404, 'Run not found'))).toBe(true)
    // Not reachable on the re-run route today — it answers 404 so it cannot
    // leak existence — but the rule must already be right if that changes.
    expect(isAccessLoss(new ApiError(403, 'Forbidden'))).toBe(true)
  })

  it('is FALSE for a 409, which says the run has no stored code', () => {
    // The endpoint reaches its 409 only after the access check passed, so
    // the caller can still open the row and Regenerate from it. Treating
    // this as access loss was a real defect: the row-level Run again button
    // fires blind on exactly these runs, because the history payload
    // carries no robot_code for the row to check.
    const e = new ApiError(
      409,
      'No stored code for this run — it predates code persistence. Use Regenerate instead.',
    )
    expect(isAccessLoss(e)).toBe(false)
  })

  it('is false for client mistakes, throttling and server faults', () => {
    for (const status of [400, 422, 429, 500, 502, 503]) {
      expect(isAccessLoss(new ApiError(status, 'x'))).toBe(false)
    }
  })

  it('is false for anything that is not an ApiError', () => {
    // A dropped connection arrives as a bare TypeError, and lib/sse throws a
    // plain Error on 401 before redirecting to /login. Neither says the run
    // is gone, and an unknown shape must never evict.
    expect(isAccessLoss(new TypeError('Failed to fetch'))).toBe(false)
    expect(isAccessLoss(new Error('Session expired. Please sign in again.'))).toBe(false)
    expect(isAccessLoss({ status: 404 })).toBe(false)
    expect(isAccessLoss(undefined)).toBe(false)
    expect(isAccessLoss(null)).toBe(false)
  })
})
