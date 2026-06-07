"""
Coverage tests for workflow_service.py — targeting 9 missing lines.

Tests:
- _SlotReleaser.done(): no-op when _count is already 0 (prevents double-release)
- _store_hint_metadata: TTL eviction purges entries older than _HINT_CACHE_TTL_SECONDS
- _store_hint_metadata: size cap eviction removes oldest entries when over _HINT_CACHE_MAX_SIZE
"""

from datetime import datetime, timezone
from unittest.mock import patch

import src.backend.services.workflow_service as ws
from src.backend.services.workflow_service import _SlotReleaser, _store_hint_metadata


# ---------------------------------------------------------------------------
# _SlotReleaser.done() — no-op after slot released
# ---------------------------------------------------------------------------

class TestSlotReleaserDoneNoOp:
    def test_done_releases_slot_exactly_once(self):
        """done() called after _count reaches 0 must not trigger _release_workflow_slot again."""
        released_calls = []

        with patch(
            "src.backend.services.workflow_service._release_workflow_slot",
            side_effect=lambda: released_calls.append(1),
        ):
            releaser = _SlotReleaser(participant_count=1)
            releaser.done()  # _count → 0, slot released (call 1)
            releaser.done()  # _count <= 0, returns early — no second call
            releaser.done()  # same guard

        assert len(released_calls) == 1

    def test_two_participant_latch_releases_once(self):
        """Two-party latch: slot released only when BOTH participants call done()."""
        released_calls = []

        with patch(
            "src.backend.services.workflow_service._release_workflow_slot",
            side_effect=lambda: released_calls.append(1),
        ):
            releaser = _SlotReleaser(participant_count=2)
            releaser.done()   # _count → 1, slot NOT yet released
            assert released_calls == []
            releaser.done()   # _count → 0, slot released
            assert released_calls == [1]
            releaser.done()   # extra call — no-op guard
            assert released_calls == [1]  # still exactly once


# ---------------------------------------------------------------------------
# _store_hint_metadata — TTL eviction
# ---------------------------------------------------------------------------

class TestStoreHintMetadataTTLEviction:
    def setup_method(self):
        with ws._hint_metadata_lock:
            ws._hint_metadata_cache.clear()

    def teardown_method(self):
        with ws._hint_metadata_lock:
            ws._hint_metadata_cache.clear()

    def test_expired_entries_removed_before_insert(self):
        """Entries older than _HINT_CACHE_TTL_SECONDS are purged when a new entry is stored."""
        now = datetime.now(tz=timezone.utc).timestamp()
        expired_id = "workflow-expired"
        fresh_id = "workflow-fresh"

        with ws._hint_metadata_lock:
            # expired: stored more than TTL seconds ago
            ws._hint_metadata_cache[expired_id] = {
                "_stored_at": now - ws._HINT_CACHE_TTL_SECONDS - 1,
            }
            # fresh: stored just now
            ws._hint_metadata_cache[fresh_id] = {
                "_stored_at": now,
            }

        _store_hint_metadata("new-wf", {"agents": {}})

        with ws._hint_metadata_lock:
            assert expired_id not in ws._hint_metadata_cache
            assert fresh_id in ws._hint_metadata_cache
            assert "new-wf" in ws._hint_metadata_cache

    def test_non_expired_entries_survive(self):
        """Entries within TTL are not touched."""
        now = datetime.now(tz=timezone.utc).timestamp()

        with ws._hint_metadata_lock:
            for i in range(3):
                ws._hint_metadata_cache[f"keep-{i}"] = {
                    "_stored_at": now - 60,  # 60 s ago — well within TTL
                }

        _store_hint_metadata("new-wf-2", {"agents": {}})

        with ws._hint_metadata_lock:
            for i in range(3):
                assert f"keep-{i}" in ws._hint_metadata_cache


# ---------------------------------------------------------------------------
# _store_hint_metadata — size cap eviction
# ---------------------------------------------------------------------------

class TestStoreHintMetadataSizeCap:
    def setup_method(self):
        with ws._hint_metadata_lock:
            ws._hint_metadata_cache.clear()

    def teardown_method(self):
        with ws._hint_metadata_lock:
            ws._hint_metadata_cache.clear()

    def test_oldest_entry_evicted_when_at_max_size(self):
        """Inserting beyond _HINT_CACHE_MAX_SIZE evicts the entry with the smallest _stored_at."""
        original_max = ws._HINT_CACHE_MAX_SIZE
        ws._HINT_CACHE_MAX_SIZE = 3
        try:
            now = datetime.now(tz=timezone.utc).timestamp()
            with ws._hint_metadata_lock:
                ws._hint_metadata_cache["oldest"] = {"_stored_at": now + 1}
                ws._hint_metadata_cache["middle"] = {"_stored_at": now + 2}
                ws._hint_metadata_cache["newest"] = {"_stored_at": now + 3}

            # Cache is at cap (3). One more → "oldest" should be evicted.
            _store_hint_metadata("incoming", {"agents": {}})

            with ws._hint_metadata_lock:
                assert "oldest" not in ws._hint_metadata_cache
                assert "middle" in ws._hint_metadata_cache
                assert "newest" in ws._hint_metadata_cache
                assert "incoming" in ws._hint_metadata_cache
                assert len(ws._hint_metadata_cache) == ws._HINT_CACHE_MAX_SIZE
        finally:
            ws._HINT_CACHE_MAX_SIZE = original_max

    def test_size_cap_evicts_multiple_when_overflow_exceeds_one(self):
        """If the cache grows past cap by more than 1, ALL overflowing entries are removed."""
        original_max = ws._HINT_CACHE_MAX_SIZE
        ws._HINT_CACHE_MAX_SIZE = 2
        try:
            now = datetime.now(tz=timezone.utc).timestamp()
            with ws._hint_metadata_lock:
                ws._hint_metadata_cache["a"] = {"_stored_at": now + 1}  # oldest
                ws._hint_metadata_cache["b"] = {"_stored_at": now + 2}
                ws._hint_metadata_cache["c"] = {"_stored_at": now + 3}  # newest of pre-existing

            # overflow = len(3) - max(2) + 1 = 2; evicts "a" and "b"
            _store_hint_metadata("new", {"agents": {}})

            with ws._hint_metadata_lock:
                assert "a" not in ws._hint_metadata_cache
                assert "b" not in ws._hint_metadata_cache
                assert "c" in ws._hint_metadata_cache
                assert "new" in ws._hint_metadata_cache
                assert len(ws._hint_metadata_cache) == ws._HINT_CACHE_MAX_SIZE
        finally:
            ws._HINT_CACHE_MAX_SIZE = original_max
