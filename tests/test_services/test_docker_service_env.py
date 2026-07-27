"""docker_service must see src/backend/.env values regardless of import order.

Root cause being pinned: docker_service reads TEST_RUNNER_IMAGE_TAG (and
friends) via os.getenv at MODULE IMPORT time, while load_dotenv("src/backend/.env")
only runs when src.backend.core.config is first imported. A process that
imports docker_service before config (runner_exec.app did exactly this) gets
the hard-coded defaults silently — it only ever worked under run.sh because
the shell exported the whole .env first. The fix makes docker_service import
config before its own env reads, so the module is correct in ANY process.

These tests re-import the real modules against a temp CWD + temp .env, then
restore the original module objects so the rest of the suite is untouched.
"""

import os
import sys
from contextlib import contextmanager
from importlib import import_module

_MODULES = ("src.backend.services.docker_service", "src.backend.core.config")


@contextmanager
def _fresh_import_with_env_file(tmp_path, monkeypatch, env_lines):
    """Yield a freshly-imported docker_service whose CWD .env is env_lines.

    Simulating a bare process needs more than popping sys.modules: Python's
    `from package import name` resolves via getattr(package, name) first, so
    the parent packages' attributes must be cleared too or the old module
    objects are silently reused without re-executing load_dotenv.
    """
    env_file = tmp_path / "src" / "backend" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    core_pkg = import_module("src.backend.core")
    services_pkg = import_module("src.backend.services")
    saved_attrs = [
        (core_pkg, "config", getattr(core_pkg, "config", None)),
        (services_pkg, "docker_service", getattr(services_pkg, "docker_service", None)),
    ]
    saved = {name: sys.modules.get(name) for name in _MODULES}
    saved_env = {
        key: os.environ.pop(key, None)
        for key in ("TEST_RUNNER_IMAGE_TAG", "REMOTE_DOCKER_IMAGE",
                    "PREFER_REMOTE_DOCKER_IMAGE", "TEST_EXECUTION_TIMEOUT")
    }
    monkeypatch.chdir(tmp_path)
    for name in _MODULES:
        sys.modules.pop(name, None)
    for pkg, attr, _ in saved_attrs:
        if hasattr(pkg, attr):
            delattr(pkg, attr)
    try:
        yield import_module("src.backend.services.docker_service")
    finally:
        for name in _MODULES:
            sys.modules.pop(name, None)
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
        for pkg, attr, value in saved_attrs:
            if value is not None:
                setattr(pkg, attr, value)
            elif hasattr(pkg, attr):
                delattr(pkg, attr)
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)


class TestImportTimeEnvReads:
    def test_image_tag_comes_from_env_file_without_shell_export(
            self, tmp_path, monkeypatch):
        with _fresh_import_with_env_file(
                tmp_path, monkeypatch,
                ["TEST_RUNNER_IMAGE_TAG=hermetic-tag:test"]) as ds_mod:
            assert ds_mod.IMAGE_TAG == "hermetic-tag:test"

    def test_image_tag_default_when_env_file_lacks_it(
            self, tmp_path, monkeypatch):
        with _fresh_import_with_env_file(
                tmp_path, monkeypatch, ["UNRELATED=1"]) as ds_mod:
            assert ds_mod.IMAGE_TAG == "robot-test-runner:latest"

    def test_remote_image_default_contains_no_newline(
            self, tmp_path, monkeypatch):
        # 'monkscode\nlrf:...' — \n in a plain string is a NEWLINE, not a path
        # separator. The default must be a valid image reference.
        with _fresh_import_with_env_file(
                tmp_path, monkeypatch, ["UNRELATED=1"]) as ds_mod:
            assert "\n" not in ds_mod.REMOTE_IMAGE
            assert ds_mod.REMOTE_IMAGE == "monkscode/nlrf:test-runner-latest"
