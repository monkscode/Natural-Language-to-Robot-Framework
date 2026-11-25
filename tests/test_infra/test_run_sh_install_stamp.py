"""run.sh's backend install stamp covers every file the install reads.

The defect (CodeRabbit on PR #203, 2026-11-25): run.sh installs the backend venv
with `--overrides uv-overrides.txt -r "$BACKEND_REQ"`, but its stamp hashed only
the requirements file, so a change to uv-overrides.txt alone left an existing
venv on its old packages.

The test runs the REAL `_req_hash()` and `BACKEND_REQ=` / `BACKEND_HASH=` lines,
cut from run.sh's text, under bash in a temp tree — never a re-typed copy.

Referenced by: none (leaf test module).
Depends on: run.sh (`_req_hash`, `BACKEND_REQ`, `BACKEND_HASH`), a bash on PATH.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parents[2] / "run.sh"


def _stamp_lines() -> str:
    text = RUN_SH.read_text(encoding="utf-8")
    func = re.search(r"^_req_hash\(\) \{\n.*?^\}\n", text, re.M | re.S)
    req = re.search(r"^BACKEND_REQ=.*$", text, re.M)
    hashed = re.search(r"^BACKEND_HASH=.*$", text, re.M)
    assert func and req and hashed, "run.sh no longer holds the stamp lines"
    return f"{func.group(0)}{req.group(0)}\n{hashed.group(0)}\nprintf %s \"$BACKEND_HASH\"\n"


def _backend_hash(bash: str, root: Path) -> str:
    out = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", _stamp_lines()],
        cwd=root, capture_output=True, text=True, timeout=30, check=True,
    )
    assert out.stdout, out.stderr
    return out.stdout


def test_backend_stamp_changes_with_requirements_or_overrides(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH")
    req = tmp_path / "src" / "backend" / "requirements.txt"
    req.parent.mkdir(parents=True)
    overrides = tmp_path / "uv-overrides.txt"
    req.write_text("crewai==1.15.44\n", encoding="utf-8")
    overrides.write_text("lancedb; sys_platform == 'never'\n", encoding="utf-8")

    first = _backend_hash(bash, tmp_path)
    assert _backend_hash(bash, tmp_path) == first

    overrides.write_text("pyarrow; sys_platform == 'never'\n", encoding="utf-8")
    after_overrides = _backend_hash(bash, tmp_path)
    assert after_overrides != first

    req.write_text("crewai==1.15.45\n", encoding="utf-8")
    assert _backend_hash(bash, tmp_path) != after_overrides
