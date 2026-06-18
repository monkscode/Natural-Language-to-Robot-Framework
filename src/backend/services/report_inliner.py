"""Inline external screenshot references in Robot Framework report HTML.

Once /reports serves HTML under `Content-Security-Policy: sandbox`
(report_endpoints.serve_report_file), the report document has an opaque origin
and its sub-resource requests no longer carry the report cookie — so an external
<img src="screenshot.png"> would 404. This module rewrites those references to
self-contained data: URIs BEFORE the run is persisted/served.

THIS MODULE IS THE SINGLE OWNER OF ROBOT'S LOG-HTML SCREENSHOT FORMAT. If a Robot
upgrade changes how screenshots are referenced, update _SCREENSHOT_REF_RE here —
nowhere else. inline_report_screenshots also logs a WARNING when screenshot files
exist on disk but were not inlined, the signature of such a format change.

Referenced by: services/workflow_service.py (called once after execution, before
artifact_store.persist_run).
Depends on: stdlib only.
"""

import base64
import logging
import mimetypes
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Report HTML we rewrite (run root only). dryrun/ artifacts are throwaway and
# never served as a report, so they are intentionally excluded.
_REPORT_HTML_FILES = ("log.html", "report.html")

# An image reference inside Robot's log.html. The reference lives in the embedded
# `window.output` JS blob, so the quotes are backslash-escaped:
#   <img src=\"browser/screenshot/fail-screenshot-1.png\" .../>
#   <a href=\"browser/screenshot/fail-screenshot-1.png\" ...>
# The optional backslash (\\?) also matches a plain unescaped quote, so a future
# Robot quoting change or a raw .html file still works. The path is anchored only
# by "image extension + the file exists on disk" (see inline_report_screenshots),
# NOT a hardcoded folder — so the Browser layout (browser/screenshot/…) and the
# Selenium layout (selenium-screenshot-1.png at run root) both work unchanged.
_SCREENSHOT_REF_RE = re.compile(
    r'(src|href)=(\\?")([^"\\]+\.(?:png|jpe?g|gif))(\\?")'
)

# The image extensions _SCREENSHOT_REF_RE accepts, as a tuple the drift detector
# (_warn_on_uninlined) reuses — so the "present but not inlined" signal never lags
# the matcher. Keep in sync with the extension group in the regex above.
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")


def _resolve_within(base: Path, relpath: str) -> Path | None:
    """Resolve *relpath* under *base*; None if it escapes (.., absolute, symlink).
    Mirrors artifact_store's containment check; kept local so this module stays
    dependency-free."""
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, relpath))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return Path(target)


def _atomic_rewrite(path: Path, content: str, count: int) -> int:
    """Replace *path*'s contents atomically and return *count*, or 0 on failure.

    /reports streams this file on another thread, so a plain write_text
    (truncate-then-write in place) could be read mid-write as a truncated/blank
    report. Writing a sibling temp and os.replace-ing it in is atomic on POSIX
    and Windows. On Windows os.replace can still raise if a reader holds the file
    open; we swallow it (the complete old file stays) and clean up the temp, so
    the module's never-raises contract holds and a failed rewrite counts as 0.
    """
    tmp_path = path.with_name(path.name + ".inlining.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
        return count
    except OSError as e:
        logger.warning("[REPORT] could not rewrite %s: %s", path, e)
        tmp_path.unlink(missing_ok=True)
        return 0


def inline_report_screenshots(run_dir: str | Path) -> int:
    """Rewrite external screenshot refs in a run's report HTML to data: URIs.

    Returns the number of references inlined (0 is normal for a passed run with no
    screenshots). Idempotent — data: URIs no longer match the pattern. Never
    raises; logs a WARNING when screenshots exist on disk but remain un-inlined.
    """
    run_dir = Path(run_dir)
    total = 0
    for name in _REPORT_HTML_FILES:
        html_path = run_dir / name
        if not html_path.is_file():
            continue
        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("[REPORT] could not read %s: %s", html_path, e)
            continue

        count = 0

        def _replace(m: "re.Match[str]") -> str:
            nonlocal count
            relpath = m.group(3)
            target = _resolve_within(run_dir, relpath)
            if target is None or not target.is_file():
                return m.group(0)  # missing / escaping — leave unchanged
            try:
                raw = target.read_bytes()
            except OSError:
                return m.group(0)
            mime = mimetypes.guess_type(relpath)[0] or "image/png"
            data = base64.b64encode(raw).decode("ascii")
            count += 1
            # Preserve the matched attribute name and (escaped) quote delimiters.
            return f"{m.group(1)}={m.group(2)}data:{mime};base64,{data}{m.group(4)}"

        new_html = _SCREENSHOT_REF_RE.sub(_replace, html)
        if count:
            # _atomic_rewrite returns count once the new HTML has actually
            # landed, 0 if it could not (the complete old file stays in place).
            total += _atomic_rewrite(html_path, new_html, count)

    _warn_on_uninlined(run_dir)
    return total


def _warn_on_uninlined(run_dir: Path) -> None:
    """Drift detector: if a screenshot file on disk still appears by name in the
    served HTML, it was not inlined — likely a Robot format change. Regex-
    independent and idempotency-safe: a successfully inlined file's name is gone
    from the HTML (replaced by base64). Scans the same image extensions the
    matcher accepts (_IMAGE_SUFFIXES) so the signal never lags it."""
    imgs = [
        p for p in run_dir.rglob("*")
        if p.suffix.lower() in _IMAGE_SUFFIXES
        and p.is_file()
        and "dryrun" not in p.relative_to(run_dir).parts
    ]
    if not imgs:
        return
    combined = ""
    for name in _REPORT_HTML_FILES:
        p = run_dir / name
        if p.is_file():
            try:
                combined += p.read_text(encoding="utf-8")
            except OSError:
                pass
    leftover = sorted({p.name for p in imgs if p.name in combined})
    if leftover:
        logger.warning(
            "[REPORT] screenshots present but not inlined in %s (Robot log format "
            "may have changed; update report_inliner._SCREENSHOT_REF_RE): %s",
            run_dir, leftover,
        )
