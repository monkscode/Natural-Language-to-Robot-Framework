"""The bench must leave no test rows behind.

Bench data reaching the Tests page is the detachment failure run_bench.py's
docstring exists to prevent.
"""
import inspect


def test_the_detach_step_deletes_the_tests_it_creates():
    import bench.run_bench as rb
    src = inspect.getsource(rb)
    assert "DELETE FROM tests" in src, (
        "run_bench must delete the tests rows a bench run creates")


def test_the_capture_step_saves_the_new_tables_as_evidence():
    import bench.run_bench as rb
    src = inspect.getsource(rb)
    assert "tests.json" in src and "test_versions.json" in src


def test_the_docstring_still_describes_what_is_detached():
    import bench.run_bench as rb
    assert "test_versions" in rb.__doc__
