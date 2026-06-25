import pytest
from src.backend.runner_exec.validation import safe_run_id, safe_test_filename


@pytest.mark.parametrize("good", ["abc123", "a1b2-c3d4", "run_001", "9f8e7d6c5b4a"])
def test_safe_run_id_accepts_plain_components(good):
    assert safe_run_id(good) == good


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "a\\b", ".", "..", "a b", "a;b", "x" * 200])
def test_safe_run_id_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        safe_run_id(bad)


def test_safe_test_filename_accepts_robot():
    assert safe_test_filename("test.robot") == "test.robot"


@pytest.mark.parametrize("bad", ["test.py", "../test.robot", "a/b.robot", "test.robot ", ".robot", ""])
def test_safe_test_filename_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        safe_test_filename(bad)
