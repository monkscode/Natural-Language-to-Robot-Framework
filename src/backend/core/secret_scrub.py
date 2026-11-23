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
A changed file is replaced (temp file in the same folder, then ``os.replace``), never opened for writing.

Referenced by: the run-finish and error-reporting paths of the secrets feature (#131).
Depends on: the standard library only.
"""

import base64
import html
import io
import json
import logging
import os
import re
import shutil
import tempfile
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
    r'(\[(?:"(?:[^"\\]|\\.)*"\s*(?:,\s*)?)*\])'
    r"(\);)"
)
# A JS \xNN escape (JSON has none); the pair form skips an escaped backslash before an x.
_JS_HEX = re.compile(r"\\(\\|x([0-9a-fA-F]{2}))")
_TEXT_SUFFIXES = {".txt", ".log"}
# A test writes into its own run folder, so it can plant (or keep growing) a file of any size; the
# scrub never reads more than one byte past this from one file, nor expands its compressed text past it
# (docker_service's output.xml readers use it too). The API process has its own copy of the limit:
# failure_analyzer._MAX_OUTPUT_XML_BYTES.
MAX_REPORT_FILE_BYTES = 20 * 1024 * 1024
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


def _inflate(entry: str, budget: list[int]) -> str:
    """One compressed entry's text, charged to its file's budget. ValueError past the budget (a
    zlib bomb: deflate reaches ~1000:1) or on a cut-short stream; the file then fails closed."""
    inflater = zlib.decompressobj()
    raw = inflater.decompress(base64.b64decode(entry), budget[0] + 1)  # max_length 0 means no limit
    if not inflater.eof or len(raw) > budget[0]:
        raise ValueError("compressed text over the size limit, or cut short")
    budget[0] -= len(raw)
    return raw.decode("utf-8")


def _scrub_strings_array(array_js: str, values: list[str], budget: list[int]) -> str:
    """The array with every entry masked; the input itself when nothing changed."""
    entries = json.loads(_js_unescape_hex(array_js))
    out = []
    for entry in entries:
        if entry.startswith("*"):
            out.append("*" + mask_values(entry[1:], values))
            continue
        text = _inflate(entry, budget)
        masked = mask_values(text, values)
        out.append(entry if masked == text else base64.b64encode(zlib.compress(masked.encode("utf-8"), 9)).decode("ascii"))
    if out == entries:
        return array_js
    # ensure_ascii keeps the page valid JS whatever the text holds; < and > as \x3c / \x3e
    # so a "</script>" in a message cannot end the script block.
    return json.dumps(out, separators=(",", ":")).replace("<", "\\x3c").replace(">", "\\x3e")


def _scrub_html(src: str, values: list[str]) -> str:
    budget = [MAX_REPORT_FILE_BYTES]  # what all of this file's compressed entries may expand to, together
    return _STRINGS.sub(lambda m: m.group(1) + _scrub_strings_array(m.group(2), values, budget) + m.group(3), src)


def _read_bounded(path: Path) -> bytes:
    """The file's bytes from ONE read of at most MAX_REPORT_FILE_BYTES + 1; ValueError when it holds
    more. One read, not a size check first: a container Docker failed to stop can still be writing."""
    with open(path, "rb") as f:
        data = f.read(MAX_REPORT_FILE_BYTES + 1)
    if len(data) > MAX_REPORT_FILE_BYTES:
        raise ValueError("report file over the size limit")
    return data


def _replace_file(path: Path, data: bytes) -> None:
    """Write ``data`` over ``path`` by replacing the file: the runner writes reports as root (644) into a
    folder anyone may write, so the scrub (uid 1000) cannot open them for writing but can replace them.
    The temp name ends in none of the scrubbed suffixes; a failed write leaves no temp file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".scrubtmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(path, tmp)  # mkstemp makes it 0600; other readers of the folder need the old mode
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _scrub_xml(path: Path, values: list[str]) -> bool:
    try:
        tree = ET.ElementTree(ET.fromstring(_read_bounded(path)))
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
        buf = io.BytesIO()
        tree.write(buf, encoding="UTF-8", xml_declaration=True)
        _replace_file(path, buf.getvalue())
    return dirty


def _scrub_text(path: Path, values: list[str], fn=mask_values) -> bool:
    src = _read_bounded(path).decode("utf-8", errors="replace")
    out = fn(src, values)
    if out == src:
        return False
    _replace_file(path, out.encode("utf-8"))
    return True


def scrub_run_dir(run_dir: Path, values: list[str]) -> list[str]:
    """Mask the values in every report file under ``run_dir``; return the changed relative paths.

    A changed file is replaced, not written in place, so a root-owned 644 report in this folder is masked too.
    Images and other binary files are left alone. Nothing is logged but file names. A file that
    cannot be scrubbed does not stop the others: after all are done, ``ScrubError`` names it.
    A symlink, or a path that resolves outside ``run_dir`` (a test container can plant one), is
    skipped: never read, never written, neither changed nor failed. A report file holding more than
    ``MAX_REPORT_FILE_BYTES`` is read no further than one byte past that: ``ScrubError`` names it,
    so the caller deletes it.
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
