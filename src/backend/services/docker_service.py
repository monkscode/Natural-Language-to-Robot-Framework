import os
import docker
import logging
import traceback
from typing import Generator, Dict, Any

IMAGE_TAG = "robot-test-runner:latest"
DOCKERFILE_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), '..', '..', '..')
ROBOT_TESTS_DIR = os.path.join(DOCKERFILE_PATH, 'robot_tests')


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


def build_image(client: docker.DockerClient) -> Generator[Dict[str, Any], None, None]:
    try:
        client.images.get(IMAGE_TAG)
        logging.info(
            f"Docker image '{IMAGE_TAG}' already exists. Skipping build.")
        yield {"status": "running", "message": "Using existing container image for test execution..."}
        return
    except docker.errors.ImageNotFound:
        logging.info(
            f"Docker image '{IMAGE_TAG}' not found. Building new image.")
        yield {"status": "running", "message": "Building container image for test execution (first time only)..."}
        try:
            build_logs = client.api.build(
                path=DOCKERFILE_PATH, tag=IMAGE_TAG, rm=True, decode=True)
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


def run_test_in_container(client: docker.DockerClient, run_id: str, test_filename: str, enable_healing: bool = True) -> Dict[str, Any]:
    container = None
    logging.info(
        f"🚀 DOCKER SERVICE: Starting test execution for run_id={run_id}, test_filename={test_filename}, enable_healing={enable_healing}")

    try:
        robot_command = ["robot", "--outputdir",
                         f"/app/robot_tests/{run_id}", f"/app/robot_tests/{run_id}/{test_filename}"]
        logging.info(
            f"🤖 DOCKER SERVICE: Robot command: {' '.join(robot_command)}")

        # Enhanced container configuration for healing support
        container_config = {
            "image": IMAGE_TAG,
            "command": robot_command,
            "volumes": {os.path.abspath(ROBOT_TESTS_DIR): {'bind': '/app/robot_tests', 'mode': 'rw'}},
            "working_dir": "/app",
            "detach": True,  # Run detached to manage container lifecycle
            "auto_remove": False,  # Don't auto-remove so we can get logs properly
            "name": f"robot-test-{run_id}"  # Give container a unique name
        }
        logging.info(
            f"🐳 DOCKER SERVICE: Container config created for robot-test-{run_id}")

        # Add healing-specific environment variables and configurations
        if enable_healing:
            container_config["environment"] = {
                "HEALING_ENABLED": "true",
                "CHROME_HEADLESS": "true",
                "DISPLAY": ":99"  # Virtual display for headless Chrome
            }
            # Add shared memory for Chrome stability
            container_config["shm_size"] = "2g"
            logging.info(
                f"🔧 DOCKER SERVICE: Added healing configuration to container")

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
        result = container.wait()
        exit_code = result['StatusCode']
        logging.info(
            f"🏁 DOCKER SERVICE: Container {container_name} finished with exit code: {exit_code}")

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
                # Parse XML to determine actual test results
                with open(output_xml_path, 'r') as f:
                    xml_content = f.read()
                    # Check if tests actually passed by looking at the XML
                    tests_passed = 'fail="0"' in xml_content and 'status="FAIL"' not in xml_content

                    logging.info(
                        f"📊 DOCKER SERVICE: Test analysis results: tests_passed={tests_passed}")

                    if tests_passed:
                        message = "Test execution finished: All tests passed."
                        logging.info(f"🎉 DOCKER SERVICE: {message}")
                        return {"status": "complete", "message": message, "result": {
                            'logs': robot_logs,
                            'log_html': f"/reports/{run_id}/log.html",
                            'report_html': f"/reports/{run_id}/report.html"
                        }}
                    else:
                        message = f"Test execution finished: Some tests failed (exit code {exit_code})."
                        logging.info(f"⚠️  DOCKER SERVICE: {message}")
                        return {"status": "complete", "message": message, "result": {
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
            return {"status": "complete", "message": message, "result": {
                'logs': robot_logs,
                'log_html': f"/reports/{run_id}/log.html",
                'report_html': f"/reports/{run_id}/report.html"
            }}
        else:
            if os.path.exists(log_html_path):
                message = f"Test execution finished: Some tests failed (exit code {exit_code})."
                logging.info(f"⚠️  DOCKER SERVICE: {message}")
                return {"status": "complete", "message": message, "result": {
                    'logs': robot_logs,
                    'log_html': f"/reports/{run_id}/log.html",
                    'report_html': f"/reports/{run_id}/report.html"
                }}
            else:
                error_logs = f"Docker container exited with a system error (exit code {exit_code}).\n"
                error_logs += "Robot Framework reports were not generated, indicating a problem with the test runner itself.\n\n"
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


def cleanup_test_containers(client: docker.DockerClient) -> Dict[str, Any]:
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


def rebuild_image(client: docker.DockerClient) -> Dict[str, str]:
    try:
        try:
            client.images.remove(image=IMAGE_TAG, force=True)
            logging.info(f"Removed existing Docker image '{IMAGE_TAG}'.")
        except docker.errors.ImageNotFound:
            logging.info(f"No existing Docker image '{IMAGE_TAG}' to remove.")

        client.images.build(path=DOCKERFILE_PATH, tag=IMAGE_TAG, rm=True)
        logging.info(f"Successfully rebuilt Docker image '{IMAGE_TAG}'.")
        return {"status": "success", "message": f"Docker image '{IMAGE_TAG}' rebuilt successfully."}
    except docker.errors.DockerException as e:
        logging.error(f"Failed to rebuild Docker image: {e}")
        raise ConnectionError(f"Docker error: {e}")


def get_docker_status(client: docker.DockerClient) -> Dict[str, Any]:
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


def create_persistent_chrome_container(client: docker.DockerClient, session_id: str) -> docker.models.containers.Container:
    """
    Create a persistent Chrome container for healing validation.

    Args:
        client: Docker client
        session_id: Unique session identifier

    Returns:
        Docker container object
    """
    try:
        container_name = f"chrome-healing-{session_id}"

        # Chrome container configuration for healing
        container = client.containers.run(
            image=IMAGE_TAG,
            name=container_name,
            command=["tail", "-f", "/dev/null"],  # Keep container running
            environment={
                "DISPLAY": ":99",
                "CHROME_HEADLESS": "true",
                "HEALING_SESSION": "true"
            },
            volumes={os.path.abspath(ROBOT_TESTS_DIR): {
                'bind': '/app/robot_tests', 'mode': 'rw'}},
            working_dir="/app",
            detach=True,
            auto_remove=False,  # Don't auto-remove for persistent sessions
            shm_size="2g",  # Shared memory for Chrome stability
            cap_add=["SYS_ADMIN"],  # Required for Chrome sandbox
            security_opt=["seccomp=unconfined"]  # Chrome security options
        )

        logging.info(f"Created persistent Chrome container: {container_name}")
        return container

    except docker.errors.DockerException as e:
        logging.error(f"Failed to create persistent Chrome container: {e}")
        raise RuntimeError(f"Failed to create Chrome container: {e}")


def execute_in_chrome_container(container: docker.models.containers.Container, command: list) -> str:
    """
    Execute a command in a persistent Chrome container.

    Args:
        container: Docker container object
        command: Command to execute

    Returns:
        Command output as string
    """
    try:
        exec_result = container.exec_run(command, stdout=True, stderr=True)
        return exec_result.output.decode('utf-8', errors='ignore')
    except docker.errors.DockerException as e:
        logging.error(f"Failed to execute command in Chrome container: {e}")
        raise RuntimeError(f"Command execution failed: {e}")


def cleanup_chrome_container(client: docker.DockerClient, session_id: str):
    """
    Clean up a persistent Chrome container.

    Args:
        client: Docker client
        session_id: Session identifier
    """
    try:
        container_name = f"chrome-healing-{session_id}"
        container = client.containers.get(container_name)
        container.stop(timeout=10)
        container.remove()
        logging.info(f"Cleaned up Chrome container: {container_name}")
    except docker.errors.NotFound:
        logging.debug(
            f"Chrome container {container_name} not found (already cleaned up)")
    except docker.errors.DockerException as e:
        logging.warning(
            f"Failed to cleanup Chrome container {container_name}: {e}")


def get_healing_container_status(client: docker.DockerClient) -> Dict[str, Any]:
    """
    Get status of healing-related containers.

    Args:
        client: Docker client

    Returns:
        Dictionary with container status information
    """
    try:
        containers = client.containers.list(
            all=True, filters={"name": "chrome-healing-"})

        container_info = []
        for container in containers:
            container_info.append({
                "name": container.name,
                "status": container.status,
                "created": container.attrs.get('Created', 'Unknown'),
                "image": container.image.tags[0] if container.image.tags else 'Unknown'
            })

        return {
            "status": "success",
            "healing_containers": container_info,
            "total_containers": len(container_info)
        }

    except docker.errors.DockerException as e:
        logging.error(f"Failed to get healing container status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "healing_containers": [],
            "total_containers": 0
        }
