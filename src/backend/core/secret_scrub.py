"""Mask org-secret values in a finished run's report folder and in error texts.

A generated test types a secret from an environment variable, but Robot Framework and
Playwright still echo the typed value into ``output.xml``, ``log.html``, ``report.html`` and
the text logs. After every run the value is replaced with ``***`` in each of its raw,
HTML/XML-escaped and JSON-escaped forms, longest value first.

``log.html`` / ``report.html`` hold the messages inside ``window.output["strings"]`` arrays,
each entry either ``*<raw text>`` or base64(zlib(text)) (RF 7.4.2 ``robot/utils/compress.py``),
so a plain text replace cannot reach the compressed ones. Only those array entries are
touched; the page's markup and script are never edited, so a value like ``status`` cannot
break the page. The same holds for XML: only parsed text and attribute values change.

Referenced by: the run-finish and error-reporting paths of the secrets feature (#131).
Depends on: the standard library only.
"""

import base64
import html
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

MASK = "***"


class ScrubError(Exception):
    """Some files of a run folder could not be scrubbed; ``files`` names them (never content)."""

    def __init__(self, files: list[str]):
        self.files = files
        super().__init__("could not scrub: " + ", ".join(files))


# One concat(...) array of JS string literals; string-literal aware so a "]);" inside a
# message cannot end the match early.
_STRINGS = re.compile(
    r'(window\.output\["strings"\]\s*=\s*window\.output\["strings"\]\.concat\()'
    r'(\[(?:"(?:[^"\\]|\\.)*"\s*,?\s*)*\])'
    r"(\);)"
)
# A JS \xNN escape (JSON has none); the pair form skips an escaped backslash before an x.
_JS_HEX = re.compile(r"\\(\\|x([0-9a-fA-F]{2}))")
_TEXT_SUFFIXES = {".txt", ".log"}
# RF-generated output.xml attributes: masking them (e.g. a value "FAIL" or "1234") breaks result parsing.
_XML_BOOKKEEPING = frozenset({
    "status", "start", "elapsed", "time", "level", "id", "line",
    "generated", "generator", "schemaversion", "rpa", "starttime", "endtime",
    # read by the failure analysers: keyword type / owner library, <stat> counts, <msg html>, flavor
    "type", "owner", "pass", "fail", "skip", "html", "flavor",
})


def is_inside(path: Path, root_real: str) -> bool:
    """True when ``path`` resolves under ``root_real`` (same test as artifact_store._safe_subpath)."""
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


def _forms(value: str) -> set[str]:
    return {
        value,
        html.escape(value, quote=False),
        html.escape(value, quote=True),
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"),
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value)[1:-1],
    }


def mask_values(text: str, values: Iterable[str]) -> str:
    """Replace every value (raw, escaped, JSON-escaped; longest first) with ``***``."""
    forms = {f for v in values if v for f in _forms(v)}
    if not forms:
        return text
    pattern = re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)))
    return pattern.sub(MASK, text)


def _js_unescape_hex(array_js: str) -> str:
    return _JS_HEX.sub(lambda m: m.group(0) if m.group(2) is None else "\\u00" + m.group(2), array_js)


def _scrub_strings_array(array_js: str, values: list[str]) -> str:
    """The array with every entry masked; the input itself when nothing changed."""
    entries = json.loads(_js_unescape_hex(array_js))
    out = []
    for entry in entries:
        if entry.startswith("*"):
            out.append("*" + mask_values(entry[1:], values))
            continue
        text = zlib.decompress(base64.b64decode(entry)).decode("utf-8")
        masked = mask_values(text, values)
        out.append(entry if masked == text else base64.b64encode(zlib.compress(masked.encode("utf-8"), 9)).decode("ascii"))
    if out == entries:
        return array_js
    # ensure_ascii keeps the page valid JS whatever the text holds; < and > as \x3c / \x3e
    # so a "</script>" in a message cannot end the script block.
    return json.dumps(out, separators=(",", ":")).replace("<", "\\x3c").replace(">", "\\x3e")


def _scrub_html(src: str, values: list[str]) -> str:
    return _STRINGS.sub(lambda m: m.group(1) + _scrub_strings_array(m.group(2), values) + m.group(3), src)


def _scrub_xml(path: Path, values: list[str]) -> bool:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return _scrub_text(path, values)
    dirty = False
    for el in tree.iter():
        for attr in ("text", "tail"):
            old = getattr(el, attr)
            if old and (new := mask_values(old, values)) != old:
                setattr(el, attr, new)
                dirty = True
        for key, old in el.attrib.items():
            if key in _XML_BOOKKEEPING:
                continue
            if (new := mask_values(old, values)) != old:
                el.attrib[key] = new
                dirty = True
    if dirty:
        tree.write(path, encoding="UTF-8", xml_declaration=True)
    return dirty


def _scrub_text(path: Path, values: list[str], fn=mask_values) -> bool:
    src = path.read_bytes().decode("utf-8", errors="replace")
    out = fn(src, values)
    if out == src:
        return False
    path.write_bytes(out.encode("utf-8"))
    return True


def scrub_run_dir(run_dir: Path, values: list[str]) -> list[str]:
    """Mask the values in every report file under ``run_dir``; return the changed relative paths.

    Images and other binary files are left alone. Nothing is logged but file names. A file that
    cannot be scrubbed does not stop the others: after all are done, ``ScrubError`` names it.
    A symlink, or a path that resolves outside ``run_dir`` (a test container can plant one), is
    skipped: never read, never written, neither changed nor failed.
    """
    values = [v for v in values if v]
    if not values:
        return []
    root = Path(run_dir)
    root_real = os.path.realpath(root)
    changed, failed = [], []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not is_inside(path, root_real) or not path.is_file():
            continue
        suffix = path.suffix.lower()
        name = path.relative_to(root).as_posix()
        try:
            if suffix == ".xml":
                did = _scrub_xml(path, values)
            elif suffix == ".html":
                did = _scrub_text(path, values, _scrub_html)
            elif suffix in _TEXT_SUFFIXES:
                did = _scrub_text(path, values)
            else:
                continue
        except Exception:  # noqa: BLE001 — never keep the error: its text may quote file content
            failed.append(name)
            continue
        if did:
            changed.append(name)
    logger.info("secret scrub changed %d file(s): %s", len(changed), mask_values(", ".join(changed), values))
    if failed:
        raise ScrubError(failed)
    return changed
