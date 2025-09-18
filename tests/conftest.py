"""
Pytest configuration and shared fixtures for the test suite.
"""

import pytest
import sys
import os
from pathlib import Path

# Add the backend source to Python path for testing
backend_path = Path(__file__).parent.parent / "src" / "backend"
sys.path.insert(0, str(backend_path))

@pytest.fixture(scope="session")
def backend_path():
    """Provide the backend source path for tests."""
    return backend_path

@pytest.fixture
def temp_robot_tests_dir(tmp_path):
    """Create a temporary robot_tests directory for testing."""
    robot_tests_dir = tmp_path / "robot_tests"
    robot_tests_dir.mkdir()
    return robot_tests_dir

@pytest.fixture
def sample_test_run_id():
    """Generate a unique test run ID."""
    import uuid
    return f"test-{uuid.uuid4().hex[:8]}"

@pytest.fixture(autouse=True)
def setup_test_logging():
    """Set up logging for tests."""
    import logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )