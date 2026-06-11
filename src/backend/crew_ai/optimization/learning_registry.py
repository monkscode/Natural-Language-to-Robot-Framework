"""
Learning Registry — Shared singleton factory for the learning system.

Single point of access for the FeedbackLoop singleton. Both
workflow_service.py and endpoints.py import from here, avoiding
coupling between API and service layers.

Pattern: thread-safe lazy init with retry-on-cooldown. The learning store is
PostgreSQL (a network dependency), so a failed init must NOT be cached forever
— e.g. the first workflow arriving before the Postgres container is healthy
would otherwise disable learning until the app restarts. A failure is cached
for _INIT_RETRY_COOLDOWN_S seconds (same 300s pattern as the embedder retry in
postgres_execution_memory), then the next call retries.

Threading safety:
- _registry_lock guards the check-and-set of the singleton global.
- Without the lock, two concurrent threads on cold start could both
  attempt init, creating two FeedbackLoop instances (two DB connections,
  two write queues). The second assignment would overwrite the first,
  orphaning its resources. The lock makes the entire init block atomic.
"""

import logging
import threading
import time

from src.backend.core.config import settings


# ---------------------------------------------------------------------------
# FeedbackLoop singleton (thread-safe, retry-on-cooldown)
# ---------------------------------------------------------------------------

# Lock guards the check-and-set of _feedback_loop_instance and
# _init_failed_at to prevent race conditions when multiple threads
# call get_feedback_loop() during cold start.
_registry_lock = threading.Lock()
_feedback_loop_instance = None
_init_failed_at: float | None = None  # time.monotonic() of the last failed init
_INIT_RETRY_COOLDOWN_S = 300


def _in_cooldown() -> bool:
    return (
        _init_failed_at is not None
        and time.monotonic() - _init_failed_at < _INIT_RETRY_COOLDOWN_S
    )


def get_feedback_loop():
    """Lazy singleton factory for FeedbackLoop.

    Thread-safe: uses _registry_lock to ensure only one thread
    performs initialization, even if multiple threads call this
    concurrently during cold start.

    On init failure (e.g. Postgres unreachable) returns None and caches the
    failure for _INIT_RETRY_COOLDOWN_S seconds, then retries — so a transient
    DB outage at startup degrades learning temporarily, not permanently.
    """
    global _feedback_loop_instance, _init_failed_at

    # Fast paths — no lock needed: object/None reads are atomic in CPython,
    # and a stale read here only costs one extra pass through the lock below.
    if _feedback_loop_instance is not None:
        return _feedback_loop_instance
    if not settings.OPTIMIZATION_ENABLED:
        return None
    if _in_cooldown():
        return None

    with _registry_lock:
        # Double-check inside the lock (another thread may have completed
        # init — or just failed — while we were waiting for it).
        if _feedback_loop_instance is not None:
            return _feedback_loop_instance
        if _in_cooldown():
            return None

        try:
            from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
            _feedback_loop_instance = FeedbackLoop()
            _init_failed_at = None
            logging.info("[LEARNING] Learning system initialized successfully")
        except Exception as e:
            _init_failed_at = time.monotonic()
            logging.warning(
                "[LEARNING] Learning system unavailable (non-blocking): %s — "
                "will retry in %ds", e, _INIT_RETRY_COOLDOWN_S,
            )

    return _feedback_loop_instance
