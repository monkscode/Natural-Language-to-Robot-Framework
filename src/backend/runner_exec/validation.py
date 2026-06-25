"""Strict validators for caller-supplied identifiers crossing the execution hop.

run_id and test_filename influence the container mount path, so they are the one
caller-controlled input the executor must constrain. Anything outside a safe path
component (separators, traversal, whitespace, over-length) is rejected before use.

Referenced by: src/backend/runner_exec/app.py.
Depends on: (stdlib only).
"""
import re

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.robot$")


def safe_run_id(value: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value) or value in (".", ".."):
        raise ValueError(f"unsafe run_id: {value!r}")
    return value


def safe_test_filename(value: str) -> str:
    if not isinstance(value, str) or not _FILENAME_RE.fullmatch(value):
        raise ValueError(f"unsafe test_filename: {value!r}")
    return value
