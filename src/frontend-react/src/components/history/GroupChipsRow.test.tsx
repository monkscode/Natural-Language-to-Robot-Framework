/**
 * GroupChipsRow — which controls the row actually draws.
 *
 * The authority RULES live in HistoryPage (canRename / canDelete are built
 * there from the caller's identity, and the server refuses the rest anyway).
 * What this file pins is the row's half of the contract: each management
 * control appears if and only if its predicate says so, so a caller is never
 * offered a button that can only answer 404 — and never denied one that
 * would have worked.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { GroupChipsRow } from './GroupChipsRow'
import type { RunGroup } from './useGroups'

const CHECKOUT: RunGroup = {
  group_id: 'g-1', name: 'Checkout', created_by: 'u-creator', run_count: 4,
}

const noop = async () => {}

function renderRow(overrides: Partial<React.ComponentProps<typeof GroupChipsRow>> = {}) {
  const props: React.ComponentProps<typeof GroupChipsRow> = {
    groups: [CHECKOUT],
    ungroupedCount: 9,
    active: null,
    onSelect: vi.fn(),
    onCreate: noop,
    onRename: noop,
    onDelete: noop,
    canRename: () => false,
    canDelete: () => false,
    canCreate: true,
    ...overrides,
  }
  const utils = render(<GroupChipsRow {...props} />)
  return { ...utils, props }
}

/** Rename/Delete live in the browse dialog, behind the groups control. */
const openBrowse = () => fireEvent.click(screen.getByTitle('Browse and manage groups'))

describe('GroupChipsRow', () => {
  it('offers a folder’s creator Rename but not Delete', () => {
    // Deleting returns every run inside to Ungrouped, un-publishing the
    // org's work — an org-level act, so not the creator's to make (D9).
    renderRow({ canRename: () => true, canDelete: () => false })
    openBrowse()

    expect(screen.getByLabelText('Rename Checkout')).toBeInTheDocument()
    expect(screen.queryByLabelText('Delete Checkout')).not.toBeInTheDocument()
  })

  it('offers an org admin both', () => {
    renderRow({ canRename: () => true, canDelete: () => true })
    openBrowse()

    expect(screen.getByLabelText('Rename Checkout')).toBeInTheDocument()
    expect(screen.getByLabelText('Delete Checkout')).toBeInTheDocument()
  })

  it('offers an unrelated member neither', () => {
    renderRow()
    openBrowse()

    expect(screen.queryByLabelText('Rename Checkout')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Delete Checkout')).not.toBeInTheDocument()
    // Still browsable and filterable — reading a folder is not managing it.
    expect(screen.getByText('Checkout')).toBeInTheDocument()
  })

  it('says "No groups yet" only once the list is actually empty', () => {
    // Reachable by deleting the last folder with the dialog open: it returns
    // to the browse list, which is now empty.
    const { rerender, props } = renderRow({ canDelete: () => true })
    openBrowse()
    expect(screen.queryByText(/No groups yet/)).not.toBeInTheDocument()

    rerender(<GroupChipsRow {...props} groups={[]} canDelete={() => true} />)

    expect(screen.getByText(/No groups yet/)).toBeInTheDocument()
  })

  it('hides the Ungrouped chip while there are no folders', () => {
    // With nothing to be grouped into, every run is ungrouped: the chip
    // equals the total and filtering by it changes nothing.
    renderRow({ groups: [] })

    expect(screen.queryByText('Ungrouped')).not.toBeInTheDocument()
    // ＋ New survives — it is how the first folder gets made.
    expect(screen.getByText('New')).toBeInTheDocument()
  })

  it('keeps the Ungrouped chip while it IS the active filter', () => {
    // Deleting the last folder must not take away the only control that
    // clears the filter it left behind.
    renderRow({ groups: [], active: 'ungrouped' })

    expect(screen.getByText('Ungrouped')).toBeInTheDocument()
  })
})
