"""Did the browser-service code actually change between two PyPI releases?

Merging any PR into the browser-service repo auto-publishes a patch bump, so a
README rewrite produces a release with no code in it: 1.0.36's wheel is
byte-identical to 1.0.35's across all 36 modules — only the dist-info version
strings differ. A watchdog that compares version strings opens a PR for that,
and a stream of no-op PRs is how a maintainer learns to merge them unread. The
one that finally moves browser-use then goes through unexamined.

So compare the code. Usage:

    python browser_service_code_diff.py <pinned> <latest>

Writes `code_changed` and a `summary` to $GITHUB_OUTPUT when it is set, and
prints the same to stdout. Fails OPEN — if either wheel cannot be read, it
reports a change, because refusing to notice is the failure being fixed.
"""

import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

PACKAGE = "browser-service"
PREFIX = "browser_service/"
TIMEOUT = 60


def _wheel_members(version: str) -> dict[str, str] | None:
    """sha256 of every packaged module in `version`, or None if unreadable."""
    url = f"https://pypi.org/pypi/{PACKAGE}/{version}/json"
    try:
        meta = json.loads(urllib.request.urlopen(url, timeout=TIMEOUT).read())
        wheel = next(f for f in meta["urls"] if f["filename"].endswith(".whl"))
        blob = urllib.request.urlopen(wheel["url"], timeout=TIMEOUT).read()
    except Exception as e:  # noqa: BLE001 — any failure means "cannot compare"
        print(f"could not read {PACKAGE} {version}: {e}", file=sys.stderr)
        return None

    zf = zipfile.ZipFile(io.BytesIO(blob))
    # dist-info is excluded on purpose: it carries the version string itself, so
    # including it would make every release look like a change, which is the
    # whole bug.
    return {
        name[len(PREFIX):]: hashlib.sha256(zf.read(name)).hexdigest()
        for name in zf.namelist()
        if name.startswith(PREFIX) and not name.endswith("/")
    }


def compare(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Human-readable lines describing what moved. Empty means nothing did."""
    lines = []
    for name in sorted(set(new) - set(old)):
        lines.append(f"added:    {name}")
    for name in sorted(set(old) - set(new)):
        lines.append(f"removed:  {name}")
    for name in sorted(set(old) & set(new)):
        if old[name] != new[name]:
            lines.append(f"changed:  {name}")
    return lines


def main() -> int:
    pinned, latest = sys.argv[1], sys.argv[2]
    old, new = _wheel_members(pinned), _wheel_members(latest)

    if old is None or new is None:
        changed, summary = True, (
            "Could not read one of the wheels, so the code was not compared. "
            "Treating this as a change — review the release notes by hand.")
    else:
        diff = compare(old, new)
        changed = bool(diff)
        if changed:
            summary = (f"{len(diff)} of {len(new)} packaged modules moved between "
                       f"{pinned} and {latest}:\n\n```\n" + "\n".join(diff) + "\n```")
        else:
            summary = (f"**No code change.** All {len(new)} packaged modules are "
                       f"byte-identical between {pinned} and {latest}; only the "
                       "dist-info version differs. The auto-publish on the "
                       "browser-service side bumps a patch for every merge, "
                       "including docs-only ones.")

    print(summary)
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"code_changed={'true' if changed else 'false'}\n")
            fh.write("summary<<CODE_DIFF_EOF\n" + summary + "\nCODE_DIFF_EOF\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
