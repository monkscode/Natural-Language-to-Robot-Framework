"""
Unit tests for src.backend.core.temp_metrics_storage — TempMetricsStorage.

Purpose: TempMetricsStorage writes/reads browser-use metrics to temp JSON files,
         enabling the NL backend to merge them after workflow completion.
         A regression means metrics are lost or leftover temp files accumulate.

Tests:
  - Write + read round-trip returns same metrics
  - Read nonexistent workflow returns None
  - Cleanup removes old files
  - Cleanup preserves recent files
  - File path generation is predictable
"""

import json
import time
import pytest
from pathlib import Path

from src.backend.core.temp_metrics_storage import TempMetricsStorage


class TestTempMetricsRoundTrip:
    """Tests for writing and reading metrics."""

    def test_write_read_round_trip(self, tmp_metrics_dir):
        """Written metrics can be read back correctly."""
        storage = TempMetricsStorage(storage_dir=str(tmp_metrics_dir))
        metrics = {"total_input_tokens": 1000, "total_output_tokens": 500}

        assert storage.write_browser_metrics("wf-001", metrics) is True

        read_back = storage.read_browser_metrics("wf-001")
        assert read_back is not None
        assert read_back["total_input_tokens"] == 1000
        assert read_back["total_output_tokens"] == 500

    def test_read_nonexistent_returns_none(self, tmp_metrics_dir):
        """Reading a non-existent workflow returns None."""
        storage = TempMetricsStorage(storage_dir=str(tmp_metrics_dir))
        assert storage.read_browser_metrics("nonexistent") is None


class TestTempMetricsCleanup:
    """Tests for file cleanup."""

    def test_cleanup_old_files(self, tmp_metrics_dir):
        """Files older than max_age_hours are deleted."""
        storage = TempMetricsStorage(storage_dir=str(tmp_metrics_dir))

        # Create an old file (modify mtime to 25 hours ago)
        old_file = tmp_metrics_dir / "old-wf.json"
        old_file.write_text('{"metrics": {}}')
        import os
        old_time = time.time() - (25 * 3600)
        os.utime(old_file, (old_time, old_time))

        storage.cleanup_old_files(max_age_hours=24)
        assert not old_file.exists()

    def test_cleanup_preserves_recent(self, tmp_metrics_dir):
        """Files newer than max_age_hours are kept."""
        storage = TempMetricsStorage(storage_dir=str(tmp_metrics_dir))
        storage.write_browser_metrics("recent-wf", {"tokens": 100})

        storage.cleanup_old_files(max_age_hours=24)
        assert storage.read_browser_metrics("recent-wf") is not None


class TestTempMetricsFilePath:
    """Tests for file path generation."""

    def test_file_path_uses_workflow_id(self, tmp_metrics_dir):
        """Generated file path includes workflow ID."""
        storage = TempMetricsStorage(storage_dir=str(tmp_metrics_dir))
        path = storage._get_file_path("wf-123")
        assert "wf-123.json" in str(path)
