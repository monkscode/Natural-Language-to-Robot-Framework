"""
Learning Registry — Shared singleton factory for the learning system.

Single point of access for the FeedbackLoop singleton. Both
workflow_service.py and endpoints.py import from here, avoiding
coupling between API and service layers.

Pattern: thread-safe lazy init with try-once caching.  If initialization
fails (e.g. SQLite/ChromaDB unavailable), the failure is cached so
subsequent calls return immediately without retrying.

Threading safety:
- _registry_lock guards the check-and-set of the singleton global.
- Without the lock, two concurrent threads on cold start could both
  pass the `if _init_attempted` guard before either sets it to True,
  creating two FeedbackLoop instances (two DB connections, two write
  queues). The second assignment would overwrite the first, orphaning
  its resources. The lock makes the entire init block atomic.
"""

import logging
import threading

from src.backend.core.config import settings


# ---------------------------------------------------------------------------
# FeedbackLoop singleton (thread-safe)
# ---------------------------------------------------------------------------

# Lock guards the check-and-set of _feedback_loop_instance and
# _feedback_loop_init_attempted to prevent race conditions when
# multiple threads call get_feedback_loop() during cold start.
_registry_lock = threading.Lock()
_feedback_loop_instance = None
_feedback_loop_init_attempted = False


def get_feedback_loop():
    """Lazy singleton factory for FeedbackLoop.

    Thread-safe: uses _registry_lock to ensure only one thread
    performs initialization, even if multiple threads call this
    concurrently during cold start.

    Tries initialization once.  If it fails (e.g. SQLite/ChromaDB
    unavailable), caches None so subsequent calls return immediately
    without retrying the same broken path.
    """
    global _feedback_loop_instance, _feedback_loop_init_attempted

    # Fast path: already initialized (no lock needed for read of
    # bool + object reference — both are atomic in CPython, and
    # _init_attempted is only ever set True→True after first init)
    if _feedback_loop_init_attempted:
        return _feedback_loop_instance

    # Slow path: first call — acquire lock to ensure single init
    with _registry_lock:
        # Double-check inside lock (another thread may have completed
        # init while we were waiting for the lock)
        if _feedback_loop_init_attempted:
            return _feedback_loop_instance

        _feedback_loop_init_attempted = True

        if not settings.OPTIMIZATION_ENABLED:
            logging.info("[LEARNING] Learning system disabled (OPTIMIZATION_ENABLED=False)")
            return None

        try:
            from src.backend.crew_ai.optimization.feedback_loop import FeedbackLoop
            _feedback_loop_instance = FeedbackLoop()
            logging.info("[LEARNING] Learning system initialized successfully")
        except Exception as e:
            logging.warning(f"[LEARNING] Learning system unavailable (non-blocking): {e}")
            _feedback_loop_instance = None

    return _feedback_loop_instance
