/**
 * How a test and the version a result ran are named on screen.
 *
 * Shared by the Tests page (the row's headline and the drawer's timeline) and
 * by Activity (the chip that replaced the re-run pill). It moved here for the
 * same reason lib/time.ts and components/history/folderPermissions.ts did:
 * the second page needed it, and two copies of a user-facing string is the
 * shape a drift defect takes on this branch.
 */

/** What a test is called: its name, else its description.
 *
 *  Takes the two fields rather than a row, because the drawer and Activity
 *  both name the same test from payloads that are not a Tests row.
 *
 *  In practice the first term never fires: owner decision D2 made the test's
 *  name its per-org key and left `tests.name` NULL for good, and nothing in
 *  the backend writes that column. It is kept as the first term so a later
 *  short-title feature needs no migration and no second definition here. */
export function labelFrom(name: string | null, userQuery: string | null): string {
  return name?.trim() || userQuery?.trim() || 'Untitled test'
}

/** The version a result ran, short, with the sentence that explains it.
 *
 *  `null` is ordinary and permanent rather than missing data: a regeneration
 *  that fails attaches its run to the test without writing a version at all
 *  (owner decision D8(b), spec case 10). Both surfaces say the same two words
 *  for it. */
export function versionLabel(n: number | null): { text: string; title: string } {
  return n == null
    ? { text: 'no version', title: 'This result names no version' }
    : { text: `v${n}`, title: `Ran version ${n}` }
}
