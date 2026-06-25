# Phase 4 — Execution Boundary: acceptance evidence (2026-06-25)

Branch: `feat/execution-boundary` (14 commits off `feat/ms`, tip `b7eb3f7`).
Plan: [docs/superpowers/plans/2026-06-25-phase4-execution-boundary.md](../plans/2026-06-25-phase4-execution-boundary.md).

**Goal of the phase:** stop FastAPI from holding the Docker socket (which is
host-root-equivalent), by moving every container operation behind a dedicated
`runner-exec` executor that builds the container spec itself. After the phase a
compromise of the FastAPI process can no longer mount the host.

**Verdict: PASS on the three security proofs (execution still works, FastAPI is
socket-less, the spawned runner is hardened and its bind cannot be subverted).**
The read-only-rootfs default stays `False` pending a dedicated headless-Chrome
validation (see Step 6).

---

## Environment

- Docker Desktop 29.5.2 (win32). `nlrf-postgres` already healthy.
- `monkscode/nlrf:fastapi-local` rebuilt from this branch (the prior local image
  predated the `runner_exec/` package, and the code is baked into the image —
  not bind-mounted — so a rebuild was required for the executor to import its app).
  The heavy dependency layers were cache hits; only the source copy rebuilt.
- Services brought up for the proof: `postgres` (already up), `runner-exec`,
  `fastapi`. `browser-service` and the LLM were intentionally NOT used — the
  proof drives a dependency-free BuiltIn test so it isolates the execution
  boundary itself.
- Acceptance fixture: [robot_tests/acc-phase4/test.robot](../../../robot_tests/acc-phase4/test.robot)
  — a BuiltIn-only `Log` + `Sleep` test (the `Sleep` widens the inspect window;
  the runner is removed immediately after each run by `run_test_in_container`).

---

## Step 3 — execution still works end-to-end with FastAPI holding no socket

Driven from INSIDE the FastAPI container (which has no socket) via the runner-exec
client, so the call path is exactly the production one:
FastAPI → HTTP → runner-exec (holds the socket) → spawns the runner → result.

```
docker compose exec -T fastapi python -c \
  "from src.backend.runner_exec import client; import json; \
   print(json.dumps(client.execute('acc-phase4','test.robot')))"
```

Result returned to FastAPI through the hop:

```json
{"status": "complete", "message": "Test execution finished: All tests passed.",
 "test_status": "passed", "output_xml_path": "/app/robot_tests/acc-phase4/output.xml",
 "exit_code": 0,
 "result": {"logs": "...Suite: Test\n  Test: Phase4 Boundary Smoke - PASS\nResults: 1 passed, 0 failed..."}}
```

A real `output.xml` was written: `robot_tests/acc-phase4/output.xml` (1841 bytes).
**Execution works with FastAPI holding no socket.**

## Step 4 — FastAPI cannot reach the Docker socket; the executor can

FastAPI boots healthy without the socket (the image entrypoint detects no socket
and skips the permission-fix step, running as `appuser` directly).

```
docker compose exec -T fastapi python -c "import docker; docker.from_env().ping()"
  -> docker.errors.DockerException: Error while fetching server API version:
     ('Connection aborted.', FileNotFoundError(2, 'No such file or directory'))   # the socket is not there

docker compose exec -T runner-exec python -c "import docker; print(docker.from_env().ping())"
  -> True                                                                          # the executor holds it
```

Mount audit (count of `docker.sock` in each container's mounts):

```
nlrf-fastapi      docker.sock mounts: 0     # expected 0
nlrf-runner-exec  docker.sock mounts: 1     # expected 1
```

**The host-root capability is gone from FastAPI; the socket lives only on the
trusted executor.**

## Step 5 — the spawned runner is hardened and its bind cannot be subverted

Captured live from the running `robot-test-acc-phase4` container (before
`run_test_in_container` removed it):

```
Image:          monkscode/nlrf:test-runner-local        # fixed image — caller cannot change it
CapDrop:        ["ALL"]
SecurityOpt:    ["no-new-privileges:true"]
NetworkMode:    bridge                                   # network intentionally KEPT (the runner drives real sites)
ReadonlyRootfs: false                                    # default off — see Step 6
Memory:         2147483648                               # 2 GiB
PidsLimit:      256
Binds:          ["/run/desktop/mnt/host/c/Users/1dhru/Documents/Projects/Natural-Language-to-Robot-Framework/robot_tests:/app/robot_tests:rw"]
docker.sock in runner mounts: 0
```

The only bind is the fixed `robot_tests` directory — no host `/` mount, no socket.
The executor resolved its own `robot_tests` host path and bound exactly that into
the runner (this is the mount-path alignment the final review flagged as
needing a live check — confirmed working).

**`cap_drop: ALL` + `no-new-privileges` + a single fixed bind, all applied by the
executor server-side. A caller supplies only `run_id` + `test_filename`.**

## Step 6 — read-only rootfs decision

The runner currently runs with `ReadonlyRootfs: false` (the shipped default of
`RUNNER_READ_ONLY_ROOTFS`). The plan only allows flipping this default to `True`
after proving headless Chrome/Playwright still passes with a read-only rootfs +
tmpfs — and "do not force read-only at the cost of breaking execution."

That validation needs a real Browser-library run (Chrome, a live site,
browser-service), which is outside this dependency-light proof. **Decision: leave
the default `False`.** The `read_only` + `tmpfs` wiring exists and is unit-tested
(`test_run_test_in_container_applies_hardening` asserts it is gated off by
default); flipping the default is deferred to a dedicated headless-Chrome run.

---

## Blast radius: before vs after (plain English)

- **Before:** the FastAPI process mounted `/var/run/docker.sock` and called
  `containers.run(...)` itself. Anyone who achieved code execution inside FastAPI
  could create a container with any host bind — e.g. mount the host root `/`
  read-write — i.e. take over the host.
- **After:** FastAPI has no socket (Step 4). The only thing it can ask for is
  "run a robot test for `run_id` X" over HTTP. The executor validates `run_id`
  (`[A-Za-z0-9_-]{1,64}`) and `test_filename` (`...\.robot`), and builds the whole
  container spec itself — fixed image, fixed single bind, fixed command, dropped
  caps. There is no field on the wire for an image, a bind path, a command, or a
  privilege flag. The worst an attacker can do is run a robot test in a hardened,
  network-but-no-host-mount container.

## Fail-safe behavior (verified in code + design)

- Executor unreachable during execution → `RunnerExecUnavailable` → caught by the
  existing `except` in `_stream_docker_execution` → execution-error SSE event.
- Executor unreachable during dryrun → `dryrun_status: "unverified"`, code still
  delivered.
- Executor unreachable for `/docker-status` → `docker_available: false`.
- Repeated connect failures trip the client circuit breaker (5 failures, 30s
  cooldown), so a dead executor fast-fails instead of hanging. HTTP errors and a
  slow-but-alive test do NOT trip it. A malformed 200 body now surfaces as
  `RunnerExecUnavailable` rather than a raw `JSONDecodeError` (fixed in `b7eb3f7`).
- A stray/forgotten direct Docker call left in FastAPI would now raise (no socket)
  rather than silently escalating to host root.

## Final whole-branch review

A read-only review over `6bef415..89dc116` (opus) returned **0 Critical, 0
blocking Important**, with the security invariant proven from five angles
(blast radius, three fail-safe paths, consumer grep, `test_live_docker.py` status,
compose correctness). The non-blocking nits it triaged were then cleared in commit
`b7eb3f7` (including the client JSON-contract fix). Gated unit suite at the tip:
**1627 passed, 1 skipped, 0 failed.**

## Honest caveats / deferred

1. Read-only rootfs default stays `False` pending a headless-Chrome-under-read-only
   run (Step 6).
2. The "normal flow" generate step (LLM + browser-service exploration) was not
   exercised here; Phase 4 did not change generation — it only redirected the
   same execute/dryrun calls onto the hop, which this proof exercises directly.
3. `tests/test_integration/test_live_docker.py` still imports the docker_service
   primitives directly. Those primitives still exist (the executor uses them), so
   the test is not broken; it now exercises the executor's internals rather than
   the FastAPI→executor HTTP boundary. A follow-up could add an HTTP-hop
   integration test. Not edited by this phase.
