"""
Unit tests for _rotate_crewai_log() in src.backend.crew_ai.crew.

Purpose: verify thread-safety of the log rotation lock and correct behavior
         for the three key paths (rotate, skip, missing file).
"""

import os
import threading
import tempfile
import pytest
from unittest.mock import patch


class TestRotateCrewaiLog:
    """Tests for _rotate_crewai_log thread safety and behavior."""

    def test_concurrent_rotation_rotates_exactly_once(self, tmp_path):
        """10 threads call _rotate_crewai_log simultaneously; file is rotated exactly once."""
        from src.backend.crew_ai import crew as crew_module

        log_file = tmp_path / "crewai.log.txt"
        # Write content larger than 50MB limit
        log_file.write_bytes(b"x" * (51 * 1024 * 1024))

        backup_path = str(log_file) + ".1"
        errors = []
        rotation_count = [0]
        rotation_lock = threading.Lock()

        original_rename = os.rename

        def counting_rename(src, dst):
            if dst == backup_path:
                with rotation_lock:
                    rotation_count[0] += 1
            original_rename(src, dst)

        with patch.object(crew_module, "CREWAI_LOG_FILE", str(log_file)), \
             patch("os.rename", side_effect=counting_rename):

            def worker():
                try:
                    crew_module._rotate_crewai_log()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors, f"Unexpected exceptions: {errors}"
        # Exactly one rotation must have occurred
        assert rotation_count[0] == 1

    def test_below_threshold_skips_rotation(self, tmp_path):
        """File smaller than 50MB → rotation is skipped."""
        from src.backend.crew_ai import crew as crew_module

        log_file = tmp_path / "crewai.log.txt"
        log_file.write_bytes(b"x" * 1024)  # 1KB — well below 50MB

        with patch.object(crew_module, "CREWAI_LOG_FILE", str(log_file)):
            crew_module._rotate_crewai_log()

        # File must still exist (not rotated)
        assert log_file.exists()
        assert not (tmp_path / "crewai.log.txt.1").exists()

    def test_missing_log_file_returns_cleanly(self, tmp_path):
        """Missing log file → function returns without error."""
        from src.backend.crew_ai import crew as crew_module

        nonexistent = str(tmp_path / "crewai.log.txt")

        # Should not raise
        with patch.object(crew_module, "CREWAI_LOG_FILE", nonexistent):
            crew_module._rotate_crewai_log()
