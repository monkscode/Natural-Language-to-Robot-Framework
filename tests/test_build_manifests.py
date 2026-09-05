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


def test_the_frontend_gate_checks_out_without_persisting_the_token():
    """actions/checkout writes GITHUB_TOKEN into .git/config unless told not to.

    This job then runs code the pull request itself authored - `npm ci` executes
    lifecycle scripts from the PR's package.json, and `npm test` runs its test
    files - so anything the PR wants can read that token off disk. No step in
    the job needs git authentication afterwards. check-browser-service-release.yml
    already sets this for the same reason.
    """
    steps = _build_images_workflow()["jobs"][FRONTEND_GATE_JOB].get("steps", [])
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert checkouts, f"'{FRONTEND_GATE_JOB}' has no checkout step"
    for s in checkouts:
        assert s.get("with", {}).get("persist-credentials") is False, (
            f"'{FRONTEND_GATE_JOB}' checks out with credential persistence on, "
            "then runs pull-request-authored npm scripts and tests that can read "
            "the token out of .git/config")


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


# ---------------------------------------------------------------------------
# SonarQube's view of the frontend.
#
# src/frontend-react was excluded from Sonar analysis entirely while it had no
# test harness. It has one now, so the exclusion came off and lcov is wired in.
# Both halves are silent when broken, which is why they are guarded here:
#
#   - Sonar does NOT fail when an lcov path is missing. It reports the whole SPA
#     as 0% covered, which fails the new-code gate on every frontend PR for a
#     reason that looks nothing like the cause.
#   - Re-adding src/frontend-react to sonar.exclusions would make the analysis
#     pass by not looking, which is how this started.
# ---------------------------------------------------------------------------

SONAR_PROPS = REPO_ROOT / "sonar-project.properties"
SONAR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sonarqube.yml"


def _sonar_props() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in SONAR_PROPS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_the_frontend_is_not_excluded_from_sonar_analysis():
    excl = _sonar_props().get("sonar.exclusions", "")
    offenders = [
        p for p in excl.split(",")
        if p.strip().startswith("src/frontend-react")
        and not p.strip().startswith(("src/frontend-react/dist", "src/frontend-react/coverage"))
    ]
    assert not offenders, (
        f"sonar.exclusions hides frontend SOURCE from analysis: {offenders}. "
        "Build output and coverage output may be excluded; source may not.")


def test_sonar_reads_the_frontend_lcov():
    props = _sonar_props()
    assert props.get("sonar.javascript.lcov.reportPaths") == "src/frontend-react/coverage/lcov.info", (
        "sonar.javascript.lcov.reportPaths is missing or wrong — Sonar would "
        "silently report the SPA as 0% covered rather than failing")


def test_vendored_shadcn_is_excluded_from_coverage_but_not_from_analysis():
    props = _sonar_props()
    cov_excl = props.get("sonar.coverage.exclusions", "")
    assert "src/frontend-react/src/components/ui/**" in cov_excl, (
        "vendored shadcn/ui must be excluded from COVERAGE (it is generated and "
        "never edited here) — but it must stay in analysis, since it ships")


def test_the_sonar_workflow_produces_the_lcov_before_it_scans():
    import yaml
    wf = yaml.safe_load(SONAR_WORKFLOW.read_text(encoding="utf-8"))
    steps = wf["jobs"]["sonarqube"]["steps"]
    names = [s.get("name") or s.get("uses", "") for s in steps]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "vitest run --coverage" in runs, (
        "sonarqube.yml never generates the frontend lcov it tells Sonar to read")
    scan = next(i for i, s in enumerate(steps)
                if "sonarqube-scan-action" in s.get("uses", ""))
    cov = next(i for i, s in enumerate(steps)
               if "vitest run --coverage" in s.get("run", ""))
    assert cov < scan, (
        f"the frontend coverage step (index {cov}) must run BEFORE the scan "
        f"(index {scan}), or the lcov does not exist when Sonar reads it")
    assert names, "sonarqube job has no steps"


def test_the_sonar_workflow_rewrites_lcov_paths_to_repo_root():
    """vitest writes lcov paths relative to ITS OWN root, so every record reads
    `SF:src/App.tsx`. Sonar resolves those against the repo root, where `src/`
    is the PYTHON backend - measured 2026-09-05, all 41 records unresolvable
    and the SPA reported as 0% covered, with no error from Sonar. The rewrite
    is what makes the lcov usable, and it is one `sed` away from being lost.
    """
    steps = __import__("yaml").safe_load(
        SONAR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["sonarqube"]["steps"]
    runs = chr(10).join(s.get("run", "") for s in steps)
    assert "SF:src/frontend-react/src/" in runs, (
        "sonarqube.yml does not rewrite the lcov paths to repo-root-relative - "
        "Sonar will silently report src/frontend-react as 0% covered")
    assert "exit 1" in runs, (
        "the lcov rewrite is not verified in CI; a partial rewrite would pass "
        "silently and produce a wrong coverage number rather than a failure")
