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
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

/** Unique to the browse dialog's own copy — never collides with the "All
 *  Groups"/active-group CHIP, which stays mounted behind the dialog. */
const browseDialogOpen = () => screen.queryByText(/Pick a group to filter the runs/) !== null

describe('GroupChipsRow — the Ungrouped chip toggles the filter', () => {
  it('selects "ungrouped" when clicked while nothing is filtered', () => {
    const onSelect = vi.fn()
    renderRow({ active: null, onSelect })

    fireEvent.click(screen.getByText('Ungrouped'))

    expect(onSelect).toHaveBeenCalledWith('ungrouped')
  })

  it('clears the filter when clicked while Ungrouped IS already active', () => {
    const onSelect = vi.fn()
    renderRow({ active: 'ungrouped', onSelect })

    fireEvent.click(screen.getByText('Ungrouped'))

    expect(onSelect).toHaveBeenCalledWith(null)
  })
})

describe('GroupChipsRow — the groups control opens the browse dialog', () => {
  it('reads "All Groups" and opens the dialog when nothing is filtered', () => {
    renderRow({ active: null })
    expect(screen.getByText('All Groups')).toBeInTheDocument()

    openBrowse()

    expect(browseDialogOpen()).toBe(true)
  })

  it('reads as the active group’s own chip, and its LEFT side also opens the dialog', () => {
    renderRow({ active: 'g-1' })
    // "All Groups" must NOT be showing — the chip has switched identity.
    expect(screen.queryByText('All Groups')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTitle('Switch or manage groups'))

    expect(browseDialogOpen()).toBe(true)
  })

  it('the chip’s own ✕ clears the filter directly, WITHOUT opening the dialog', () => {
    const onSelect = vi.fn()
    renderRow({ active: 'g-1', onSelect })

    fireEvent.click(screen.getByLabelText('Clear the group filter'))

    expect(onSelect).toHaveBeenCalledWith(null)
    expect(browseDialogOpen()).toBe(false)
  })

  it('picking a group row in the dialog selects it and closes the dialog', () => {
    const onSelect = vi.fn()
    renderRow({ onSelect })
    openBrowse()

    fireEvent.click(screen.getByText('Checkout'))

    expect(onSelect).toHaveBeenCalledWith('g-1')
    expect(browseDialogOpen()).toBe(false)
  })
})

describe('GroupChipsRow — "Clear filter" inside the browse dialog', () => {
  it('is offered only while a group is actually active', () => {
    renderRow({ active: 'g-1' })
    fireEvent.click(screen.getByTitle('Switch or manage groups'))
    expect(screen.getByText('Clear filter')).toBeInTheDocument()
  })

  it('is absent with no active filter — nothing to clear', () => {
    renderRow({ active: null })
    openBrowse()
    expect(screen.queryByText('Clear filter')).not.toBeInTheDocument()
  })

  it('clears the selection and closes the dialog when clicked', () => {
    const onSelect = vi.fn()
    renderRow({ active: 'g-1', onSelect })
    fireEvent.click(screen.getByTitle('Switch or manage groups'))

    fireEvent.click(screen.getByText('Clear filter'))

    expect(onSelect).toHaveBeenCalledWith(null)
    expect(browseDialogOpen()).toBe(false)
  })
})

describe('GroupChipsRow — creating a group', () => {
  it('disables Create until a name is entered, then submits the TRIMMED value', async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined)
    renderRow({ onCreate })
    fireEvent.click(screen.getByText('New'))
    expect(screen.getByText('Create')).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('e.g. Checkout flows'), { target: { value: '  Regression  ' } })
    expect(screen.getByText('Create')).not.toBeDisabled()

    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith('Regression'))
  })

  it('shows the server’s rejection and keeps the dialog open', async () => {
    const onCreate = vi.fn().mockRejectedValue(new Error('A group named "Dup" already exists'))
    renderRow({ onCreate })
    fireEvent.click(screen.getByText('New'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Checkout flows'), { target: { value: 'Dup' } })

    fireEvent.click(screen.getByText('Create'))

    expect(await screen.findByText('A group named "Dup" already exists')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('e.g. Checkout flows')).toBeInTheDocument()
  })
})

describe('GroupChipsRow — renaming a group', () => {
  it('calls onRename with the group id and the TRIMMED new name', async () => {
    const onRename = vi.fn().mockResolvedValue(undefined)
    renderRow({ canRename: () => true, onRename })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Rename Checkout'))

    fireEvent.change(screen.getByDisplayValue('Checkout'), { target: { value: '  Checkout Flow  ' } })
    fireEvent.click(screen.getByText('Rename'))

    await waitFor(() => expect(onRename).toHaveBeenCalledWith('g-1', 'Checkout Flow'))
  })

  it('starts with Rename disabled — the freshly-opened field equals the current name', () => {
    // submitDisabled treats an UNCHANGED name as nothing to submit, so the
    // button must not invite a no-op rename before the user edits anything.
    renderRow({ canRename: () => true, onRename: vi.fn() })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Rename Checkout'))

    expect(screen.getByText('Rename')).toBeDisabled()
  })

  it('skips the onRename call when the form is submitted with the name unchanged', async () => {
    // The button's disabled attribute already defends this (previous test);
    // this pins the SEPARATE guard inside submit() itself
    // (`if (name.trim() !== overlay.group.name)`) by submitting the form
    // directly, bypassing the disabled button — a realistic path if the two
    // guards ever drift apart.
    const onRename = vi.fn()
    renderRow({ canRename: () => true, onRename })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Rename Checkout'))

    // The dialog is portalled to document.body (a sibling of render()'s own
    // container, not a descendant), so find the form via the input rather
    // than container.querySelector.
    fireEvent.submit(screen.getByDisplayValue('Checkout').closest('form')!)

    await waitFor(() => expect(browseDialogOpen()).toBe(true))
    expect(onRename).not.toHaveBeenCalled()
  })

  it('shows the server’s rejection and keeps the dialog open', async () => {
    const onRename = vi.fn().mockRejectedValue(new Error('Only the creator or an org admin may rename this group'))
    renderRow({ canRename: () => true, onRename })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Rename Checkout'))
    fireEvent.change(screen.getByDisplayValue('Checkout'), { target: { value: 'Renamed' } })

    fireEvent.click(screen.getByText('Rename'))

    expect(await screen.findByText('Only the creator or an org admin may rename this group')).toBeInTheDocument()
  })
})

describe('GroupChipsRow — deleting a group', () => {
  it('confirms, then calls onDelete with the group id', async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined)
    renderRow({ canDelete: () => true, onDelete })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Delete Checkout'))

    fireEvent.click(screen.getByText('Delete group'))

    await waitFor(() => expect(onDelete).toHaveBeenCalledWith('g-1'))
  })

  it('shows the server’s refusal and leaves the confirmation open', async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error('Only an org admin may delete a group'))
    renderRow({ canDelete: () => true, onDelete })
    openBrowse()
    fireEvent.click(screen.getByLabelText('Delete Checkout'))

    fireEvent.click(screen.getByText('Delete group'))

    expect(await screen.findByText('Only an org admin may delete a group')).toBeInTheDocument()
  })
})
