"""Deterministic `robot --dryrun` validation gate + bounded Assembler repair loop.

Replaces the removed CrewAI LLM validator agent (Q1 root cause: code was declared
"valid" without ever being checked against the Robot Framework engine) with:

  1. run_dryrun_in_container — runs `robot --dryrun` on the generated code in a
     one-shot test-runner container, parses output.xml, and returns pass/fail plus
     the exact RF error text. Raises only on infrastructure failure (caller degrades).
  2. repair_robot_code — a top-level single-agent Assembler mini-crew that fixes
     ONLY the flagged keyword/syntax (conservative; §8.1). Returns the repaired task
     output AND a usage dict so the repair's LLM cost can be folded into the
     dashboard's crewai_* totals (§5 / decision 5).
  3. validate_and_repair — orchestrates the gate + a bounded repair loop. It is a
     SOFT gate: Docker unavailable / any error → dryrun_status:'unverified' and the
     code is STILL delivered. It NEVER raises to its caller and NEVER touches the
     learning system.
  4. extract_and_normalize_robot_code — the shared CrewAI-output extraction +
     normalization pipeline used by BOTH the main generation path and the repair
     path, so they normalize identically.

`robot --dryrun` is compile-time only: it catches unknown keywords, wrong argument
counts, syntax/parse errors, and library/resource import failures. It does NOT catch
undefined variables, invalid expressions, wrong locators, or runtime values — real
Docker execution remains the authoritative semantic gate, and the learning system is
fed ONLY by the real run (never by dryrun).

Referenced by: src/backend/services/workflow_service.py (run_agentic_workflow gate)
Depends on: src/backend/services/docker_service (container plumbing + mount resolution),
            src/backend/crew_ai/agents.RobotAgents + tasks.RobotTasks (repair crew),
            src/backend/crew_ai/robot_code_normalizer.normalize_robot_code,
            src/backend/core/workflow_metrics.calculate_crewai_cost (repair cost shape),
            src/backend/core/config.settings (DRYRUN_ENABLED / MAX_DRYRUN_FIXES / DRYRUN_TIMEOUT)
"""

import json
import logging
import os
import re
import xml.etree.ElementTree as ET

import docker
import requests as _requests

from src.backend.core.config import settings
from src.backend.core.workflow_metrics import calculate_crewai_cost
from src.backend.crew_ai.robot_code_normalizer import normalize_robot_code
from src.backend.core.artifact_store import get_artifact_store
from src.backend.services.docker_service import (
    IMAGE_TAG,
    build_image,
    get_docker_client,
    normalize_docker_mount_source,
    resolve_host_robot_tests_dir,
)

logger = logging.getLogger(__name__)

# Repair usage dict keys mirror calculate_crewai_cost() output so Step 5 can fold
# the repair crew's cost into crewai_metrics with a simple per-key add.
_USAGE_KEYS = ("llm_calls", "cost", "tokens", "prompt_tokens", "completion_tokens")

# Cap the error text fed into the repair prompt so a pathological output.xml can't
# bloat the LLM request. RF dryrun errors are short ("Did you mean: Browser.Click").
_MAX_ERROR_TEXT_CHARS = 4000


# ---------------------------------------------------------------------------
# Shared code extraction + normalization (main path AND repair path)
# ---------------------------------------------------------------------------

def extract_and_normalize_robot_code(task_output) -> str:
    """Extract robot code from a CrewAI task output and apply the full
    normalization pipeline.

    Refactored verbatim from the inline block that used to live in
    workflow_service.run_agentic_workflow so both the main generation path and the
    dryrun repair path normalize identically:
      Strategy 1 — output.pydantic.code (AssemblyOutput)
      Strategy 2 — output.json_dict['code']
      Strategy 3 — raw JSON {"code": "..."} → code, else raw output as-is
      then: escaped-newline/tab fix → normalize_robot_code → Settings-block trim →
            trailing-empty-line strip → trailing-JSON-artifact strip.
    """
    # Strategy 1: Pydantic output (output_pydantic=AssemblyOutput)
    if hasattr(task_output, 'pydantic') and task_output.pydantic:
        robot_code = task_output.pydantic.code
        logger.info("✅ Extracted robot code from output.pydantic.code (AssemblyOutput)")
    # Strategy 2: json_dict output
    elif hasattr(task_output, 'json_dict') and task_output.json_dict and 'code' in task_output.json_dict:
        robot_code = task_output.json_dict['code']
        logger.info("✅ Extracted robot code from output.json_dict['code']")
    # Strategy 3: parse raw output as JSON ({"code": "..."}) else use raw as-is
    else:
        raw_output = getattr(task_output, "raw", "") or ""
        # Guard: normalize non-string raw outputs (e.g. dict/list) to JSON string
        if not isinstance(raw_output, str):
            raw_output = json.dumps(raw_output)
        try:
            parsed_json = json.loads(raw_output)
            if isinstance(parsed_json, dict) and 'code' in parsed_json:
                robot_code = parsed_json['code']
                logger.info("✅ Extracted robot code from parsed JSON in raw output")
            else:
                robot_code = raw_output
                logger.info("✅ Using raw output as robot code (legacy format)")
        except (json.JSONDecodeError, TypeError):
            robot_code = raw_output
            logger.info("✅ Using raw output as robot code (not JSON)")

    # Normalize escaped newlines/tabs to actual characters (LLMs emit literal \n)
    if '\\n' in robot_code or '\\t' in robot_code or '\\r' in robot_code:
        robot_code = robot_code.replace('\\r\\n', '\n')  # Windows line endings
        robot_code = robot_code.replace('\\n', '\n')
        robot_code = robot_code.replace('\\t', '\t')
        robot_code = robot_code.replace('\\r', '\r')
        logger.info("✅ Normalized escaped newlines/tabs to actual characters")

    # Prefix bare CSS selectors (#id, .class) with `css=` so RF does not parse them
    # as comments (see robot_code_normalizer for full rationale).
    robot_code = normalize_robot_code(robot_code)

    # Step 1: Handle multiple Settings blocks (LLM might output code multiple times)
    settings_matches = list(re.finditer(
        r'\*\*\*\s+Settings\s+\*\*\*', robot_code, re.IGNORECASE))

    if len(settings_matches) > 1:
        logger.info(
            f"✅ Found {len(settings_matches)} Settings blocks, using the last one")
        robot_code = robot_code[settings_matches[-1].start():]
    elif len(settings_matches) == 1:
        robot_code = robot_code[settings_matches[0].start():]
        logger.info("✅ Found Settings block, extracted code from there")
    else:
        logger.warning("⚠️ No *** Settings *** block found in code!")

        variables_match = re.search(
            r'\*\*\*\s+Variables\s+\*\*\*', robot_code, re.IGNORECASE)
        test_cases_match = re.search(
            r'\*\*\*\s+Test\s+Cases\s+\*\*\*', robot_code, re.IGNORECASE)

        if variables_match:
            robot_code = robot_code[variables_match.start():]
            logger.warning(
                "⚠️ Starting from *** Variables *** instead (Settings missing!)")
        elif test_cases_match:
            robot_code = robot_code[test_cases_match.start():]
            logger.warning(
                "⚠️ Starting from *** Test Cases *** instead (Settings and Variables missing!)")
        else:
            logger.error("❌ No Robot Framework sections found in output!")

    # Step 2: Final cleanup - drop trailing empty lines
    lines = robot_code.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    robot_code = '\n'.join(lines).strip()

    # Step 3: Strip trailing JSON artifacts that may leak from LLM output
    json_trailing_patterns = [
        '"}',  # JSON closing brace with quote
    ]
    for pattern in json_trailing_patterns:
        if robot_code.endswith(pattern):
            robot_code = robot_code[:-len(pattern)].strip()
            logger.info(f"✅ Stripped trailing JSON artifact: {pattern}")

    return robot_code


# ---------------------------------------------------------------------------
# Dryrun container + output.xml parser
# ---------------------------------------------------------------------------

def _force_remove_stale_container(client, container_name: str) -> None:
    """Force-remove any stale container with this name (mirrors docker_service)."""
    try:
        existing = client.containers.get(container_name)
        logger.warning("🔬 DRYRUN: removing stale container %s", container_name)
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.error("🔬 DRYRUN: failed to remove stale container %s: %s", container_name, e)


def _remove_container_obj(container) -> None:
    """Best-effort force-remove of a created container object (NotFound-safe)."""
    if container is None:
        return
    try:
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        logger.warning("🔬 DRYRUN: container cleanup failed (non-blocking): %s", e)


def _parse_dryrun_output_xml(output_xml_path: str, exit_code: int) -> tuple[bool, str]:
    """Parse a `robot --dryrun` output.xml. Returns (passed, error_text).

    PARSER SPEC (verified empirically — S1 spike, §7): a dryrun is FAILED if ANY of:
      (1) exit_code != 0                       (unknown keyword / wrong arg count → exit 1)
      (2) statistics/total/stat fail > 0       (same per-test failures)
      (3) <errors> contains a <msg level="ERROR">
          ^ REQUIRED: a failed `Library` import does NOT change the exit code or the
            fail count (stays 0 / PASS) — it appears ONLY in <errors>. Exit code and
            statistics alone would miss it.
    Error text is collected from BOTH:
      - every <msg level="FAIL"> / <status status="FAIL"> under failing <kw>/<test>
        nodes (unknown keyword, wrong arg count — includes RF's "Did you mean:
        Browser.Click" suggestion), AND
      - every <msg level="ERROR"> in the <errors> section (import/parse failures).
    Raises RuntimeError if output.xml is unreadable (caller treats as infra → degrade).
    """
    try:
        tree = ET.parse(output_xml_path)
        root = tree.getroot()
    except Exception as e:
        raise RuntimeError(f"could not parse dryrun output.xml: {e}")

    # (2) statistics fail count — reuse docker_service.run_test_in_container pattern
    fail_count = 0
    stat = root.find('.//statistics/total/stat')
    if stat is not None:
        try:
            fail_count = int(stat.get('fail', '0'))
        except (TypeError, ValueError):
            fail_count = 0

    error_msgs: list[str] = []

    # FAIL messages on keyword/test nodes (unknown keyword, wrong arg count, syntax)
    for msg in root.iter('msg'):
        if msg.get('level') == 'FAIL':
            text = (msg.text or '').strip()
            if text:
                error_msgs.append(text)
    for status in root.iter('status'):
        if status.get('status') == 'FAIL':
            text = (status.text or '').strip()
            if text:
                error_msgs.append(text)

    # (3) <errors> section — ERROR-level messages (import/parse failures, exit 0)
    errors_section_has_error = False
    errors_node = root.find('errors')
    if errors_node is None:
        errors_node = root.find('.//errors')
    if errors_node is not None:
        for msg in errors_node.iter('msg'):
            if msg.get('level') == 'ERROR':
                errors_section_has_error = True
                text = (msg.text or '').strip()
                if text:
                    error_msgs.append(text)

    failed = (exit_code != 0) or (fail_count > 0) or errors_section_has_error

    # Dedup preserving order; cap length to keep the repair prompt tight
    seen: set[str] = set()
    unique: list[str] = []
    for m in error_msgs:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    errors_text = "\n".join(unique)
    if len(errors_text) > _MAX_ERROR_TEXT_CHARS:
        errors_text = errors_text[:_MAX_ERROR_TEXT_CHARS] + "\n... (truncated)"

    return (not failed), errors_text


def run_dryrun_in_container(client, run_id: str, robot_code: str) -> dict:
    """Run `robot --dryrun` on robot_code in a one-shot test-runner container.

    Writes the test to robot_tests/{run_id}/dryrun/dryrun.robot and runs
        robot --dryrun --outputdir /app/robot_tests/{run_id}/dryrun <file>
    in container robot-test-dryrun-{run_id}. Artifacts are isolated to the
    {run_id}/dryrun/ subdirectory so they NEVER clobber the real run's
    {run_id}/output.xml that learning + /reports depend on (learn-8). Any stale
    same-named container is force-removed first.

    Returns {"passed": bool, "errors": str, "exit_code": int}.
    Raises RuntimeError ONLY on infrastructure failure (timeout, container never
    produced output.xml, unreadable output.xml) — the caller wraps this for
    graceful degrade to dryrun_status:'unverified'.
    """
    container_name = f"robot-test-dryrun-{run_id}"
    dryrun_filename = "dryrun.robot"
    dryrun_dir = str(get_artifact_store().run_dir(run_id) / "dryrun")
    os.makedirs(dryrun_dir, exist_ok=True)
    dryrun_filepath = os.path.join(dryrun_dir, dryrun_filename)
    output_xml_path = os.path.join(dryrun_dir, "output.xml")

    # Write the test file (FastAPI-container-visible path)
    with open(dryrun_filepath, "w", encoding="utf-8") as f:
        f.write(robot_code)
    logger.info("🔬 DRYRUN: wrote test to %s", dryrun_filepath)

    # Clear any stale output.xml so a missing file unambiguously means
    # "the container produced nothing" (infra failure), not "leftover from before".
    if os.path.exists(output_xml_path):
        try:
            os.remove(output_xml_path)
        except OSError as e:
            logger.warning("🔬 DRYRUN: could not remove stale output.xml: %s", e)

    robot_command = [
        "robot", "--dryrun",
        "--outputdir", f"/app/robot_tests/{run_id}/dryrun",
        f"/app/robot_tests/{run_id}/dryrun/{dryrun_filename}",
    ]
    host_robot_tests_dir = resolve_host_robot_tests_dir(client)
    normalized_host_robot_tests_dir = normalize_docker_mount_source(host_robot_tests_dir)

    container_config = {
        "image": IMAGE_TAG,
        "command": robot_command,
        "volumes": {normalized_host_robot_tests_dir: {'bind': '/app/robot_tests', 'mode': 'rw'}},
        "working_dir": "/app",
        "detach": True,
        "auto_remove": False,
        "name": container_name,
        "network_mode": "none",
        "mem_limit": "512m",
        "pids_limit": 128,
    }

    _force_remove_stale_container(client, container_name)

    container = None
    startup_logs = ""
    try:
        container = client.containers.run(**container_config)
        logger.info("🔬 DRYRUN: started container %s (id=%s)", container_name, container.id)
        try:
            wait_result = container.wait(timeout=settings.DRYRUN_TIMEOUT)
            exit_code = wait_result['StatusCode']
        except _requests.exceptions.ReadTimeout:
            raise RuntimeError(
                f"dryrun container timed out after {settings.DRYRUN_TIMEOUT}s"
            )

        # Capture stdout/stderr for diagnostics via the Docker API (NOT
        # container.logs() — same anti-pattern guard as docker_service).
        try:
            raw_logs = client.api.logs(container.id, stdout=True, stderr=True, tail=200)
            if isinstance(raw_logs, (bytes, bytearray)):
                startup_logs = raw_logs.decode('utf-8', errors='replace').strip()
            else:
                startup_logs = str(raw_logs).strip()
        except Exception as e:
            logger.warning("🔬 DRYRUN: could not capture container logs: %s", e)
    finally:
        _remove_container_obj(container)

    # INFRA vs CODE failure: no output.xml → the container never ran RF → infra
    # failure (mirror run_test_in_container's output.xml-existence check). The
    # caller graceful-degrades to 'unverified' (this is NOT a repair trigger).
    if not os.path.exists(output_xml_path):
        raise RuntimeError(
            "dryrun produced no output.xml — treating as infrastructure failure "
            f"(exit_code={exit_code}). Container stdout/stderr (tail): {startup_logs[:500]}"
        )

    passed, errors = _parse_dryrun_output_xml(output_xml_path, exit_code)
    logger.info("🔬 DRYRUN: run_id=%s exit=%s passed=%s", run_id, exit_code, passed)
    return {"passed": passed, "errors": errors, "exit_code": exit_code}


# ---------------------------------------------------------------------------
# Repair mini-crew (fresh, top-level — NOT in-crew delegation)
# ---------------------------------------------------------------------------

def _repair_usage_dict(repair_crew, model_name: str) -> dict:
    """Convert a repair crew's usage into the calculate_crewai_cost() shape.

    Returns {"llm_calls","cost","tokens","prompt_tokens","completion_tokens"} so
    Step 5 folds it into crewai_metrics by a simple per-key add. Returns {} on any
    error (cost tracking must never break the gate).
    """
    try:
        usage_obj = repair_crew.calculate_usage_metrics()
        usage_dict = {
            'total_tokens': usage_obj.total_tokens,
            'prompt_tokens': usage_obj.prompt_tokens,
            'completion_tokens': usage_obj.completion_tokens,
            'successful_requests': usage_obj.successful_requests,
            'total_cost': getattr(usage_obj, 'total_cost', None),
        }
        return calculate_crewai_cost(usage_dict, model_name=model_name)
    except Exception as e:
        logger.warning("🔬 DRYRUN: could not compute repair usage (non-blocking): %s", e)
        return {}


def repair_robot_code(run_id, robot_code, dryrun_errors, model_provider,
                      model_name, library_type=None) -> tuple:
    """Top-level Assembler repair mini-crew. Returns (task_output, usage_dict).

    Builds FRESH RobotAgents + RobotTasks (never reuses the main-crew instances —
    §2.4). The single assembler agent has allow_delegation=False; the crew has NO
    step/task callbacks, output_log_file=None, and is NOT registered in
    progress_events — its bus events route nowhere and are safely dropped (§8.5).
    Library context is resolved from settings.ROBOT_LIBRARY when library_type is
    None so the repair agent knows Browser-vs-Selenium keywords (§8.2). The repaired
    code is read DIRECTLY from crew.tasks[0].output (no in-crew delegation).

    Pure repair step — pushes no progress itself; the caller (validate_and_repair)
    emits the repair-phase progress event before invoking this.
    """
    from crewai import Crew, Process
    from src.backend.crew_ai.agents import RobotAgents
    from src.backend.crew_ai.tasks import RobotTasks
    from src.backend.crew_ai.library_context import get_library_context

    if library_type is None:
        library_type = settings.ROBOT_LIBRARY
    library_context = get_library_context(library_type)

    # FRESH instances — own CleanedLLMWrapper + monitor, so the MAIN crew's
    # calculate_usage_metrics()/llm_monitor never see these repair calls (no
    # double-count; §5). No keyword_search tool: the assembler relies on
    # library_context for keyword knowledge (§8.2).
    agents = RobotAgents(model_provider, model_name, library_context)
    tasks = RobotTasks(library_context)
    assembler = agents.code_assembler_agent()
    # Bound the repair agent's internal iterations. Also keeps MAX_AGENT_ITERATIONS
    # a live consumer now that the validator agent (its previous consumer) is gone.
    assembler.max_iter = settings.MAX_AGENT_ITERATIONS
    fix_task = tasks.repair_code_task(assembler, robot_code, dryrun_errors)

    repair_crew = Crew(
        agents=[assembler],
        tasks=[fix_task],
        process=Process.sequential,
        verbose=True,
        output_log_file=None,  # §8.5 — keep repair noise out of logs/crewai.log
        embedder=None,
        # No step_callback/task_callback and NOT registered in progress_events:
        # a single-agent sequential crew gets no delegation tools (crewai 1.8.1
        # crew.py:1310 requires len(agents) > 1), and its fix_task.id routes
        # nowhere in progress_events, so its bus events are safely dropped (§8.5).
    )

    repair_crew.kickoff()
    task_output = repair_crew.tasks[0].output
    usage = _repair_usage_dict(repair_crew, model_name)
    return task_output, usage


# ---------------------------------------------------------------------------
# Orchestration — the soft gate (generation path only; O2 dropped execute-only)
# ---------------------------------------------------------------------------

def _accumulate_usage(acc: dict, add: dict) -> None:
    """Add per-key repair usage (calculate_crewai_cost shape) into the accumulator."""
    if not add:
        return
    for k in _USAGE_KEYS:
        if k in add:
            acc[k] = acc.get(k, 0) + add[k]


def _push_progress(progress_queue, message: str, progress: int | None = None) -> None:
    """Push a gate progress event directly onto the SSE queue (guarded).

    Direct queue.put — NOT progress_events._push_if_forward — because run_crew has
    already called unregister_workflow() by the time the gate runs, so the event-bus
    routing maps are gone (prog-4). Safe when progress_queue is None.

    When progress is None the event carries no 'progress' key, so the frontend
    progress bar HOLDS at its current value (it only updates when 'progress' is
    present). This keeps the bar monotonic across repair-loop iterations — the
    re-verify after a fix must not move the bar backwards.
    """
    if progress_queue is None:
        return
    try:
        event = {"status": "running", "message": message}
        if progress is not None:
            event["progress"] = progress
        progress_queue.put(event)
    except Exception as e:
        logger.warning("🔬 DRYRUN: progress push failed (non-blocking): %s", e)


def validate_and_repair(run_id, robot_code, model_provider, model_name, progress_queue) -> dict:
    """Soft dryrun gate + bounded Assembler repair loop (generation path only).

    Returns a dict ALWAYS (never raises — a gate exception must never reach the
    outer workflow handler, which would convert a deliverable result into a hard
    error, the opposite of the soft-gate contract):
        {"code": <possibly-repaired code>,
         "dryrun_status": "passed" | "failed" | "unverified" | "skipped",
         "dryrun_errors": <str, only when failed>,
         "repair_usage": <dict, calculate_crewai_cost shape; {} when no repair ran>}

    Flow:
      1. DRYRUN_ENABLED off OR empty/whitespace code → skipped (no container; §8.4).
      2. Acquire Docker client + ensure the test-runner image (build_image). Any
         failure → unverified, code unchanged (learn-6 graceful degrade).
      3. Up to MAX_DRYRUN_FIXES+1 dryruns with a repair between each:
           - pass → passed.
           - fail with attempts left → conservative repair (fault-isolated; §8.3),
             stop early if repair errors / empties / makes no change (§8.6).
           - any dryrun infra failure → unverified (graceful degrade).
      4. Exhausted without a pass → failed (still delivered — soft gate).

    NEVER calls the learning system (learn-5): dryrun pass/fail must not enter the
    learning DB; only the real Docker run feeds learning.
    """
    repair_usage: dict = {}

    # §8.4 — skip the gate (and never spawn a container) when disabled or empty.
    if not settings.DRYRUN_ENABLED or not robot_code or not robot_code.strip():
        reason = "DRYRUN_ENABLED=False" if not settings.DRYRUN_ENABLED else "empty code"
        logger.info("🔬 DRYRUN: skipping gate (%s) for run_id=%s", reason, run_id)
        return {"code": robot_code, "dryrun_status": "skipped", "repair_usage": repair_usage}

    # learn-6 — Docker client acquire + image ensure. Any failure degrades to
    # 'unverified' and STILL delivers the code; never raises to the caller.
    try:
        client = get_docker_client()
        # Message-only (bar holds at 80) so a cold-host image build/pull — which can
        # take minutes (R2) — is not a silent freeze. Normally the image is present
        # and this flashes by.
        _push_progress(progress_queue, "🔬 Preparing verification environment...")
        for _build_event in build_image(client):
            pass  # consume the generator to ensure the image is present
    except Exception as e:
        logger.warning(
            "🔬 DRYRUN: Docker unavailable — delivering unverified (non-blocking): %s",
            e, exc_info=True,
        )
        return {"code": robot_code, "dryrun_status": "unverified",
                "message": f"Docker unavailable: {e}", "repair_usage": repair_usage}

    code = robot_code
    last_result = None
    try:
        for attempt in range(settings.MAX_DRYRUN_FIXES + 1):
            # Advance the bar to 88 on the first verify only; re-verifies after a
            # repair hold the bar (progress=None) so it never moves backwards.
            _push_progress(progress_queue, "🔬 Verifying generated test...",
                           88 if attempt == 0 else None)
            last_result = run_dryrun_in_container(client, run_id, code)

            if last_result["passed"]:
                logger.info("🔬 DRYRUN: passed for run_id=%s (attempt %d)", run_id, attempt)
                return {"code": code, "dryrun_status": "passed", "repair_usage": repair_usage}

            # Failed — repair only if attempts remain.
            if attempt < settings.MAX_DRYRUN_FIXES:
                _push_progress(progress_queue, "🔧 Fixing test code...", 92)
                try:
                    task_output, attempt_usage = repair_robot_code(
                        run_id, code, last_result["errors"],
                        model_provider, model_name,
                    )
                    # Count the repair cost as soon as it is known, before
                    # extraction, so it is not lost if extraction later fails.
                    _accumulate_usage(repair_usage, attempt_usage)
                    new_code = extract_and_normalize_robot_code(task_output)
                except Exception as e:
                    # §8.3 — repair fault isolation: degrade with current code,
                    # never propagate to the outer handler.
                    logger.warning(
                        "🔬 DRYRUN: repair attempt failed — stopping (non-blocking): %s",
                        e, exc_info=True,
                    )
                    break
                if not new_code or not new_code.strip():
                    logger.warning("🔬 DRYRUN: repair produced empty code — stopping")
                    break
                if new_code == code:
                    # §8.6 — no-progress short-circuit: re-dryrunning identical code
                    # cannot help; save a container spawn + an LLM call.
                    logger.info("🔬 DRYRUN: repair made no change — stopping early (§8.6)")
                    break
                code = new_code
    except Exception as e:
        # run_dryrun_in_container infrastructure failure mid-loop (Docker died,
        # output.xml missing, timeout, unreadable XML) → graceful degrade.
        logger.warning(
            "🔬 DRYRUN: dryrun execution error — delivering unverified (non-blocking): %s",
            e, exc_info=True,
        )
        return {"code": code, "dryrun_status": "unverified",
                "message": str(e), "repair_usage": repair_usage}

    # Loop exhausted (or broke early) without a pass → failed, but STILL delivered.
    return {"code": code, "dryrun_status": "failed",
            "dryrun_errors": last_result["errors"] if last_result else "",
            "repair_usage": repair_usage}
