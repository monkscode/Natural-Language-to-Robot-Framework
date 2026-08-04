"""Pin-parity guards across the build manifests.

Four version pins are shared across files that no single build reads together,
so nothing mechanical has ever held them equal - only comments, and comments
lost once already: the playwright pin forked to 1.57.0 in requirements-bus.txt
against 1.62.0 in the shipped image, so the bench validated an engine
production did not run. It was caught by hand.

Two invariants are guarded here:

  1. playwright: requirements-bus.txt (what the bench installs) == the pin in
     Dockerfile.browser-service (what the image installs). This is the browser
     engine the locator stack resolves against.
  2. the Robot Framework toolchain: Dockerfile.test-runner (what executes the
     generated tests) == the libdoc-builder stage of Dockerfile.fastapi (what
     produces the container's /app/libdocs) == the `version` recorded in the
     committed data/libdocs/*.json (what the assembler is prompted with, both
     locally and on the bench). Generation and execution must describe the same
     library, or the assembler writes keywords against docs the runner does not
     implement.

Referenced by: nothing at runtime - this is a build-manifest contract test.
Depends on: the repo layout only. No imports from src/, no network, no Docker.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    path = REPO_ROOT / name
    assert path.exists(), f"{name} is missing from the repo root"
    return path.read_text(encoding="utf-8")


def _pin(text: str, package: str, source: str) -> str:
    """Extract the single `package==X.Y.Z` pin from a manifest or Dockerfile.

    Matches an optional extras group (`robotframework-browser[bb]==...`) and
    tolerates the surrounding quoting Dockerfiles use for extras.
    """
    pattern = rf'^\s*"?{re.escape(package)}(?:\[[^\]]+\])?==([0-9][^\s"\\;#]*)'
    found = re.findall(pattern, text, flags=re.MULTILINE)
    assert found, f"no pinned `{package}==` found in {source}"
    assert len(set(found)) == 1, f"{package} pinned to conflicting values in {source}: {found}"
    return found[0]


def _libdoc_version(library: str) -> str:
    path = REPO_ROOT / "data" / "libdocs" / f"{library}.json"
    assert path.exists(), f"data/libdocs/{library}.json is missing (it is committed on purpose)"
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def test_playwright_pin_matches_between_bench_venv_and_shipped_image():
    """The bench must resolve locators on the engine the image ships."""
    bench = _pin(_read("requirements-bus.txt"), "playwright", "requirements-bus.txt")
    image = _pin(_read("Dockerfile.browser-service"), "playwright", "Dockerfile.browser-service")
    assert bench == image, (
        f"playwright forked: requirements-bus.txt={bench}, "
        f"Dockerfile.browser-service={image}. Change both files together or not at all."
    )


@pytest.mark.parametrize(
    "package, libdoc",
    [
        ("robotframework", "builtin"),
        ("robotframework-browser", "browser"),
    ],
)
def test_robot_toolchain_pin_matches_runner_libdoc_stage_and_committed_libdocs(package, libdoc):
    """Execution, container libdoc generation and the committed libdocs agree."""
    runner = _pin(_read("Dockerfile.test-runner"), package, "Dockerfile.test-runner")
    builder = _pin(_read("Dockerfile.fastapi"), package, "Dockerfile.fastapi (libdoc stage)")
    committed = _libdoc_version(libdoc)

    assert runner == builder, (
        f"{package} forked between images: Dockerfile.test-runner={runner}, "
        f"Dockerfile.fastapi={builder}. The container would generate keyword docs "
        f"for a library the runner does not execute."
    )
    assert runner == committed, (
        f"{package} pinned to {runner} but data/libdocs/{libdoc}.json records "
        f"{committed}. Regenerate the libdocs from the pinned version: "
        f"tools/generate_libdocs.py, run inside the test-runner image."
    )
