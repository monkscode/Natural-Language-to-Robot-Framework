/**
 * The one predicate that decides whether a page/nav-link pair is reachable.
 *
 * Lives in its own module rather than in App.tsx or app-sidebar.tsx: App.tsx
 * already imports app-sidebar.tsx (for <AppSidebar />), so app-sidebar.tsx
 * importing a predicate back out of App.tsx would be a module cycle. Both
 * files import gateAllowed from here instead, so the page a URL reaches and
 * the nav link that offers it read the SAME rule — a future edit can no
 * longer change one and silently leave the other stale (a nav link to a page
 * that then bounces, or a hidden link to a page still reachable by URL).
 *
 * Pure, and exported so the /learning divergence this predicate fixes (an
 * org admin the API admits could not open the page the SPA gated on
 * platform `admin`) is testable without rendering App or the sidebar — see
 * App.test.tsx and app-sidebar.test.tsx.
 */

/** The gating requirements a page or nav item can carry. All optional: an
 * entry with none of these is reachable by every authenticated user. */
export interface Gate {
  admin?: boolean
  orgAdmin?: boolean
  viewLearning?: boolean
}

/** The caller's role flags, exactly as useAuth() exposes them. */
export interface GateFlags {
  isAdmin: boolean
  isOrgAdmin: boolean
  canViewLearning: boolean
}

export function gateAllowed(gate: Gate, auth: GateFlags): boolean {
  return (!gate.admin || auth.isAdmin) && (!gate.orgAdmin || auth.isOrgAdmin) &&
    (!gate.viewLearning || auth.canViewLearning)
}
