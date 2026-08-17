import os
import re
import docker
import logging
import threading
import traceback
import requests as _requests
import xml.etree.ElementTree as ET
from collections.abc import Generator
from typing import Any

# Load src/backend/.env BEFORE the getenv reads below. config's import runs
# load_dotenv as a side effect; without this, a process that imports this
# module before config (runner_exec.app did) silently gets the hard-coded
# defaults — it only ever worked under run.sh because the shell exported the
# whole .env first. Observed failure: runner-exec built 'robot-test-runner:
# latest' from scratch instead of using the .env-pinned local image.
from src.backend.core import config as _config  # noqa: F401

# Test runner image - can be overridden by TEST_RUNNER_IMAGE_TAG env var
_DEFAULT_LOCAL_IMAGE_TAG = 'robot-test-runner:latest'
_DEFAULT_REMOTE_IMAGE = 'monkscode/nlrf:test-runner-latest'
IMAGE_TAG = os.getenv('TEST_RUNNER_IMAGE_TAG', _DEFAULT_LOCAL_IMAGE_TAG)
# Remote image pulled when IMAGE_TAG is missing locally. The pulled image is then
# tagged AS IMAGE_TAG, so these two must name the same build — otherwise the user's
# configured tag silently holds different content. Hard-coding the published
# -latest default did exactly that: a user following the README and setting
# TEST_RUNNER_IMAGE_TAG=...-develop received the main-branch image under the
# -develop name. Track IMAGE_TAG unless the operator names a mirror explicitly.
# IMAGE_TAG's own default is a local-only name with no registry, so that case
# keeps the published image as the pull source.
REMOTE_IMAGE = os.getenv('REMOTE_DOCKER_IMAGE', '').strip() or (
    _DEFAULT_REMOTE_IMAGE if IMAGE_TAG == _DEFAULT_LOCAL_IMAGE_TAG else IMAGE_TAG)
# Whether to prefer remote images - can be overridden by PREFER_REMOTE_DOCKER_IMAGE env var
PREFER_REMOTE_IMAGE = os.getenv('PREFER_REMOTE_DOCKER_IMAGE', 'false').lower() == 'true'
# Maximum seconds to wait for a test container to finish (default: 30 minutes)
TEST_EXECUTION_TIMEOUT = int(os.getenv('TEST_EXECUTION_TIMEOUT', '1800'))

DOCKERFILE_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), '..', '..', '..')

# The artifact store owns the staging root; docker_service consumes it so there
# is a single source of truth for the robot_tests/ path. Host-path mount
# resolution below stays here — that is an execution concern, not storage.
from src.backend.core.artifact_store import STAGING_ROOT
from src.backend.core.config import settings
ROBOT_TESTS_DIR = str(STAGING_ROOT)

# Docker-in-Docker Support: When running inside a Docker container, we need to use
# the host's absolute path for volume mounts, not the container's internal path.
# This env var should be set in docker-compose.yml to the host's robot_tests path.
HOST_ROBOT_TESTS_DIR = os.getenv('HOST_ROBOT_TESTS_DIR', os.path.abspath(ROBOT_TESTS_DIR))


def resolve_host_robot_tests_dir(client: docker.DockerClient) -> str:
    """
    Resolve the host path mounted to /app/robot_tests for this FastAPI container.

    This avoids brittle ${PWD} behavior on Windows/Compose by querying Docker
    for the current container mount source and falling back to env/config only
    when lookup is unavailable.
    """
    candidate_container_refs = [
        os.getenv('HOSTNAME', '').strip(),
        os.getenv('CONTAINER_NAME', '').strip(),
        'nlrf-fastapi'
    ]

    for container_ref in candidate_container_refs:
        if not container_ref:
            continue
        try:
            current_container = client.containers.get(container_ref)
            for mount in current_container.attrs.get('Mounts', []):
                if mount.get('Destination') == '/app/robot_tests' and mount.get('Source'):
                    source = mount['Source']
                    logging.info(
                        f"🐳 DOCKER SERVICE: Resolved host robot_tests mount from container inspect ({container_ref}): {source}")
                    return source
        except docker.errors.NotFound:
            # Expected miss: this candidate name isn't a running container (e.g.
            # the machine HOSTNAME under `run.sh` dev, which is not a container at
            # all). Not an error — we try the next candidate and fall back to
            # HOST_ROBOT_TESTS_DIR if none match. DEBUG so it stops printing a
            # false-alarm WARNING on every run.
            logging.debug(
                f"🐳 DOCKER SERVICE: container inspect miss for '{container_ref}' "
                "(not a container); trying next candidate")
        except docker.errors.DockerException as e:
            # A real Docker problem (daemon unreachable, permission denied, etc.) —
            # keep this at WARNING; it may explain a downstream mount failure.
            logging.warning(
                "⚠️  DOCKER SERVICE: Docker error resolving mount source via container "
                f"inspect ({container_ref}): {type(e).__name__}: {e}")

    logging.info(
        f"🐳 DOCKER SERVICE: Falling back to HOST_ROBOT_TESTS_DIR: {HOST_ROBOT_TESTS_DIR}")
    return HOST_ROBOT_TESTS_DIR


def normalize_docker_mount_source(path: str) -> str:
    """
    Normalize host path for Docker bind mount source.

    On Docker Desktop (Linux engine), Windows paths like C:\\... need to be
    converted to /run/desktop/mnt/host/c/... when passed from a Linux container.
    """
    if not path:
        return path

    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', path):
        drive = path[0].lower()
        remainder = path[2:].replace('\\', '/').lstrip('/')
        normalized = f"/run/desktop/mnt/host/{drive}/{remainder}"
        logging.info(
            f"🐳 DOCKER SERVICE: Normalized Windows mount path '{path}' -> '{normalized}'")
        return normalized

    return path


def log_docker_operation(operation: str, details: str = "", level: str = "info"):
    """Centralized logging for all Docker operations to help debug 409 errors."""
    prefix = "🐳 DOCKER_DEBUG"
    message = f"{prefix}: {operation}"
    if details:
        message += f" - {details}"

    if level == "error":
        logging.error(message)
        logging.error(f"{prefix}: Stack trace: {traceback.format_stack()}")
    elif level == "warning":
        logging.warning(message)
    else:
        logging.info(message)


class ContainerLogsInterceptor:
    """Wrapper to intercept and log any container.logs() calls that might still exist."""

    def __init__(self, container):
        self._container = container
        log_docker_operation("ContainerLogsInterceptor",
                             f"Wrapping container {container.name if hasattr(container, 'name') else container.id}")

    def __getattr__(self, name):
        if name == 'logs':
            log_docker_operation("CONTAINER_LOGS_CALL_DETECTED",
                                 f"🚨 CRITICAL: Someone is trying to call container.logs()! This should NOT happen!", "error")
            log_docker_operation("CONTAINER_LOGS_CALL_DETECTED",
                                 f"Container: {self._container.name if hasattr(self._container, 'name') else self._container.id}", "error")
            log_docker_operation("CONTAINER_LOGS_CALL_DETECTED",
                                 f"Stack trace: {''.join(traceback.format_stack())}", "error")
            # Return a function that raises an error instead of calling the real logs method

            def logs_error_function(*args, **kwargs):
                error_msg = "🚨 CRITICAL ERROR: container.logs() was called! This should use Robot Framework files instead!"
                log_docker_operation(
                    "CONTAINER_LOGS_ERROR", error_msg, "error")
                raise RuntimeError(error_msg)
            return logs_error_function
        return getattr(self._container, name)


def get_docker_client():
    log_docker_operation("get_docker_client",
                         "Attempting to connect to Docker")
    try:
        client = docker.from_env()
        client.ping()
        log_docker_operation("get_docker_client",
                             "Successfully connected to Docker")
        return client
    except docker.errors.DockerException as e:
        log_docker_operation("get_docker_client",
                             f"Docker connection failed: {e}", "error")
        raise ConnectionError(
            f"Docker is not available. Please ensure Docker Desktop is installed and running. Details: {e}")


# Serialises image provisioning so a boot-time warm-up and a Run Test click never
# start two pulls of the same ~0.5 GB image. Whichever arrives second waits, then
# finds the image already present and returns immediately. Held only by callers
# that fully consume build_image (the ensure-image endpoint) or by warm_image_cache
# — never inside the generator itself, where an abandoned consumer would leak it.
IMAGE_PROVISION_LOCK = threading.Lock()


def warm_image_cache(client: docker.DockerClient) -> bool:
    """Pull the runner image ahead of time. Returns True if it pulled one.

    Called in the background at runner-exec startup. The download is ~0.5 GB and
    measured 99s; starting it here overlaps it with signup, query entry and the
    generation stage, instead of making the user watch it after clicking Run Test.

    PULL ONLY — it never falls back to a local build. A build is minutes of CPU and
    disk, and silently starting one on every boot is a surprise; the on-demand path
    in build_image still builds when that is how the deployment is configured.

    Never raises: this runs on a startup thread, and a Docker hiccup at boot must
    not take the executor down. A failure just means the first run pays the cost,
    which is the behaviour without warm-up anyway.
    """
    if not PREFER_REMOTE_IMAGE:
        logging.info("[WARMUP] Remote pull disabled; leaving image provisioning to first use.")
        return False
    with IMAGE_PROVISION_LOCK:
        try:
            client.images.get(IMAGE_TAG)
            logging.info(f"[WARMUP] Runner image '{IMAGE_TAG}' already present.")
            return False
        except docker.errors.ImageNotFound:
            pass
        except Exception as e:  # noqa: BLE001 — socket unavailable at boot, etc.
            logging.warning(f"[WARMUP] Could not inspect local images: {e}")
            return False

        logging.info(f"[WARMUP] Pre-fetching runner image {REMOTE_IMAGE} in the background...")
        try:
            for log in client.api.pull(REMOTE_IMAGE, stream=True, decode=True):
                if 'error' in log:
                    # The daemon reports most pull failures this way rather than
                    # by raising, so this branch is the common one.
                    raise docker.errors.APIError(log['error'])
            client.images.get(REMOTE_IMAGE).tag(IMAGE_TAG)
            logging.info(f"[WARMUP] Runner image ready as '{IMAGE_TAG}'.")
            return True
        except Exception as e:  # noqa: BLE001 — best-effort by design
            logging.warning(
                f"[WARMUP] Pre-fetch of {REMOTE_IMAGE} failed ({e}); "
                "the first test run will provision the image instead.")
            return False


def build_image(client: docker.DockerClient) -> Generator[dict[str, Any], None, None]:
    """
    Ensure the Docker image is available for test execution.
    Tries to pull from Docker Hub first, falls back to local build if needed.
    """
    try:
        # Check if image already exists locally
        client.images.get(IMAGE_TAG)
        logging.info(
            f"Docker image '{IMAGE_TAG}' already exists. Skipping build.")
        yield {"status": "running", "message": "Using existing container image for test execution..."}
        return
    except docker.errors.ImageNotFound:
        logging.info(
            f"Docker image '{IMAGE_TAG}' not found locally.")
        
        # Try to pull pre-built image from Docker Hub first (if enabled)
        if PREFER_REMOTE_IMAGE:
            logging.info(f"Attempting to pull pre-built image from Docker Hub: {REMOTE_IMAGE}")
            yield {"status": "running", "message": "Downloading pre-built container image from Docker Hub..."}
            
            try:
                pull_logs = client.api.pull(REMOTE_IMAGE, stream=True, decode=True)
                for log in pull_logs:
                    if 'status' in log:
                        status_msg = log['status']
                        if 'progress' in log:
                            status_msg += f" {log['progress']}"
                        logging.info(f"🐳 Pull: {status_msg}")
                        yield {"status": "running", "log": status_msg}
                    if 'error' in log:
                        logging.error(f"Docker pull error: {log['error']}")
                        raise docker.errors.APIError(log['error'])
                
                # Tag the pulled image with our local tag
                remote_image = client.images.get(REMOTE_IMAGE)
                remote_image.tag(IMAGE_TAG)
                logging.info(f"✅ Successfully pulled and tagged image from Docker Hub as '{IMAGE_TAG}'")
                yield {"status": "running", "message": "Pre-built image downloaded successfully!"}
                return
                
            except (docker.errors.APIError, docker.errors.ImageNotFound) as pull_error:
                logging.warning(f"⚠️ Failed to pull from Docker Hub: {pull_error}")
                logging.info("Falling back to local image build...")
                yield {"status": "running", "message": "Pull failed, building container image locally (first time only)..."}
        else:
            logging.info("Remote image pull disabled. Building locally...")
            yield {"status": "running", "message": "Building container image locally (first time only)..."}
        
        # Fallback: Build locally if pull failed
        try:
            # Ensure BuildKit is enabled for modern Dockerfile features and --mount usage.
            # Some environments (especially Windows) may not have DOCKER_BUILDKIT set,
            # so we set it in the process environment to avoid build failures.
            os.environ.setdefault('DOCKER_BUILDKIT', '1')
            logging.info("🐳 DOCKER_DEBUG: Ensuring DOCKER_BUILDKIT=1 for the build process")

            build_logs = client.api.build(
                path=DOCKERFILE_PATH, dockerfile='Dockerfile.test-runner', tag=IMAGE_TAG, rm=True, decode=True)
            for log in build_logs:
                if 'stream' in log:
                    log_message = log['stream'].strip()
                    if log_message:
                        yield {"status": "running", "log": log_message}
                if 'error' in log:
                    logging.error(f"Docker build error: {log['error']}")
                    raise docker.errors.BuildError(
                        log['error'], build_log=build_logs)
            logging.info(f"Successfully built Docker image '{IMAGE_TAG}'.")
            yield {"status": "running", "message": "Container image built successfully!"}
        except docker.errors.BuildError as e:
            logging.error(f"Failed to build Docker image: {e}")
            raise


def run_test_in_container(client: docker.DockerClient, run_id: str, test_filename: str) -> dict[str, Any]:
    container = None
    logging.info(
        f"🚀 DOCKER SERVICE: Starting test execution for run_id={run_id}, test_filename={test_filename}")

    try:
        robot_command = ["robot", "--outputdir",
                         f"/app/robot_tests/{run_id}", f"/app/robot_tests/{run_id}/{test_filename}"]
        logging.info(
            f"🤖 DOCKER SERVICE: Robot command: {' '.join(robot_command)}")

        host_robot_tests_dir = resolve_host_robot_tests_dir(client)
        normalized_host_robot_tests_dir = normalize_docker_mount_source(host_robot_tests_dir)

        # Always validate file presence using the FastAPI container-visible path.
        container_test_file = os.path.join(ROBOT_TESTS_DIR, run_id, test_filename)
        if not os.path.exists(container_test_file):
            raise RuntimeError(
                "Test file not found in FastAPI container robot_tests directory. "
                f"Expected: {container_test_file}. "
                "This usually means the test file was not written correctly before Docker execution."
            )

        host_test_file = os.path.join(normalized_host_robot_tests_dir, run_id, test_filename)
        if not os.path.exists(host_test_file):
            if normalized_host_robot_tests_dir.startswith('/run/desktop/mnt/host/'):
                logging.info(
                    f"ℹ️  DOCKER SERVICE: Skipping strict host file pre-check for Docker Desktop mount path: {host_test_file}. "
                    "Container-local check already passed."
                )
            else:
                logging.warning(
                    f"⚠️  DOCKER SERVICE: Host path pre-check could not verify file existence: {host_test_file}. "
                    "Continuing anyway because Docker daemon may still resolve this mount source."
                )

        # Container configuration
        # Docker-in-Docker: Use resolved host path for volume mount
        container_config = {
            "image": IMAGE_TAG,
            "command": robot_command,
            "volumes": {normalized_host_robot_tests_dir: {'bind': '/app/robot_tests', 'mode': 'rw'}},
            "working_dir": "/app",
            "detach": True,  # Run detached to manage container lifecycle
            "auto_remove": False,  # Don't auto-remove so we can get logs properly
            "name": f"robot-test-{run_id}",  # Give container a unique name
            "mem_limit": "2g",
            "pids_limit": 256,
            # Phase 4 least-privilege: drop all caps and block privilege
            # escalation. Chrome runs with --no-sandbox so it needs no caps.
            # Network is intentionally kept — the runner drives real websites.
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        if settings.RUNNER_READ_ONLY_ROOTFS:
            # Validated in Task 10. Chrome/Playwright need writable scratch even
            # with --disable-dev-shm-usage, so a read-only rootfs gets tmpfs for
            # /tmp. This "/tmp" is a per-container, in-memory tmpfs mount target
            # (isolated from the host and other containers, wiped on exit), not a
            # shared host temp dir — see the python:S5443 note in
            # sonar-project.properties.
            container_config["read_only"] = True
            container_config["tmpfs"] = {"/tmp": "size=512m,exec"}
        logging.info(
            f"🐳 DOCKER SERVICE: Container config created for robot-test-{run_id} with volume {normalized_host_robot_tests_dir}:/app/robot_tests")

        # Clean up any existing container with the same name
        container_name = f"robot-test-{run_id}"
        logging.info(
            f"🧹 DOCKER SERVICE: Checking for existing container: {container_name}")
        try:
            existing_container = client.containers.get(container_name)
            logging.warning(
                f"🚨 DOCKER SERVICE: Found existing container {container_name}, removing it")
            existing_container.remove(force=True)
            logging.info(
                f"✅ DOCKER SERVICE: Successfully removed existing container {container_name}")
        except docker.errors.NotFound:
            logging.info(
                f"✅ DOCKER SERVICE: No existing container {container_name} found, proceeding")
        except Exception as e:
            logging.error(
                f"❌ DOCKER SERVICE: Failed to remove existing container {container_name}: {e}")
            # Try to clean up all test containers
            cleanup_test_containers(client)

        # Create and start the container
        logging.info(
            f"🚀 DOCKER SERVICE: Creating and starting container {container_name}")
        container = client.containers.run(**container_config)
        logging.info(
            f"✅ DOCKER SERVICE: Container {container_name} created successfully with ID: {container.id}")

        # Wrap container with interceptor to catch any logs() calls
        container = ContainerLogsInterceptor(container)
        log_docker_operation(
            "container_wrapped", f"Container {container_name} wrapped with logs interceptor")

        # Wait for container to finish
        logging.info(
            f"⏳ DOCKER SERVICE: Waiting for container {container_name} to finish execution")
        try:
            result = container.wait(timeout=TEST_EXECUTION_TIMEOUT)
            exit_code = result['StatusCode']
            logging.info(
                f"🏁 DOCKER SERVICE: Container {container_name} finished with exit code: {exit_code}")
        except _requests.exceptions.ReadTimeout:
            logging.error(
                f"❌ DOCKER SERVICE: Container {container_name} timed out after {TEST_EXECUTION_TIMEOUT}s. Killing it.")
            try:
                container._container.stop(timeout=10)
                container._container.remove(force=True)
            except Exception as cleanup_err:
                logging.warning(f"Cleanup after timeout failed: {cleanup_err}")
            raise RuntimeError(
                f"Test execution timed out after {TEST_EXECUTION_TIMEOUT // 60} minutes. "
                "The test may have an infinite loop or unresponsive browser. Container has been stopped."
            )

        container_startup_logs = ""
        try:
            # Capture container stdout/stderr before cleanup for system-error diagnosis.
            raw_logs = client.api.logs(container.id, stdout=True, stderr=True, tail=400)
            if isinstance(raw_logs, (bytes, bytearray)):
                container_startup_logs = raw_logs.decode('utf-8', errors='replace').strip()
            else:
                container_startup_logs = str(raw_logs).strip()
        except Exception as e:
            logging.warning(
                f"⚠️  DOCKER SERVICE: Could not capture container startup logs: {e}")

        # Clean up container immediately after execution to prevent conflicts
        logging.info(
            f"🧹 DOCKER SERVICE: Starting container cleanup for {container_name}")
        try:
            container.remove()
            logging.info(
                f"✅ DOCKER SERVICE: Successfully removed container {container_name}")
        except docker.errors.NotFound:
            logging.info(
                f"ℹ️  DOCKER SERVICE: Container {container_name} was already removed")
        except Exception as e:
            logging.error(
                f"❌ DOCKER SERVICE: Failed to remove container {container_name}: {e}")

        # Use Robot Framework output files instead of Docker container logs
        output_xml_path = os.path.join(ROBOT_TESTS_DIR, run_id, "output.xml")
        log_html_path = os.path.join(ROBOT_TESTS_DIR, run_id, "log.html")
        report_html_path = os.path.join(ROBOT_TESTS_DIR, run_id, "report.html")

        logging.info(
            f"📁 DOCKER SERVICE: Looking for Robot Framework output files:")
        logging.info(f"   - output.xml: {output_xml_path}")
        logging.info(f"   - log.html: {log_html_path}")
        logging.info(f"   - report.html: {report_html_path}")

        # Generate logs from Robot Framework files (much more reliable than Docker logs)
        logging.info(
            f"📋 DOCKER SERVICE: Extracting logs from Robot Framework files (NOT from Docker container logs)")
        robot_logs = _extract_robot_framework_logs(
            output_xml_path, log_html_path, exit_code)
        logging.info(
            f"✅ DOCKER SERVICE: Successfully extracted {len(robot_logs)} characters of Robot Framework logs")

        # Check test results from Robot Framework output files
        logging.info(
            f"🔍 DOCKER SERVICE: Analyzing test results from Robot Framework files")
        if os.path.exists(output_xml_path):
            logging.info(
                f"✅ DOCKER SERVICE: Found output.xml file, parsing test results")
            try:
                # Parse XML properly to determine actual test results
                # We need to check the TOP-LEVEL test status, not grep for "FAIL" strings
                # because "Run Keyword And Ignore Error" can have FAIL status inside but test still passes
                tree = ET.parse(output_xml_path)
                root = tree.getroot()
                
                # Check statistics section for overall pass/fail count
                stats = root.find('.//statistics/total/stat')
                if stats is not None:
                    fail_count = int(stats.get('fail', '0'))
                    pass_count = int(stats.get('pass', '0'))
                    tests_passed = fail_count == 0 and pass_count > 0
                    logging.info(
                        f"📊 DOCKER SERVICE: Test statistics: pass={pass_count}, fail={fail_count}, tests_passed={tests_passed}")
                else:
                    # Fallback to old method if statistics section not found
                    with open(output_xml_path, 'r') as f:
                        xml_content = f.read()
                        tests_passed = 'fail="0"' in xml_content
                    logging.warning(f"⚠️  DOCKER SERVICE: Statistics section not found in output.xml, using fallback method")

                if tests_passed:
                    message = "Test execution finished: All tests passed."
                    logging.info(f"🎉 DOCKER SERVICE: {message}")
                    return {"status": "complete", "message": message, "test_status": "passed",
                            "output_xml_path": output_xml_path,
                            "exit_code": exit_code,
                            "result": {
                        'logs': robot_logs,
                        'log_html': f"/reports/{run_id}/log.html",
                        'report_html': f"/reports/{run_id}/report.html"
                    }}
                else:
                    message = f"Test execution finished: Some tests failed (exit code {exit_code})."
                    logging.info(f"⚠️  DOCKER SERVICE: {message}")
                    return {"status": "complete", "message": message, "test_status": "failed",
                            "output_xml_path": output_xml_path,
                            "exit_code": exit_code,
                            "result": {
                        'logs': robot_logs,
                        'log_html': f"/reports/{run_id}/log.html",
                        'report_html': f"/reports/{run_id}/report.html"
                    }}

            except Exception as e:
                logging.error(
                    f"❌ DOCKER SERVICE: Failed to parse output.xml: {e}")
                # Fall back to exit code analysis

        else:
            logging.warning(
                f"⚠️  DOCKER SERVICE: output.xml file not found at {output_xml_path}")

        # Fallback based on exit code when XML parsing fails
        logging.info(
            f"🔄 DOCKER SERVICE: Using exit code fallback analysis (exit_code={exit_code})")
        if exit_code == 0:
            message = "Test execution finished: All tests passed."
            logging.info(f"✅ DOCKER SERVICE: {message}")
            return {"status": "complete", "message": message, "test_status": "passed",
                    "output_xml_path": output_xml_path if os.path.exists(output_xml_path) else None,
                    "exit_code": exit_code,
                    "result": {
                'logs': robot_logs,
                'log_html': f"/reports/{run_id}/log.html",
                'report_html': f"/reports/{run_id}/report.html"
            }}
        else:
            if os.path.exists(log_html_path):
                message = f"Test execution finished: Some tests failed (exit code {exit_code})."
                logging.info(f"⚠️  DOCKER SERVICE: {message}")
                return {"status": "complete", "message": message, "test_status": "failed",
                        "output_xml_path": output_xml_path if os.path.exists(output_xml_path) else None,
                        "exit_code": exit_code,
                        "result": {
                    'logs': robot_logs,
                    'log_html': f"/reports/{run_id}/log.html",
                    'report_html': f"/reports/{run_id}/report.html"
                }}
            else:
                error_logs = f"Docker container exited with a system error (exit code {exit_code}).\n"
                error_logs += "Robot Framework reports were not generated, indicating a problem with the test runner itself.\n\n"
                if container_startup_logs:
                    error_logs += "Container stdout/stderr (tail):\n"
                    error_logs += f"{container_startup_logs}\n\n"
                error_logs += f"Available Logs:\n{robot_logs}"
                logging.error(f"❌ DOCKER SERVICE: System error - {error_logs}")
                raise RuntimeError(error_logs)

    except Exception as e:
        # Log detailed error information
        log_docker_operation(
            "EXCEPTION_OCCURRED", f"Exception in run_test_in_container: {str(e)}", "error")
        log_docker_operation(
            "EXCEPTION_TYPE", f"Exception type: {type(e).__name__}", "error")

        # Check if this is the 409 error we're looking for
        if "409" in str(e) and "logs" in str(e):
            log_docker_operation("409_ERROR_DETECTED",
                                 "🚨 FOUND THE 409 LOGS ERROR!", "error")
            log_docker_operation("409_ERROR_DETAILS",
                                 f"Full error: {str(e)}", "error")
            log_docker_operation(
                "409_ERROR_STACK", f"Stack trace: {''.join(traceback.format_exc())}", "error")

        # Clean up container if it exists
        if container:
            container_name = getattr(container, 'name', 'unknown')
            if hasattr(container, '_container'):  # It's our interceptor
                container_name = getattr(
                    container._container, 'name', 'unknown')

            logging.error(
                f"🧹 DOCKER SERVICE: Exception occurred, cleaning up container {container_name}")
            try:
                if hasattr(container, '_container'):  # It's our interceptor
                    container._container.remove(force=True)
                else:
                    container.remove(force=True)
                logging.info(f"✅ DOCKER SERVICE: Emergency cleanup successful")
            except Exception as cleanup_error:
                logging.error(
                    f"❌ DOCKER SERVICE: Emergency cleanup failed: {cleanup_error}")

        logging.error(
            f"❌ DOCKER SERVICE: Docker container execution failed: {e}")
        raise RuntimeError(f"Docker container execution failed: {e}")


def _extract_robot_framework_logs(output_xml_path: str, log_html_path: str, exit_code: int) -> str:
    """
    Extract logs from Robot Framework output files instead of Docker container logs.

    Args:
        output_xml_path: Path to output.xml file
        log_html_path: Path to log.html file  
        exit_code: Container exit code

    Returns:
        Formatted log string with test execution details
    """
    logging.info(f"📋 LOG EXTRACTOR: Starting Robot Framework log extraction")
    logging.info(f"   - output_xml_path: {output_xml_path}")
    logging.info(f"   - log_html_path: {log_html_path}")
    logging.info(f"   - exit_code: {exit_code}")

    logs = []

    # Add basic execution info
    logs.append(f"Robot Framework Test Execution (Exit Code: {exit_code})")
    logs.append("=" * 50)

    # Try to extract information from output.xml
    if os.path.exists(output_xml_path):
        logging.info(
            f"✅ LOG EXTRACTOR: Found output.xml file, parsing XML content")
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(output_xml_path)
            root = tree.getroot()
            logging.info(
                f"📊 LOG EXTRACTOR: Successfully parsed XML, root element: {root.tag}")

            # Extract suite information
            suite = root.find('.//suite')
            if suite is not None:
                suite_name = suite.get('name', 'Unknown Suite')
                logs.append(f"Suite: {suite_name}")
                logging.info(
                    f"📁 LOG EXTRACTOR: Found test suite: {suite_name}")

                # Extract test information
                tests = suite.findall('.//test')
                logging.info(
                    f"🧪 LOG EXTRACTOR: Found {len(tests)} test(s) in suite")
                for test in tests:
                    test_name = test.get('name', 'Unknown Test')
                    test_status = test.find('.//status')
                    if test_status is not None:
                        status = test_status.get('status', 'UNKNOWN')
                        start_time = test_status.get('starttime', '')
                        end_time = test_status.get('endtime', '')

                        logs.append(f"  Test: {test_name} - {status}")
                        if start_time and end_time:
                            logs.append(
                                f"    Time: {start_time} to {end_time}")

                        # Extract failure messages
                        if status == 'FAIL':
                            message = test_status.text
                            if message:
                                logs.append(f"    Error: {message.strip()}")

                            # Extract keyword failures for more detail
                            keywords = test.findall('.//kw')
                            for kw in keywords:
                                kw_status = kw.find('.//status')
                                if kw_status is not None and kw_status.get('status') == 'FAIL':
                                    kw_name = kw.get('name', 'Unknown Keyword')
                                    kw_message = kw_status.text
                                    logs.append(
                                        f"    Failed Keyword: {kw_name}")
                                    if kw_message:
                                        logs.append(
                                            f"      Details: {kw_message.strip()}")

                # Extract statistics
                statistics = root.find('.//statistics')
                if statistics is not None:
                    total = statistics.find('.//stat[@pass]')
                    if total is not None:
                        passed = total.get('pass', '0')
                        failed = total.get('fail', '0')
                        logs.append(
                            f"Results: {passed} passed, {failed} failed")

        except Exception as e:
            logging.error(f"❌ LOG EXTRACTOR: Failed to parse output.xml: {e}")
            logs.append(f"Failed to parse output.xml: {e}")
    else:
        logging.warning(
            f"⚠️  LOG EXTRACTOR: No output.xml file found at {output_xml_path}")
        logs.append(
            "No output.xml file found - test execution may have failed to start")

    # Add log file availability info
    if os.path.exists(log_html_path):
        logging.info(
            f"✅ LOG EXTRACTOR: Found log.html file at {log_html_path}")
        logs.append(f"Detailed logs available in: {log_html_path}")
    else:
        logging.warning(
            f"⚠️  LOG EXTRACTOR: No log.html file found at {log_html_path}")
        logs.append("No log.html file generated")

    final_logs = "\n".join(logs)
    logging.info(
        f"✅ LOG EXTRACTOR: Completed log extraction, generated {len(final_logs)} characters")
    return final_logs


def cleanup_test_containers(client: docker.DockerClient) -> dict[str, Any]:
    """Clean up any orphaned test containers."""
    try:
        # Find all containers with robot-test prefix
        containers = client.containers.list(
            all=True, filters={"name": "robot-test-"})

        cleaned_count = 0
        for container in containers:
            try:
                container.remove(force=True)
                cleaned_count += 1
                logging.info(
                    f"Cleaned up orphaned container: {container.name}")
            except Exception as e:
                logging.warning(
                    f"Failed to clean up container {container.name}: {e}")

        return {
            "status": "success",
            "message": f"Cleaned up {cleaned_count} orphaned test containers",
            "containers_cleaned": cleaned_count
        }
    except Exception as e:
        logging.error(f"Failed to cleanup test containers: {e}")
        return {
            "status": "error",
            "message": f"Failed to cleanup containers: {e}",
            "containers_cleaned": 0
        }


def rebuild_image(client: docker.DockerClient) -> dict[str, str]:
    try:
        try:
            client.images.remove(image=IMAGE_TAG, force=True)
            logging.info(f"Removed existing Docker image '{IMAGE_TAG}'.")
        except docker.errors.ImageNotFound:
            logging.info(f"No existing Docker image '{IMAGE_TAG}' to remove.")

        client.images.build(path=DOCKERFILE_PATH, dockerfile='Dockerfile.test-runner', tag=IMAGE_TAG, rm=True)
        logging.info(f"Successfully rebuilt Docker image '{IMAGE_TAG}'.")
        return {"status": "success", "message": f"Docker image '{IMAGE_TAG}' rebuilt successfully."}
    except docker.errors.DockerException as e:
        logging.error(f"Failed to rebuild Docker image: {e}")
        raise ConnectionError(f"Docker error: {e}")


def get_docker_status(client: docker.DockerClient) -> dict[str, Any]:
    try:
        image = client.images.get(IMAGE_TAG)
        image_info = {
            "exists": True,
            "id": image.id,
            "created": image.attrs.get('Created', 'Unknown'),
            "size": f"{image.attrs.get('Size', 0) / (1024*1024):.1f} MB"
        }
    except docker.errors.ImageNotFound:
        image_info = {"exists": False}

    return {
        "status": "success",
        "docker_available": True,
        "image": image_info
    }
