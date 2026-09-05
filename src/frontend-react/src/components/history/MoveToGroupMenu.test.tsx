/**
 * MoveToGroupMenu — the one filing control shared by the history row, the
 * detail drawer, and the bulk-select toolbar.
 *
 * Two things are load-bearing per the component's own docstring: the
 * `onClick={e => e.stopPropagation()}` guard on DropdownMenuContent (without
 * it, clicking an item bubbles through the REACT tree — which portals do NOT
 * escape — to whatever the caller wrapped this menu in, e.g. a history row's
 * onClick, opening that row's drawer behind the menu the user was actually
 * using), and "Remove from group" being offered under EITHER currentGroupId
 * OR showRemove (the bulk-select toolbar has no single currentGroupId but
 * still needs the control, per the `showRemove` prop comment). This file
 * pins both, plus the create-and-move flow and its error path.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MoveToGroupMenu } from './MoveToGroupMenu'
import type { RunGroup } from './useGroups'

afterEach(() => vi.resetAllMocks())

const CHECKOUT: RunGroup = { group_id: 'g-1', name: 'Checkout', created_by: 'u1', run_count: 3 }
const REGRESSION: RunGroup = { group_id: 'g-2', name: 'Regression', created_by: 'u1', run_count: 1 }

function renderMenu(overrides: Partial<React.ComponentProps<typeof MoveToGroupMenu>> = {}) {
  const props: React.ComponentProps<typeof MoveToGroupMenu> = {
    groups: [CHECKOUT, REGRESSION],
    currentGroupId: null,
    onMove: vi.fn(),
    onCreateGroup: vi.fn().mockResolvedValue({ group_id: 'g-3', name: 'New', created_by: 'u1', run_count: 0 }),
    trigger: <button type="button">Open menu</button>,
    ...overrides,
  }
  const utils = render(<MoveToGroupMenu {...props} />)
  return { ...utils, props }
}

// DropdownMenuTrigger (@radix-ui/react-dropdown-menu) opens on POINTERDOWN,
// not click (see its source: onPointerDown calls context.onOpenToggle(); the
// button carries no onClick open-handler at all) — a plain fireEvent.click
// never opens it. fireEvent.pointerDown ALSO doesn't work here: jsdom has no
// PointerEvent class, so testing-library falls back to a bare Event whose
// `.button` reads back undefined, and Radix's handler requires `=== 0`. A
// MouseEvent typed 'pointerdown' carries a real, settable `.button` and
// React routes it to onPointerDown the same way (it dispatches on the DOM
// event's type string, not its class) — verified against this exact
// component before writing the rest of the suite.
function pointerDown(el: Element) {
  fireEvent(el, new MouseEvent('pointerdown', { bubbles: true, cancelable: true, button: 0 }))
}
const openMenu = () => pointerDown(screen.getByText('Open menu'))

describe('MoveToGroupMenu — listing groups', () => {
  it('lists every group in the org', () => {
    renderMenu()
    openMenu()

    expect(screen.getByText('Checkout')).toBeInTheDocument()
    expect(screen.getByText('Regression')).toBeInTheDocument()
  })

  it('says "No groups yet." when the org has none', () => {
    renderMenu({ groups: [] })
    openMenu()

    expect(screen.getByText('No groups yet.')).toBeInTheDocument()
  })

  it('checks off the run’s CURRENT group and no other', () => {
    renderMenu({ currentGroupId: 'g-2' })
    openMenu()

    const regressionRow = screen.getByText('Regression').closest('[role="menuitem"]')!
    const checkoutRow = screen.getByText('Checkout').closest('[role="menuitem"]')!
    expect(regressionRow.querySelector('svg.lucide-check')).not.toBeNull()
    expect(checkoutRow.querySelector('svg.lucide-check')).toBeNull()
  })
})

describe('MoveToGroupMenu — moving', () => {
  it('calls onMove with the clicked group’s id', () => {
    const onMove = vi.fn()
    renderMenu({ onMove })
    openMenu()

    fireEvent.click(screen.getByText('Checkout'))

    expect(onMove).toHaveBeenCalledWith('g-1')
  })

  it('does not let a menu-item click escape to an ancestor click handler', () => {
    // The exact bug the component's docstring calls out: the menu is
    // portalled to <body>, but React's synthetic events bubble the REACT
    // tree, so an unguarded item click would also fire the history ROW's
    // onClick underneath it and open that run's drawer behind the menu.
    const onMove = vi.fn()
    const ancestorClick = vi.fn()
    render(
      <div onClick={ancestorClick}>
        <MoveToGroupMenu
          groups={[CHECKOUT]}
          currentGroupId={null}
          onMove={onMove}
          onCreateGroup={vi.fn()}
          trigger={<button type="button">Open menu</button>}
        />
      </div>,
    )
    pointerDown(screen.getByText('Open menu'))

    fireEvent.click(screen.getByText('Checkout'))

    expect(onMove).toHaveBeenCalledWith('g-1')
    expect(ancestorClick).not.toHaveBeenCalled()
  })
})

describe('MoveToGroupMenu — "Remove from group"', () => {
  it('is offered when the run already has a group, and calls onMove(null)', () => {
    const onMove = vi.fn()
    renderMenu({ currentGroupId: 'g-1', onMove })
    openMenu()

    fireEvent.click(screen.getByText('Remove from group'))

    expect(onMove).toHaveBeenCalledWith(null)
  })

  it('is offered for the bulk toolbar (showRemove) even with no single currentGroupId', () => {
    renderMenu({ currentGroupId: undefined, showRemove: true })
    openMenu()

    expect(screen.getByText('Remove from group')).toBeInTheDocument()
  })

  it('is hidden for an ungrouped run outside the bulk toolbar', () => {
    renderMenu({ currentGroupId: null, showRemove: false })
    openMenu()

    expect(screen.queryByText('Remove from group')).not.toBeInTheDocument()
  })
})

describe('MoveToGroupMenu — create-and-move', () => {
  it('disables Create & move until a name is entered', () => {
    renderMenu()
    openMenu()
    fireEvent.click(screen.getByText('New group…'))

    expect(screen.getByText('Create & move')).toBeDisabled()
  })

  it('creates the group, then moves into it using the SERVER-assigned id', async () => {
    const onMove = vi.fn()
    const onCreateGroup = vi.fn().mockResolvedValue({ group_id: 'g-server', name: 'Smoke', created_by: 'u1', run_count: 0 })
    renderMenu({ onMove, onCreateGroup })
    openMenu()
    fireEvent.click(screen.getByText('New group…'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Checkout flows'), { target: { value: '  Smoke  ' } })

    fireEvent.click(screen.getByText('Create & move'))

    await waitFor(() => expect(onCreateGroup).toHaveBeenCalledWith('Smoke'))
    expect(onMove).toHaveBeenCalledWith('g-server')
  })

  it('shows the server’s rejection and leaves the dialog open for another try', async () => {
    const onMove = vi.fn()
    const onCreateGroup = vi.fn().mockRejectedValue(new Error('A group with that name exists'))
    renderMenu({ onMove, onCreateGroup })
    openMenu()
    fireEvent.click(screen.getByText('New group…'))
    fireEvent.change(screen.getByPlaceholderText('e.g. Checkout flows'), { target: { value: 'Dup' } })

    fireEvent.click(screen.getByText('Create & move'))

    expect(await screen.findByText('A group with that name exists')).toBeInTheDocument()
    expect(onMove).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText('e.g. Checkout flows')).toBeInTheDocument()
  })

  it('closes on Cancel without creating or moving anything', () => {
    const onMove = vi.fn()
    const onCreateGroup = vi.fn()
    renderMenu({ onMove, onCreateGroup })
    openMenu()
    fireEvent.click(screen.getByText('New group…'))

    fireEvent.click(screen.getByText('Cancel'))

    expect(screen.queryByPlaceholderText('e.g. Checkout flows')).not.toBeInTheDocument()
    expect(onCreateGroup).not.toHaveBeenCalled()
    expect(onMove).not.toHaveBeenCalled()
  })
})
