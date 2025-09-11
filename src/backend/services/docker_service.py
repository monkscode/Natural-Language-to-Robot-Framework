import os
import docker
import logging
from typing import Generator, Dict, Any

IMAGE_TAG = "robot-test-runner:latest"
DOCKERFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
ROBOT_TESTS_DIR = os.path.join(DOCKERFILE_PATH, 'robot_tests')

def get_docker_client():
    try:
        client = docker.from_env()
        client.ping()
        return client
    except docker.errors.DockerException as e:
        logging.error(f"Docker connection failed: {e}")
        raise ConnectionError(f"Docker is not available. Please ensure Docker Desktop is installed and running. Details: {e}")

def build_image(client: docker.DockerClient) -> Generator[Dict[str, Any], None, None]:
    try:
        client.images.get(IMAGE_TAG)
        logging.info(f"Docker image '{IMAGE_TAG}' already exists. Skipping build.")
        yield {"status": "running", "message": "Using existing container image for test execution..."}
        return
    except docker.errors.ImageNotFound:
        logging.info(f"Docker image '{IMAGE_TAG}' not found. Building new image.")
        yield {"status": "running", "message": "Building container image for test execution (first time only)..."}
        try:
            build_logs = client.api.build(path=DOCKERFILE_PATH, tag=IMAGE_TAG, rm=True, decode=True)
            for log in build_logs:
                if 'stream' in log:
                    log_message = log['stream'].strip()
                    if log_message:
                        yield {"status": "running", "log": log_message}
                if 'error' in log:
                    logging.error(f"Docker build error: {log['error']}")
                    raise docker.errors.BuildError(log['error'], build_log=build_logs)
            logging.info(f"Successfully built Docker image '{IMAGE_TAG}'.")
            yield {"status": "running", "message": "Container image built successfully!"}
        except docker.errors.BuildError as e:
            logging.error(f"Failed to build Docker image: {e}")
            raise

def run_test_in_container(client: docker.DockerClient, run_id: str, test_filename: str) -> Dict[str, Any]:
    try:
        robot_command = ["robot", "--outputdir", f"/app/robot_tests/{run_id}", f"/app/robot_tests/{run_id}/{test_filename}"]
        container_logs = client.containers.run(
            image=IMAGE_TAG,
            command=robot_command,
            volumes={os.path.abspath(ROBOT_TESTS_DIR): {'bind': '/app/robot_tests', 'mode': 'rw'}},
            working_dir="/app",
            stderr=True,
            stdout=True,
            detach=False,
            auto_remove=True
        )
        logs = container_logs.decode('utf-8')
        message = "Test execution finished: All tests passed."
        log_html_path = f"/reports/{run_id}/log.html"
        report_html_path = f"/reports/{run_id}/report.html"
        return {"status": "complete", "message": message, "result": {'logs': logs, 'log_html': log_html_path, 'report_html': report_html_path}}
    except docker.errors.ContainerError as e:
        logs = e.container.logs().decode('utf-8', errors='ignore')
        if os.path.exists(os.path.join(ROBOT_TESTS_DIR, run_id, "log.html")):
            logging.warning(f"Robot test execution finished with failures (exit code {e.exit_status}).")
            report_html_url = f"/reports/{run_id}/report.html"
            log_html_url = f"/reports/{run_id}/log.html"
            message = f"Test execution finished: Some tests failed (exit code {e.exit_status})."
            return {"status": "complete", "message": message, "result": {'logs': logs, 'log_html': log_html_url, 'report_html': report_html_url}}
        else:
            logging.error(f"Docker container failed before Robot Framework could generate a report (exit code {e.exit_status}).")
            error_logs = f"Docker container exited with a system error (exit code {e.exit_status}).\n"
            error_logs += "Robot Framework reports were not generated, indicating a problem with the test runner itself.\n\n"
            error_logs += f"Container Logs:\n{logs}"
            raise RuntimeError(error_logs)

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
