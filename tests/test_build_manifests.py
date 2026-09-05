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


# ---------------------------------------------------------------------------
# The publish gate.
#
# build-images.yml and sonarqube.yml are both triggered by a push to develop,
# and nothing connected them: the images that overwrite `-develop` — the tag the
# README tells every user to pull — were published whether or not the suite that
# runs alongside them passed. The two workflows simply raced. This holds the
# gate in place, because a `needs:` line is exactly the kind of thing that gets
# dropped while making an unrelated job faster.
# ---------------------------------------------------------------------------

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-images.yml"

# Every job that produces an image. base-browser is included: it is not pushed
# under a tag users pull, but browser-service and test-runner are built FROM it.
IMAGE_JOBS = [
    "build-base-browser",
    "build-fastapi",
    "build-frontend",
    "build-browser-service",
    "build-test-runner",
]

GATE_JOB = "test"


def _build_images_workflow() -> dict:
    import yaml
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_publish_gate_job_exists_and_runs_the_suite():
    wf = _build_images_workflow()
    assert GATE_JOB in wf["jobs"], (
        f"build-images.yml has no '{GATE_JOB}' job — nothing stops a red suite "
        "from publishing images")
    steps = wf["jobs"][GATE_JOB].get("steps", [])
    run_text = "\n".join(s.get("run", "") for s in steps)
    assert "pytest" in run_text, f"the '{GATE_JOB}' job does not run pytest"


@pytest.mark.parametrize("job", IMAGE_JOBS)
def test_every_image_build_waits_for_the_suite(job):
    wf = _build_images_workflow()
    assert job in wf["jobs"], f"build-images.yml has no '{job}' job"
    needs = wf["jobs"][job].get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert GATE_JOB in needs, (
        f"'{job}' does not depend on '{GATE_JOB}' — it can publish an image "
        "built from code the suite rejected")


# ---------------------------------------------------------------------------
# The FRONTEND publish gate.
#
# Until test-frontend existed, no workflow ran a single frontend test. Both
# pytest lanes are backend-only, and sonar-project.properties excludes
# src/frontend-react outright, so the only thing standing between a broken
# React change and a published frontend image was `tsc -b` inside
# Dockerfile.frontend. That catches a type error and nothing else.
#
# Guarded for the same reason as the backend gate above: a `needs:` line and a
# test command are exactly what gets dropped while making a job faster.
# ---------------------------------------------------------------------------

FRONTEND_GATE_JOB = "test-frontend"


def test_the_frontend_publish_gate_exists_and_runs_the_suite():
    wf = _build_images_workflow()
    assert FRONTEND_GATE_JOB in wf["jobs"], (
        f"build-images.yml has no '{FRONTEND_GATE_JOB}' job — nothing runs the "
        "frontend suite before its image is published")
    steps = wf["jobs"][FRONTEND_GATE_JOB].get("steps", [])
    run_text = "\n".join(s.get("run", "") for s in steps)
    # `npm test` is package.json's `vitest run`; accept either spelling so the
    # job may call vitest directly without silently losing the gate.
    assert "npm test" in run_text or "vitest" in run_text, (
        f"the '{FRONTEND_GATE_JOB}' job does not run the frontend suite")
    assert "tsc" in run_text, (
        f"the '{FRONTEND_GATE_JOB}' job does not typecheck — tsc is the only "
        "check that covers the TSX the suite does not reach")


def test_the_frontend_image_waits_for_the_frontend_suite():
    wf = _build_images_workflow()
    needs = wf["jobs"]["build-frontend"].get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert FRONTEND_GATE_JOB in needs, (
        f"'build-frontend' does not depend on '{FRONTEND_GATE_JOB}' — it can "
        "publish an image built from React code the frontend suite rejected")


def test_the_frontend_gate_runs_the_same_node_major_as_the_image_build():
    # A suite that passes on a different Node major than the image builds on is
    # not a gate on the thing being shipped. Dockerfile.frontend's build stage
    # is the authority.
    import re
    wf = _build_images_workflow()
    dockerfile = (REPO_ROOT / "Dockerfile.frontend").read_text(encoding="utf-8")
    m = re.search(r"^FROM\s+node:(\d+)", dockerfile, re.MULTILINE)
    assert m, "Dockerfile.frontend has no `FROM node:<major>` build stage"
    image_major = m.group(1)

    steps = wf["jobs"][FRONTEND_GATE_JOB].get("steps", [])
    versions = [
        str(s["with"]["node-version"])
        for s in steps
        if s.get("uses", "").startswith("actions/setup-node") and "node-version" in s.get("with", {})
    ]
    assert versions, f"'{FRONTEND_GATE_JOB}' does not pin a Node version"
    assert all(v.split(".")[0] == image_major for v in versions), (
        f"'{FRONTEND_GATE_JOB}' runs Node {versions} but Dockerfile.frontend "
        f"builds on node:{image_major}")
