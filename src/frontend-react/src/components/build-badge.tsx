/**
 * The commit this instance is running, shown in the sidebar footer.
 *
 * `-develop` and `-latest` are moving tags, so "which build am I on?" cannot be
 * answered from the outside — the image tag a container was pulled under says
 * nothing about the code inside it. The backend reports its stamp on
 * /api/health; this puts it where a user can read it out without knowing any
 * docker commands, which is what a bug report needs.
 *
 * Shows the 7-character prefix (git's own short form) and copies the full value
 * on click, so nothing is lost to the abbreviation.
 */

import { useEffect, useState } from 'react'

interface HealthBuild {
  build?: { commit?: string }
}

export function BuildBadge() {
  const [commit, setCommit] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    // Plain fetch, deliberately not lib/api: that wrapper clears the token and
    // redirects to /login on a 401. /api/health carries no auth guard today, but
    // a diagnostic label must never be able to sign anyone out if that changes.
    //
    // Failure is silent on purpose: an error toast for a version string would be
    // noise on a page that is working fine.
    fetch('/api/health', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? (r.json() as Promise<HealthBuild>) : null))
      .then((h) => { if (alive) setCommit(h?.build?.commit ?? null) })
      .catch(() => { /* leave it unrendered */ })
    return () => { alive = false }
  }, [])

  if (!commit) return null

  const short = commit === 'unknown' ? 'unknown' : commit.slice(0, 7)

  const copy = () => {
    navigator.clipboard?.writeText(commit).then(
      () => { setCopied(true); setTimeout(() => setCopied(false), 1500) },
      () => { /* clipboard blocked (non-https origin) — the title still shows it */ },
    )
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={commit === 'unknown'
        ? 'This build was not stamped with a commit (a local run, not a published image)'
        : `Build ${commit} — click to copy`}
      className="px-2 pb-1 text-left text-[10px] font-mono text-sidebar-foreground/50 hover:text-sidebar-foreground/80 group-data-[collapsible=icon]:hidden"
    >
      {copied ? 'copied' : `build ${short}`}
    </button>
  )
}
