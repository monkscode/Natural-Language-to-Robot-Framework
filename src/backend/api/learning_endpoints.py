"""
Learning Dashboard API endpoints (/api/learning/*).

Admin curation surface for the Adaptive Learning System.
Pairs with the /learning frontend SPA (Step 10).

Write design note: admin write endpoints open a fresh Postgres connection
(via pg_compat) rather than routing through LearningWriteQueue. This is
intentional:
  - LearningWriteQueue's "sole writer" invariant targets automated
    pipeline writes (frequent, must not block the workflow thread).
  - Admin writes are synchronous by design (UI needs an immediate
    response) and human-speed (not high-frequency).
  - Postgres MVCC handles any momentary contention with the write-queue
    thread safely — no SQLITE_BUSY, so no WAL/busy_timeout needed.

Referenced by: main.py (router registration, prefix="/api/learning")
Depends on: execution_memory.py, feedback_loop.py, learning_registry.py
"""

import json
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.backend.auth.jwt_utils import require_user, require_admin, is_validated_admin
from src.backend.auth.ownership import is_dashboard_viewer
from src.backend.crew_ai.optimization.learning_registry import get_feedback_loop
from src.backend.crew_ai.optimization import pg_compat
from src.backend.core.config import settings
from src.backend.crew_ai.optimization.learning_config import (
    MAX_FEEDBACK_TEXT_CHARS,
    _get_conflict_detection_model,
    _get_conflict_detection_completion_kwargs,
    _call_conflict_detection_llm,
    _parse_review_response,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost using LiteLLM's built-in model cost database.

    LiteLLM's model_cost dict is updated with each LiteLLM release, so this
    function requires no maintenance as models are added or repriced — just
    bump the litellm pin when pricing changes matter.

    Returns 0.0 silently if the model is unknown to LiteLLM.
    """
    try:
        import litellm
        input_cost, output_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
        return input_cost + output_cost
    except Exception:
        return 0.0


def _model_rate_info(model: str) -> dict | None:
    """Return per-token rate metadata for UI display, or None if unknown.

    Reads from LiteLLM's model_cost dict — same source as _estimate_cost.
    Returns input/output rates scaled to per-million-tokens for readability.
    """
    try:
        import litellm
        info = litellm.model_cost.get(model)
        if not info:
            return None
        inp = info.get("input_cost_per_token", 0)
        out = info.get("output_cost_per_token", 0)
        return {
            "input_cost_per_1m_tokens": round(inp * 1_000_000, 6),
            "output_cost_per_1m_tokens": round(out * 1_000_000, 6),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _require_feedback_loop():
    """FastAPI dependency — raises 503 if learning system is disabled."""
    fb = get_feedback_loop()
    if fb is None:
        raise HTTPException(
            status_code=503,
            detail="Learning system is not enabled (OPTIMIZATION_ENABLED=false)",
        )
    return fb


def _admin_conn():
    """Open a fresh write-capable Postgres connection (via pg_compat) for admin ops.

    Admin writes use their own short-lived connection rather than the
    LearningWriteQueue: they are synchronous (the UI needs an immediate
    response) and human-speed. On Postgres, MVCC handles any concurrency
    with the pipeline's writer thread — no SQLITE_BUSY, so no WAL/busy_timeout
    needed. SQLite-dialect SQL (`?` placeholders, sqlite3.Row-style access,
    `last_insert_rowid()`) runs unchanged through the pg_compat adapter.
    """
    return pg_compat.connect(settings.DATABASE_URL)


# ---------------------------------------------------------------------------
# Request body models
# ---------------------------------------------------------------------------

class HintCreateRequest(BaseModel):
    feedback_text: str
    anchor_query: str                       # example user request the hint applies to
    scope: str                              # url | domain | global
    org_id: str                             # owning org — hints never cross one
    domain: str | None = None
    url: str | None = None
    category: str | None = None
    original_failure_category: str | None = None
    run_triage: bool = False
    actor: str


class HintPatchRequest(BaseModel):
    scope: str | None = None
    domain: str | None = None
    url: str | None = None
    category: str | None = None
    original_failure_category: str | None = None
    actor: str
    reason: str | None = None
    feedback_text: str | None = None        # presence → HTTP 400


class ActorReasonRequest(BaseModel):
    actor: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict | None:
    return dict(row) if row else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_actor(admin: dict | None, request_actor: str | None = None) -> str:
    """Audit identity. ALWAYS the authenticated user's email when a token is
    present — in the live app require_admin guards this router, so a verified
    token always exists and the client-supplied actor is never trusted. The
    request_actor fallback only engages in tests (bare-router apps without a
    token, or endpoint functions called directly — where `admin` is the
    un-resolved Depends sentinel, hence the isinstance check)."""
    if not isinstance(admin, dict):
        admin = None
    return (admin or {}).get("email") or (request_actor or "").strip() or "admin"


def _write_hint_audit(
    conn,  # pg_compat.CompatConnection (sqlite3-Connection-like)
    hint_id: int,
    action: str,
    actor: str,
    reason: str | None,
    before: dict | None,
    after: dict | None,
):
    conn.execute(
        "INSERT INTO hint_audit "
        "(hint_id, action, actor, reason, before_value, after_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            hint_id, action, actor, reason,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            _now(),
        ),
    )


# ---------------------------------------------------------------------------
# 1. GET /hints
# ---------------------------------------------------------------------------

@router.get("/hints")
def list_hints(
    status: str | None = Query(None, description="flagged|active|auto_disabled|retracted|llm_review_disabled"),
    scope: str | None = Query(None),
    domain: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    fb=Depends(_require_feedback_loop),
    user: dict | None = Depends(require_user),
):
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org: str | None = None if (admin or user is None) else user.get("org_id")

    conn = fb.execution_memory.get_read_connection()
    try:
        conditions = []
        params: list = []

        # Org scope: a hint belongs to exactly one org, so a non-platform-admin
        # sees their own org's and nothing else.
        if scope_org is not None:
            conditions.append("org_id = ?")
            params.append(scope_org)

        if status == "flagged":
            conditions.append("is_active=1 AND conflict_flagged=1")
        elif status == "active":
            conditions.append("is_active=1 AND conflict_flagged=0")
        elif status == "retracted":
            conditions.append(
                "is_active=0 AND EXISTS("
                "SELECT 1 FROM hint_audit ha "
                "WHERE ha.hint_id=nl_feedback_corrections.id AND ha.action='retract')"
            )
        elif status == "auto_disabled":
            conditions.append(
                "is_active=0 AND NOT EXISTS("
                "SELECT 1 FROM hint_audit ha "
                "WHERE ha.hint_id=nl_feedback_corrections.id "
                "AND ha.action IN ('retract', 'llm_review_disable'))"
            )
        elif status == "llm_review_disabled":
            conditions.append(
                "is_active=0 "
                "AND NOT EXISTS(SELECT 1 FROM hint_audit ha "
                "  WHERE ha.hint_id=nl_feedback_corrections.id AND ha.action='retract') "
                "AND EXISTS(SELECT 1 FROM hint_audit ha "
                "  WHERE ha.hint_id=nl_feedback_corrections.id AND ha.action='llm_review_disable')"
            )
        # else: no status filter (all)

        if scope in ("url", "domain", "global"):
            conditions.append("scope = ?")
            params.append(scope)

        if domain:
            # Domains are stored lowercased (extract_domain), but admin-created
            # hints may predate that — compare case-insensitively on both sides.
            conditions.append("LOWER(domain) = LOWER(?)")
            params.append(domain)

        if search:
            # ILIKE: SQLite LIKE was case-insensitive; keep that behaviour on Postgres.
            conditions.append("feedback_text ILIKE ?")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Status-priority expression: Flagged=1, Active=2, Auto-disabled=3, Retracted=4
        sort_priority = (
            "CASE "
            "WHEN is_active=1 AND conflict_flagged=1 THEN 1 "
            "WHEN is_active=1 AND conflict_flagged=0 THEN 2 "
            "WHEN is_active=0 AND EXISTS("
            "  SELECT 1 FROM hint_audit ha2 "
            "  WHERE ha2.hint_id=nl_feedback_corrections.id AND ha2.action='retract'"
            ") THEN 4 "
            "ELSE 3 END"
        )

        # Per-row flag so the frontend can show "LLM-DISABLED" badge vs "AUTO-DISABLED".
        # Uses the same logic as the stats query and the llm_review_disabled filter above.
        llm_review_disabled_col = (
            "CASE WHEN is_active=0 "
            "AND NOT EXISTS(SELECT 1 FROM hint_audit ha3 "
            "  WHERE ha3.hint_id=nl_feedback_corrections.id AND ha3.action='retract') "
            "AND EXISTS(SELECT 1 FROM hint_audit ha4 "
            "  WHERE ha4.hint_id=nl_feedback_corrections.id AND ha4.action='llm_review_disable') "
            "THEN 1 ELSE 0 END"
        )

        total = conn.execute(
            f"SELECT COUNT(*) FROM nl_feedback_corrections {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT *, ({sort_priority}) AS sort_priority, "
            f"({llm_review_disabled_col}) AS llm_review_disabled "
            f"FROM nl_feedback_corrections {where} "
            f"ORDER BY sort_priority ASC, last_seen DESC "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "hints": [_row_to_dict(r) for r in rows],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. GET /hints/{id}
# ---------------------------------------------------------------------------

@router.get("/hints/{hint_id}")
def get_hint(
    hint_id: int,
    fb=Depends(_require_feedback_loop),
    user: dict | None = Depends(require_user),
):
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org: str | None = None if (admin or user is None) else user.get("org_id")

    conn = fb.execution_memory.get_read_connection()
    try:
        if scope_org is not None:
            row = conn.execute(
                "SELECT * FROM nl_feedback_corrections "
                "WHERE id = ? AND org_id = ?",
                (hint_id, scope_org),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hint {hint_id} not found")

        # The timeline is keyed by hint_id only — hint_audit and trigger_events
        # carry no org_id, so they cannot be org-scoped in SQL. The SELECT above
        # already restricts an org-scoped caller to their own org, so this is now
        # belt-and-braces rather than the sole gate it was while a shared hint
        # could be fetched from another org. Kept: the cost of being wrong is the
        # originating org's workflow ids, trigger reasons and audit actors, and
        # this is what still holds if that SELECT is ever widened again.
        timeline: list = []
        if scope_org is None or row["org_id"] == scope_org:
            audit_rows = conn.execute(
                "SELECT 'hint_audit' AS source, id, action, actor, reason, "
                "       before_value, after_value, created_at, "
                "       NULL AS trigger_type, NULL AS workflow_id "
                "FROM hint_audit WHERE hint_id = ?",
                (hint_id,),
            ).fetchall()

            trigger_rows = conn.execute(
                "SELECT 'trigger_events' AS source, id, "
                "       CASE "
                "         WHEN COALESCE(actually_flagged_hint_ids, flagged_hint_ids) "
                "              @> to_jsonb(?::int) THEN 'flagged' "
                "         ELSE 'flag_recommended_suppressed' "
                "       END AS action, "
                "       trigger_type AS actor, reason, "
                "       NULL AS before_value, NULL AS after_value, created_at, "
                "       trigger_type, workflow_id "
                "FROM trigger_events "
                "WHERE flagged_hint_ids IS NOT NULL "
                "  AND flagged_hint_ids <> '[]'::jsonb "
                "  AND flagged_hint_ids @> to_jsonb(?::int)",
                (hint_id, hint_id),
            ).fetchall()

            timeline = sorted(
                [_row_to_dict(r) for r in list(audit_rows) + list(trigger_rows)],
                key=lambda r: r["created_at"] or "",
                reverse=True,
            )

        return {"hint": _row_to_dict(row), "timeline": timeline}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. POST /hints  (admin add)
# ---------------------------------------------------------------------------

@router.post("/hints", status_code=201)
def create_hint(
    request: HintCreateRequest,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    if not request.actor.strip():
        raise HTTPException(status_code=400, detail="actor is required")
    # Every hint is owned by exactly one org. Generic guidance wanted in several
    # orgs is created once per org (the manual half of copy-on-promote), so each
    # copy keeps its own counters, flags and disables permanently.
    org_id = request.org_id.strip()
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id is required")
    actor = _audit_actor(admin, request.actor)
    text = request.feedback_text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="feedback_text is required")
    if len(text) > MAX_FEEDBACK_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"feedback_text must be ≤{MAX_FEEDBACK_TEXT_CHARS} characters")
    # anchor_query — the example user request this hint applies to. It is the
    # text the query-similarity filter embeds and matches future queries
    # against, so an admin hint without one would never be retrieved.
    anchor = request.anchor_query.strip()
    if len(anchor) < 3:
        raise HTTPException(
            status_code=400,
            detail="anchor_query is required (an example user request, ≥3 characters)",
        )
    if len(anchor) > 500:
        raise HTTPException(status_code=400, detail="anchor_query must be ≤500 characters")
    if request.scope not in ("url", "domain", "global"):
        raise HTTPException(status_code=400, detail="scope must be url, domain, or global")
    # F5: normalise before validating — whitespace-only is truthy in Python,
    # so `not request.domain`/`not request.url` alone let "   " through, and
    # scope='url' never validated domain at all, so domain='' passed
    # straight to storage unnormalised.
    domain = (request.domain or "").strip() or None
    url = (request.url or "").strip() or None
    if request.scope == "url" and not url:
        raise HTTPException(status_code=400, detail="url is required when scope is 'url'")
    if request.scope == "domain" and not domain:
        raise HTTPException(status_code=400, detail="domain is required when scope is 'domain'")

    category = request.category or "uncategorized"
    if request.run_triage and fb.nl_engine is not None:
        try:
            triage = fb.nl_engine.process_feedback("admin-add", text, "completely_wrong")
            category = triage.get("category", category)
        except Exception as e:
            logger.warning(
                "[LEARNING API] Admin triage failed for hint %r (non-blocking, "
                "using provided/default category %r): %s",
                text[:50], category, e,
            )

    now = _now()
    conn = _admin_conn()
    try:
        # BEGIN IMMEDIATE takes no lock on Postgres — pg_compat._is_noop
        # swallows it — so this SELECT does NOT block a concurrent identical
        # create, and the dedup check below is advisory only. The real guard
        # is the uq_nlfc_dedup_* unique index, which the INSERT's
        # IntegrityError handler turns into a retriable 409 (see there). The
        # line stays for SQLite-dialect fidelity, as elsewhere in this module.
        conn.execute("BEGIN IMMEDIATE")
        # Dedup within the TARGET org, matching uq_nlfc_dedup_* and the engine's
        # own key. Identical text in another org is a separate hint by design —
        # that is what keeps the two copies' counters independent.
        #
        # url-scoped hints key on url too, mirroring uq_nlfc_dedup_url_v21 and
        # NLFeedbackEngine.learn_from_feedback: identical text on two pages of
        # the same domain is two hints, one per page. Without the url predicate
        # the second page's create bumps evidence on the FIRST page's hint and
        # the hint the admin asked for is never written.
        #
        # global-scoped hints drop domain, mirroring uq_nlfc_dedup_global_v21:
        # a global hint applies to every query in the org, so the page it was
        # typed on is not part of its identity. Keeping domain here would make
        # this SELECT narrower than its index — the duplicate would go unfound
        # and the INSERT would 409 on the constraint instead of bumping
        # evidence on the hint that already says the same thing.
        if request.scope == "url":
            existing = conn.execute(
                "SELECT * FROM nl_feedback_corrections "
                "WHERE feedback_text = ? AND domain IS NOT DISTINCT FROM ? "
                "AND url IS NOT DISTINCT FROM ? AND scope = ? "
                "AND org_id IS NOT DISTINCT FROM ?",
                (text, domain, url, request.scope, org_id),
            ).fetchone()
        elif request.scope == "global":
            existing = conn.execute(
                "SELECT * FROM nl_feedback_corrections "
                "WHERE feedback_text = ? AND scope = 'global' "
                "AND org_id IS NOT DISTINCT FROM ?",
                (text, org_id),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT * FROM nl_feedback_corrections "
                "WHERE feedback_text = ? AND domain IS NOT DISTINCT FROM ? AND scope = ? "
                "AND org_id IS NOT DISTINCT FROM ?",
                (text, domain, request.scope, org_id),
            ).fetchone()

        if existing:
            # Duplicate text — bump evidence, reactivate, and clear any conflict flag.
            # Admin re-asserting identical text is at least as authoritative as a user
            # re-submit (which also clears conflict_flagged). If the LLM was right about
            # the flag, the next Trigger 1/2 will re-flag with fresh evidence.
            was_flagged = existing["conflict_flagged"] == 1
            conn.execute(
                "UPDATE nl_feedback_corrections "
                "SET evidence_count = evidence_count + 1, last_seen = ?, is_active = 1, "
                "    conflict_flagged = 0, conflict_flagged_at = NULL, "
                "    conflict_flag_reason = NULL "
                "WHERE id = ?",
                (now, existing["id"]),
            )
            if was_flagged:
                _write_hint_audit(
                    conn, existing["id"], "unflag", actor,
                    "Admin re-submitted existing hint — implicit unflag",
                    {"conflict_flagged": 1}, {"conflict_flagged": 0},
                )
            _write_hint_audit(
                conn, existing["id"], "reinforce", actor,
                "Admin re-submitted existing hint — evidence incremented",
                None, {"evidence_count": existing["evidence_count"] + 1},
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM nl_feedback_corrections WHERE id = ?", (existing["id"],)
            ).fetchone()
            return {"hint": _row_to_dict(row), "created": False}

        try:
            conn.execute(
                "INSERT INTO nl_feedback_corrections "
                "(feedback_text, category, scope, domain, url, original_failure_category, "
                " evidence_count, anchor_query, source_workflow_id, created_at, last_seen, "
                " created_via, org_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, 'admin', ?)",
                (
                    text, category, request.scope, domain, url,
                    request.original_failure_category, anchor, now, now, org_id,
                ),
            )
        except sqlite3.IntegrityError:
            # BEGIN IMMEDIATE is a no-op on Postgres, so the duplicate check
            # above no longer holds a write lock: a concurrent identical create
            # can land between the SELECT and this INSERT. The UNIQUE constraint
            # is the real guard — surface the loser as a retriable conflict,
            # not a 500.
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="An identical hint was just created — refresh to see it.",
            ) from None
        hint_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _write_hint_audit(
            conn, hint_id, "create", actor, None, None,
            {
                "feedback_text": text, "scope": request.scope,
                "domain": domain, "url": url,
                "category": category, "created_via": "admin",
                "org_id": org_id,
            },
        )
        conn.commit()

        # Embed this admin hint's anchor into learning_anchors. create_hint
        # runs on the FastAPI request thread, and add_anchor is writer-thread
        # only — so the add MUST go through the write queue. Best-effort: a
        # missed doc is healed by the next reconcile.
        try:
            # org_id matters: filter_by_query_similarity matches an org-less
            # anchor for EVERY calling org, so an unowned anchor would leave the
            # SQL gate as the only thing standing between orgs.
            fb.write_queue.submit(
                fb.execution_memory.add_anchor, "nl", hint_id, anchor,
                org_id=org_id,
            )
        except Exception as e:
            logger.warning(
                "[LEARNING API] anchor enqueue failed for hint %d "
                "(non-blocking): %s", hint_id, e,
            )

        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return {"hint": _row_to_dict(row), "created": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[LEARNING API] create_hint failed: %s", e, exc_info=True)
        # A busy_timeout expiry surfaces as sqlite3.OperationalError "database
        # is locked" — transient contention with another writer, not a bug.
        # Tell the admin it is retriable so the (still-open) form can be resubmitted.
        msg = str(e).lower()
        if isinstance(e, sqlite3.OperationalError) and ("lock" in msg or "busy" in msg):
            detail = "Database is busy — please retry in a moment."
        else:
            detail = "Failed to create hint"
        raise HTTPException(status_code=500, detail=detail)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. PATCH /hints/{id}
# ---------------------------------------------------------------------------

@router.patch("/hints/{hint_id}")
def patch_hint(
    hint_id: int,
    request: HintPatchRequest,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    if request.feedback_text is not None:
        raise HTTPException(
            status_code=400,
            detail="feedback_text cannot be edited in place — use retract + re-add",
        )
    if not request.actor.strip():
        raise HTTPException(status_code=400, detail="actor is required")
    actor = _audit_actor(admin, request.actor)

    conn = _admin_conn()
    try:
        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hint {hint_id} not found")

        row_dict = _row_to_dict(row)
        updates: dict = {}
        before: dict = {}

        # F5: normalise before anything below reads request.domain/request.url —
        # whitespace-only is truthy in Python, so the scope branch's
        # `resolved_url = request.url or row_dict.get("url")` fallback (and
        # the equivalent for domain) must see the normalised value or a
        # whitespace-only input overwrites the stored value with blank
        # instead of falling through to it.
        norm_domain = None
        if request.domain is not None:
            norm_domain = (request.domain or "").strip() or None
        norm_url = None
        if request.url is not None:
            norm_url = (request.url or "").strip() or None

        if request.scope is not None:
            if request.scope not in ("url", "domain", "global"):
                raise HTTPException(status_code=400, detail="scope must be url, domain, or global")
            # Narrower-scope validation with column retention
            if request.scope == "url":
                resolved_url = norm_url or row_dict.get("url")
                if not resolved_url:
                    raise HTTPException(
                        status_code=400,
                        detail="url required when changing scope to 'url'",
                    )
                before["url"] = row_dict.get("url")
                updates["url"] = resolved_url
            elif request.scope == "domain":
                resolved_domain = norm_domain or row_dict.get("domain")
                if not resolved_domain:
                    raise HTTPException(
                        status_code=400,
                        detail="domain required when changing scope to 'domain'",
                    )
                before["domain"] = row_dict.get("domain")
                updates["domain"] = resolved_domain
            before["scope"] = row_dict["scope"]
            updates["scope"] = request.scope

        # Explicit field updates (only if provided and not already set via scope logic)
        if request.url is not None and "url" not in updates:
            before["url"] = row_dict.get("url")
            updates["url"] = norm_url
        if request.domain is not None and "domain" not in updates:
            before["domain"] = row_dict.get("domain")
            updates["domain"] = norm_domain
        if request.category is not None:
            before["category"] = row_dict.get("category")
            updates["category"] = request.category
        if request.original_failure_category is not None:
            before["original_failure_category"] = row_dict.get("original_failure_category")
            updates["original_failure_category"] = request.original_failure_category

        # F5: the scope/target consistency check create_hint already has
        # (learning_endpoints.py:431-436) — the scope branch above already
        # enforces it when `scope` is itself in the request, but a domain/url
        # edit that leaves the CURRENT scope unchanged took the explicit-field
        # branch above and skipped it entirely. Validate the resulting row
        # regardless of which branch produced it.
        resolved_scope = request.scope if request.scope is not None else row_dict["scope"]
        if resolved_scope == "domain":
            final_domain = updates["domain"] if "domain" in updates else row_dict.get("domain")
            if not final_domain:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "this hint is domain-scoped but has no domain — "
                        "the patch must supply one"
                    ),
                )
        elif resolved_scope == "url":
            final_url = updates["url"] if "url" in updates else row_dict.get("url")
            if not final_url:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "this hint is url-scoped but has no url — "
                        "the patch must supply one"
                    ),
                )

        if not updates:
            return {"hint": row_dict, "changed": False}

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        try:
            conn.execute(
                f"UPDATE nl_feedback_corrections SET {set_clause} WHERE id = ?",
                list(updates.values()) + [hint_id],
            )
        except sqlite3.IntegrityError:
            # scope/domain/url ARE the dedup key, so an edit can land on another
            # hint's key — reachable from the drawer, which sends all three on
            # every save. That is a conflict the admin can resolve, not a server
            # fault, and create_hint already answers 409 for these same indexes.
            # The only IntegrityError this UPDATE can raise is one of them: it
            # never touches the primary key, scope is validated to one of three
            # literals above, and every other column it writes is nullable.
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Another hint in this org already matches this one's text "
                    "and target. Retract the duplicate, or choose a different "
                    "scope/domain/url."
                ),
            ) from None
        after = dict(updates)
        # A patch may change any combination of scope/url/domain/category — a
        # single action verb cannot name all of them, and before/after already
        # records exactly which fields changed. Use a neutral label rather than
        # mislabel a url- or domain-only patch as a category change.
        _write_hint_audit(conn, hint_id, "patch", actor, request.reason, before, after)
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return {"hint": _row_to_dict(updated), "changed": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[LEARNING API] patch_hint %d failed: %s", hint_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update hint")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. POST /hints/{id}/unflag
# ---------------------------------------------------------------------------

@router.post("/hints/{hint_id}/unflag")
def unflag_hint(
    hint_id: int,
    request: ActorReasonRequest,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    if not request.actor.strip():
        raise HTTPException(status_code=400, detail="actor is required")
    actor = _audit_actor(admin, request.actor)

    conn = _admin_conn()
    try:
        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hint {hint_id} not found")

        if row["conflict_flagged"] == 0:
            return {"hint": _row_to_dict(row), "changed": False, "note": "hint was not flagged"}

        conn.execute(
            "UPDATE nl_feedback_corrections "
            "SET conflict_flagged=0, conflict_flagged_at=NULL, conflict_flag_reason=NULL "
            "WHERE id = ?",
            (hint_id,),
        )
        _write_hint_audit(
            conn, hint_id, "unflag", actor, request.reason,
            {"conflict_flagged": 1}, {"conflict_flagged": 0},
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return {"hint": _row_to_dict(updated), "changed": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[LEARNING API] unflag_hint %d failed: %s", hint_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to unflag hint")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5b. POST /hints/{id}/promote — REMOVED (2026-07-02 access-control review).
# Promotion flipped an org-learned hint to a shared one and nulled its anchor
# org, injecting it into every org whose runs match the hint's domain. Hints
# learned on one customer's site must never reach another customer, so the
# route was deleted to make that guarantee structural. The shared-visibility
# flag it set is itself gone now (schema v20): a hint belongs to exactly one
# org, and generic guidance wanted in several is CREATED once per org via
# POST /hints. test_learning_promote.py and test_hints_are_org_owned.py pin
# both halves.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. POST /hints/{id}/retract
# ---------------------------------------------------------------------------

@router.post("/hints/{hint_id}/retract")
def retract_hint(
    hint_id: int,
    request: ActorReasonRequest,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    if not request.actor.strip():
        raise HTTPException(status_code=400, detail="actor is required")
    actor = _audit_actor(admin, request.actor)

    conn = _admin_conn()
    try:
        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hint {hint_id} not found")

        if row["is_active"] == 0:
            return {"hint": _row_to_dict(row), "changed": False, "note": "hint was already retracted"}

        conn.execute(
            "UPDATE nl_feedback_corrections SET is_active=0, disabled_at=? WHERE id = ?",
            (_now(), hint_id),
        )
        _write_hint_audit(
            conn, hint_id, "retract", actor, request.reason,
            {"is_active": 1}, {"is_active": 0},
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return {"hint": _row_to_dict(updated), "changed": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[LEARNING API] retract_hint %d failed: %s", hint_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retract hint")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. POST /hints/{id}/reactivate
# ---------------------------------------------------------------------------

@router.post("/hints/{hint_id}/reactivate")
def reactivate_hint(
    hint_id: int,
    request: ActorReasonRequest,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    if not request.actor.strip():
        raise HTTPException(status_code=400, detail="actor is required")
    actor = _audit_actor(admin, request.actor)

    conn = _admin_conn()
    try:
        row = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Hint {hint_id} not found")

        if row["is_active"] == 1:
            return {"hint": _row_to_dict(row), "changed": False, "note": "hint was already active"}

        conn.execute(
            "UPDATE nl_feedback_corrections "
            "SET is_active=1, conflict_flagged=0, conflict_flagged_at=NULL, "
            # Step 4b: admin reactivation is a fresh chance — reset unused_count
            # so the hint is not immediately re-retired; success/failure kept.
            "    conflict_flag_reason=NULL, disabled_at=NULL, unused_count=0 WHERE id=?",
            (hint_id,),
        )
        _write_hint_audit(
            conn, hint_id, "reactivate", actor, request.reason,
            {"is_active": 0}, {"is_active": 1, "conflict_flagged": 0},
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return {"hint": _row_to_dict(updated), "changed": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[LEARNING API] reactivate_hint %d failed: %s", hint_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reactivate hint")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. GET /triggers
# ---------------------------------------------------------------------------

@router.get("/triggers")
def list_triggers(
    trigger_type: str | None = Query(None),
    since: str | None = Query(None),
    workflow_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    conn = fb.execution_memory.get_read_connection()
    try:
        conditions: list[str] = []
        params: list = []

        if trigger_type:
            conditions.append("trigger_type = ?")
            params.append(trigger_type)

        if since:
            if since.endswith("d"):
                try:
                    days = int(since[:-1])
                    conditions.append("created_at >= ?")
                    params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid since value: {since!r}")
            else:
                conditions.append("created_at >= ?")
                params.append(since)

        if workflow_id:
            conditions.append("workflow_id = ?")
            params.append(workflow_id)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM trigger_events {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT * FROM trigger_events {where} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "triggers": [_row_to_dict(r) for r in rows],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9. GET /triggers/{id}
# ---------------------------------------------------------------------------

@router.get("/triggers/{trigger_id}")
def get_trigger(
    trigger_id: int,
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    conn = fb.execution_memory.get_read_connection()
    try:
        row = conn.execute(
            "SELECT * FROM trigger_events WHERE id = ?", (trigger_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Trigger event {trigger_id} not found")

        row_dict = _row_to_dict(row)
        execution = None
        if row_dict.get("workflow_id"):
            exec_row = conn.execute(
                "SELECT robot_code, working_code, user_query, test_status, timestamp "
                "FROM execution_records WHERE workflow_id = ?",
                (row_dict["workflow_id"],),
            ).fetchone()
            execution = _row_to_dict(exec_row)

        # Resolve hint texts for all active_hint_ids in one query
        hint_texts: dict[int, str] = {}
        try:
            # active_hint_ids is jsonb → psycopg returns a parsed list (or None).
            active_ids = row_dict.get("active_hint_ids") or []
        except (ValueError, TypeError):
            active_ids = []
        if active_ids:
            placeholders = ",".join("?" * len(active_ids))
            hint_rows = conn.execute(
                f"SELECT id, feedback_text FROM nl_feedback_corrections WHERE id IN ({placeholders})",
                active_ids,
            ).fetchall()
            hint_texts = {r["id"]: r["feedback_text"] for r in hint_rows}

        return {"trigger": row_dict, "execution": execution, "hint_texts": hint_texts}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 9b. GET /runs  +  GET /runs/{workflow_id}  (per-testcase observability, N3)
# ---------------------------------------------------------------------------

@router.get("/runs")
def list_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    fb=Depends(_require_feedback_loop),
    user: dict | None = Depends(require_user),
):
    """Recent test-case runs (execution_records), newest first — the entry point
    for the per-run learning journey. Optional status + user_query-substring
    filters. A failed run has no trigger event, so this (not /triggers) is how a
    failure is found.
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org: str | None = None if (admin or user is None) else user.get("org_id")

    conn = fb.execution_memory.get_read_connection()
    try:
        where, params = [], []
        # Org scope: non-platform-admins see only their org's execution records.
        if scope_org is not None:
            where.append("org_id = ?")
            params.append(scope_org)
        if status:
            where.append("test_status = ?")
            params.append(status)
        if q:
            # ILIKE: SQLite LIKE was case-insensitive; keep that behaviour on Postgres.
            where.append("user_query ILIKE ?")
            params.append(f"%{q}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM execution_records {where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT workflow_id, timestamp, user_query, url, domain, test_status, "
            f"       failure_category, injected_hint_ids "
            f"FROM execution_records {where_sql} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        runs = []
        for r in rows:
            d = _row_to_dict(r)
            try:
                d["nl_injected_count"] = len(d.get("injected_hint_ids") or [])
            except (ValueError, TypeError):
                d["nl_injected_count"] = 0
            runs.append(d)
        return {"total": total, "limit": limit, "offset": offset, "runs": runs}
    finally:
        conn.close()


@router.get("/runs/{workflow_id}")
def get_run(
    workflow_id: str,
    fb=Depends(_require_feedback_loop),
    user: dict | None = Depends(require_user),
):
    """Everything the learning system recorded for one test-case run: the
    execution record, its metrics, the per-hint selection->attribution funnel
    (hint_workflow_trace LEFT JOIN the hint row for current text/state), and any
    trigger events. The funnel populates only for runs executed under Part 2; a
    run with no execution record (learning skipped, or a legacy row aggregated
    away before T1) keeps a standalone funnel with run=null (no FK). 404 only
    when nothing at all exists for this workflow_id.
    """
    admin = is_validated_admin(user)
    if not is_dashboard_viewer(user, is_platform_admin=admin):
        raise HTTPException(status_code=403, detail="Org-admin access required")
    scope_org: str | None = None if (admin or user is None) else user.get("org_id")

    conn = fb.execution_memory.get_read_connection()
    try:
        if scope_org is not None:
            run = _row_to_dict(conn.execute(
                "SELECT * FROM execution_records WHERE workflow_id = ? AND org_id = ?",
                (workflow_id, scope_org),
            ).fetchone())
        else:
            run = _row_to_dict(conn.execute(
                "SELECT * FROM execution_records WHERE workflow_id = ?", (workflow_id,)
            ).fetchone())
        # SCOPED EARLY EXIT: if this org has no execution record for this workflow_id,
        # return 404 immediately — before running metrics/trace/triggers queries.
        # hint_workflow_trace and learning_metrics have no org_id column, so they
        # cannot be org-scoped; serving them to a scoped caller whose execution_record
        # lookup returned None would leak another org's trace rows (D3 violation).
        # Platform-admins (scope_org=None) are unaffected: a trace-only run with
        # no execution record still returns 200 for platform admins.
        if scope_org is not None and run is None:
            raise HTTPException(
                status_code=404,
                detail=f"No run data for workflow {workflow_id}",
            )
        metrics = _row_to_dict(conn.execute(
            "SELECT * FROM learning_metrics WHERE workflow_id = ? "
            "ORDER BY timestamp DESC LIMIT 1", (workflow_id,)
        ).fetchone())
        # Funnel: the captured selection trace + the hint's CURRENT text/state.
        # LEFT JOIN so a since-deleted hint still shows its trace row (text NULL).
        trace = [_row_to_dict(r) for r in conn.execute(
            "SELECT t.hint_id, t.scope, t.source, t.priority, t.similarity_score, "
            "       t.available, t.injected, t.drop_reason, t.attribution_bucket, "
            "       t.attribution_reason, h.feedback_text, h.is_active, "
            "       h.conflict_flagged "
            "FROM hint_workflow_trace t "
            "LEFT JOIN nl_feedback_corrections h ON h.id = t.hint_id "
            "WHERE t.workflow_id = ? "
            "ORDER BY t.injected DESC, t.similarity_score DESC", (workflow_id,)
        ).fetchall()]
        triggers = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM trigger_events WHERE workflow_id = ? "
            "ORDER BY created_at DESC", (workflow_id,)
        ).fetchall()]
        if run is None and not trace and not triggers:
            raise HTTPException(
                status_code=404,
                detail=f"No run data for workflow {workflow_id}",
            )
        return {"run": run, "metrics": metrics, "trace": trace, "triggers": triggers}
    finally:
        conn.close()


# Part-2 review/dashboard signal thresholds (inform-only, tunable from the
# dashboard — NOT gates). FAILURE_ASSOCIATION_REVIEW_THRESHOLD doubles as a soft
# fairness floor (a brand-new hint cannot reach it yet).
FAILURE_ASSOCIATION_REVIEW_THRESHOLD = 3
NEVER_ATTRIBUTED_REVIEW_MIN_INJECTIONS = 5
NEVER_ATTRIBUTED_REVIEW_MIN_AGE_DAYS = 14


def _compute_failure_associations(conn, hint_ids: list) -> dict:
    """Step 5b: per hint id, count recent FAILING execution_records that injected
    the hint AND whose failure_category relates to the hint's own
    original_failure_category (via RELATED_CATEGORIES). Inform-only review signal
    so a harmful never-passing hint is visible without a manual review trigger.

    SQL jsonb hybrid: a bounded (LIMIT 2000), 30-day subquery over failing runs,
    joined to the candidate hints by jsonb containment
    (injected_hint_ids @> to_jsonb(hint.id), GIN-indexed); Python then applies the 4-entry
    RELATED_CATEGORIES relation so the dict stays the single source of truth.
    Scale caveat: at high volume the row cap binds before 30d → a recent-window
    estimate, not lifetime.
    """
    if not hint_ids:
        return {}
    from src.backend.crew_ai.optimization.nl_feedback_engine import RELATED_CATEGORIES
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    placeholders = ",".join("?" * len(hint_ids))
    rows = conn.execute(
        f"""
        SELECT hint.id AS hint_id,
               hint.original_failure_category AS orig_cat,
               er.failure_category AS fail_cat
        FROM nl_feedback_corrections hint
        JOIN (
            SELECT injected_hint_ids, failure_category
            FROM execution_records
            WHERE test_status = 'failed'
              AND injected_hint_ids IS NOT NULL
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 2000
        ) er ON er.injected_hint_ids @> to_jsonb(hint.id)
        WHERE hint.id IN ({placeholders})
        """,
        [cutoff_30d] + list(hint_ids),
    ).fetchall()
    counts: dict = {}
    for r in rows:
        fail = r["fail_cat"]
        if not fail:
            continue
        orig = r["orig_cat"]
        related = RELATED_CATEGORIES.get(orig, {orig} if orig else set())
        if fail in related:
            counts[r["hint_id"]] = counts.get(r["hint_id"], 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 10. GET /stats
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_dashboard_stats(
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    conn = fb.execution_memory.get_read_connection()
    try:
        cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        cutoff_never_attr = (datetime.now(timezone.utc) - timedelta(days=NEVER_ATTRIBUTED_REVIEW_MIN_AGE_DAYS)).isoformat()

        # Hint inventory — point-in-time counts (locked SQL from plan)
        inv = conn.execute("""
            SELECT
              SUM(CASE WHEN is_active=1 AND conflict_flagged=0 THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN is_active=1 AND conflict_flagged=1 THEN 1 ELSE 0 END) AS flagged,
              SUM(CASE WHEN is_active=0 AND EXISTS (
                  SELECT 1 FROM hint_audit ha
                  WHERE ha.hint_id = nl_feedback_corrections.id AND ha.action='retract'
              ) THEN 1 ELSE 0 END) AS retracted,
              SUM(CASE WHEN is_active=0
                  AND NOT EXISTS (SELECT 1 FROM hint_audit ha
                      WHERE ha.hint_id = nl_feedback_corrections.id AND ha.action='retract')
                  AND EXISTS (SELECT 1 FROM hint_audit ha
                      WHERE ha.hint_id = nl_feedback_corrections.id AND ha.action='llm_review_disable')
              THEN 1 ELSE 0 END) AS llm_review_disabled,
              SUM(CASE WHEN is_active=0 AND NOT EXISTS (
                  SELECT 1 FROM hint_audit ha
                  WHERE ha.hint_id = nl_feedback_corrections.id
                  AND ha.action IN ('retract', 'llm_review_disable')
              ) THEN 1 ELSE 0 END) AS auto_disabled,
              SUM(CASE WHEN created_via='admin' THEN 1 ELSE 0 END) AS admin_created,
              SUM(CASE WHEN created_via='workflow' THEN 1 ELSE 0 END) AS workflow_created
            FROM nl_feedback_corrections
        """).fetchone()

        # Trigger activity (30d) per type.
        # `flagged_events` here counts triggers that actually flagged at
        # least one hint — uses actually_flagged_hint_ids (schema v12).
        # COALESCE preserves pre-v12 row behaviour: NULL falls back to
        # flagged_hint_ids, matching the old (LLM-recommendation-based)
        # semantics for those legacy rows. New rows always write a JSON
        # array (possibly '[]'), never NULL.
        trigger_rows = conn.execute("""
            SELECT trigger_type,
                   COUNT(*) AS fired,
                   SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                   SUM(CASE WHEN status='succeeded'
                             AND COALESCE(actually_flagged_hint_ids,
                                          flagged_hint_ids) <> '[]'::jsonb
                            THEN 1 ELSE 0 END) AS flagged_events
            FROM trigger_events
            WHERE created_at >= ?
              -- Cleanliness (Part 2): usage_attribution gets its own M1 panel;
              -- keep this flag/engagement breakdown to the flagging triggers.
              AND trigger_type IN ('trigger_1', 'trigger_2')
            GROUP BY trigger_type
        """, (cutoff_30d,)).fetchall()

        # LLM accuracy KPI — engagement + reversal rates.
        # Denominator measures enforcement (actually flagged), not LLM
        # recommendation, so triggers fully suppressed by the strong-history
        # guard do NOT inflate the engagement-rate denominator. The
        # _compute_exonerations path (weekly review) deliberately keeps
        # reading flagged_hint_ids — it wants the LLM-judgment view.
        flagged_events = conn.execute("""
            SELECT COUNT(*) AS n FROM trigger_events
            WHERE created_at >= ?
              AND status = 'succeeded'
              AND COALESCE(actually_flagged_hint_ids, flagged_hint_ids) <> '[]'::jsonb
        """, (cutoff_30d,)).fetchone()["n"]

        # Excluded by ACTOR, not by an action list. The invariant is "a HUMAN
        # reviewed this flag" — 'Pending review' on the dashboard is
        # flagged_events - reviewed_events, so a machine-written row counted
        # here hides a flag nobody has looked at. An action list was always
        # going to be one verb behind: migration 21 alone writes two ('merge'
        # and 'merge_recommendation_dropped') onto the same survivor hint.
        # These four are every machine writer of hint_audit:
        #   'migration_v21'          pg_schema.py migration 21 (both verbs)
        #   'trigger_1'/'trigger_2'  _flag_hints_no_commit's own flag row
        #   'system'                 _auto_disable_hint
        # Every other writer takes its actor from a verified token
        # (_audit_actor, or feedback_insight['actor'] on the engine paths), so
        # a person can never arrive under one of these names. A NEW machine
        # actor must be added here.
        reviewed_events = conn.execute("""
            SELECT COUNT(*) AS n FROM trigger_events te
            WHERE te.created_at >= ?
              AND te.status = 'succeeded'
              AND COALESCE(te.actually_flagged_hint_ids, te.flagged_hint_ids) <> '[]'::jsonb
              AND EXISTS (
                  SELECT 1
                  FROM hint_audit ha
                  WHERE COALESCE(te.actually_flagged_hint_ids, te.flagged_hint_ids)
                        @> to_jsonb(ha.hint_id)
                    AND ha.created_at > te.created_at
                    AND ha.actor NOT IN
                        ('migration_v21', 'system', 'trigger_1', 'trigger_2')
              )
        """, (cutoff_30d,)).fetchone()["n"]

        reversed_events = conn.execute("""
            SELECT COUNT(*) AS n FROM trigger_events te
            WHERE te.created_at >= ?
              AND te.status = 'succeeded'
              AND COALESCE(te.actually_flagged_hint_ids, te.flagged_hint_ids) <> '[]'::jsonb
              AND EXISTS (
                  SELECT 1
                  FROM hint_audit ha
                  WHERE COALESCE(te.actually_flagged_hint_ids, te.flagged_hint_ids)
                        @> to_jsonb(ha.hint_id)
                    AND ha.action = 'unflag'
                    -- Redundant today — no machine writer emits 'unflag', so
                    -- the actor test cannot subtract anything here. Kept per
                    -- M5 (both EXISTS clauses get this predicate) so it is
                    -- load-bearing the day the 'unflag' restriction above is
                    -- ever widened; not evidence that a machine can write one.
                    AND ha.actor NOT IN
                        ('migration_v21', 'system', 'trigger_1', 'trigger_2')
                    AND ha.created_at > te.created_at
              )
        """, (cutoff_30d,)).fetchone()["n"]

        engagement_rate = reviewed_events / flagged_events if flagged_events > 0 else None
        reversal_rate = reversed_events / reviewed_events if reviewed_events > 0 else None

        # Manual recoveries (30d) (locked SQL from plan)
        unflags_30d = conn.execute(
            "SELECT COUNT(*) AS n FROM hint_audit "
            "WHERE action='unflag' AND created_at >= ?",
            (cutoff_30d,),
        ).fetchone()["n"]
        retracts_30d = conn.execute(
            "SELECT COUNT(*) AS n FROM hint_audit "
            "WHERE action='retract' AND created_at >= ?",
            (cutoff_30d,),
        ).fetchone()["n"]

        # LLM cost (30d) (locked SQL from plan) + estimated cost from rate table
        cost_rows = conn.execute("""
            SELECT SUM(input_tokens)  AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   AVG(llm_latency_ms) AS avg_latency_ms,
                   llm_model
            FROM trigger_events
            WHERE created_at >= ?
              AND llm_model IS NOT NULL
            GROUP BY llm_model
        """, (cutoff_30d,)).fetchall()

        cost_by_model = []
        total_cost_usd = 0.0
        for cr in cost_rows:
            cd = _row_to_dict(cr)
            inp = cd["input_tokens"] or 0
            out = cd["output_tokens"] or 0
            est = _estimate_cost(cd["llm_model"], inp, out)
            cd["estimated_cost_usd"] = round(est, 6)
            cd["rate_info"] = _model_rate_info(cd["llm_model"])
            cost_by_model.append(cd)
            total_cost_usd += est

        # ===== Part 2 panels (usage attribution) =====
        eff_report = fb.metrics_tracker.get_effectiveness_report()

        # M1 — attribution health: status breakdown + a "credited nothing
        # lately" warning + the F6 retirement-reversal accuracy proxy + the
        # holdout-lift (None → frontend renders "n/a", never 0).
        attr_status_counts = {
            r["status"]: r["n"] for r in conn.execute("""
                SELECT status, COUNT(*) AS n FROM trigger_events
                WHERE created_at >= ?
                  AND trigger_type = 'usage_attribution'
                GROUP BY status
            """, (cutoff_30d,)).fetchall()
        }
        attr_credited = conn.execute("""
            SELECT COUNT(*) AS n FROM trigger_events
            WHERE created_at >= ?
              AND trigger_type = 'usage_attribution' AND status = 'succeeded'
              AND (COALESCE(used_hint_ids, '[]'::jsonb) <> '[]'::jsonb
                   OR COALESCE(unused_hint_ids, '[]'::jsonb) <> '[]'::jsonb)
        """, (cutoff_30d,)).fetchone()["n"]

        # F6 — state-based retirement-reversal rate. Of hints auto-disabled with
        # reason 'never_used', how many are CURRENTLY active again. Read STATE
        # (not reactivate events) so all three reactivation paths are counted.
        from src.backend.crew_ai.optimization.nl_feedback_engine import (
            AUTO_DISABLE_REASON_NEVER_USED,
        )
        retired_ids = [
            r["hint_id"] for r in conn.execute(
                "SELECT DISTINCT hint_id FROM hint_audit "
                "WHERE action='auto_disable' AND reason=?",
                (AUTO_DISABLE_REASON_NEVER_USED,),
            ).fetchall()
        ]
        retirement_reversal_rate = None
        if retired_ids:
            ph = ",".join("?" * len(retired_ids))
            reversed_n = conn.execute(
                f"SELECT COUNT(*) AS n FROM nl_feedback_corrections "
                f"WHERE id IN ({ph}) AND is_active=1", retired_ids,
            ).fetchone()["n"]
            retirement_reversal_rate = round(reversed_n / len(retired_ids), 4)

        attr_total = sum(attr_status_counts.values())
        attribution_health = {
            "events_by_status": attr_status_counts,
            "events_total": attr_total,
            "credited_events": attr_credited,
            "credited_nothing_recently": attr_total > 0 and attr_credited == 0,
            "retirement_reversal_rate": retirement_reversal_rate,
            "retired_never_used_total": len(retired_ids),
            "holdout_lift": (eff_report.get("natural_comparison") or {}).get("honest_lift"),
        }

        # M3 — review candidates: never-succeeded active hints with a high
        # category-related failure-association count (Step 5b, inform-only).
        candidate_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM nl_feedback_corrections "
                "WHERE is_active=1 AND conflict_flagged=0 AND success_count=0"
            ).fetchall()
        ]
        fa_counts = _compute_failure_associations(conn, candidate_ids)
        review_candidates = [
            {"hint_id": hid, "failure_associations": n}
            for hid, n in sorted(fa_counts.items(), key=lambda kv: kv[1], reverse=True)
            if n >= FAILURE_ASSOCIATION_REVIEW_THRESHOLD
        ][:50]

        # FR5 — never-attributed: injected into >= N distinct workflows but never
        # once scored on a pass (applied_count==0 ⟺ never attributed, since
        # applied = success+failure+unused), aged past a floor. Survives the FR1
        # reset (normal hints earn applied>0 within a few passes). Inform-only.
        never_attributed = [
            _row_to_dict(r) for r in conn.execute(
                """
                SELECT hint.id, hint.feedback_text, hint.scope, hint.domain,
                       COUNT(DISTINCT er.workflow_id) AS injections
                FROM nl_feedback_corrections hint
                JOIN (
                    SELECT workflow_id, injected_hint_ids
                    FROM execution_records
                    WHERE injected_hint_ids IS NOT NULL
                      AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT 5000
                ) er ON er.injected_hint_ids @> to_jsonb(hint.id)
                WHERE hint.is_active=1 AND hint.conflict_flagged=0
                  AND hint.applied_count=0
                  AND hint.created_at <= ?
                GROUP BY hint.id
                HAVING COUNT(DISTINCT er.workflow_id) >= ?
                ORDER BY injections DESC
                LIMIT 50
                """,
                (cutoff_90d, cutoff_never_attr, NEVER_ATTRIBUTED_REVIEW_MIN_INJECTIONS),
            ).fetchall()
        ]

        return {
            "hint_inventory": _row_to_dict(inv),
            "trigger_activity": [_row_to_dict(r) for r in trigger_rows],
            "llm_accuracy": {
                "flagged_events": flagged_events,
                "reviewed_events": reviewed_events,
                "reversed_events": reversed_events,
                "pending_review": flagged_events - reviewed_events,
                "engagement_rate": round(engagement_rate, 4) if engagement_rate is not None else None,
                "reversal_rate": round(reversal_rate, 4) if reversal_rate is not None else None,
                "threshold_applicable": reviewed_events >= 10,
            },
            "manual_actions": {
                "unflags_30d": unflags_30d,
                "retracts_30d": retracts_30d,
            },
            "llm_cost": {
                "by_model": cost_by_model,
                "total_estimated_usd": round(total_cost_usd, 6),
            },
            "learning_effectiveness": eff_report,
            "attribution_health": attribution_health,
            "review_candidates": review_candidates,
            "never_attributed": never_attributed,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 10b. GET /health — learning-system health status
# ---------------------------------------------------------------------------

@router.get("/health")
def get_learning_health(
    _platform: dict = Depends(require_admin),
):
    """Learning-system health status — OK / DEGRADED / FAILED / DISABLED.

    Deliberately NOT gated by _require_feedback_loop: that dependency returns
    503, which the frontend renders as "disabled" — so a genuine init failure
    would look intentional. This endpoint always responds and distinguishes
    DISABLED (OPTIMIZATION_ENABLED=false) from FAILED (config on, but the
    FeedbackLoop failed to initialize, e.g. a migration error).
    """
    fb = get_feedback_loop()
    if fb is None:
        from src.backend.core.config import settings
        status = "FAILED" if settings.OPTIMIZATION_ENABLED else "DISABLED"
        return {"status": status}
    try:
        return {"status": fb.get_health_status()}
    except Exception as e:
        logger.warning("[LEARNING API] health status check failed: %s", e)
        return {"status": "unknown"}


# ---------------------------------------------------------------------------
# 11. LLM Admin Hint Review — request models
# ---------------------------------------------------------------------------

class ReviewDecisionRequest(BaseModel):
    admin_decision: str        # "approved" | "rejected"
    admin_notes: str | None = None


# ---------------------------------------------------------------------------
# 12. LLM Admin Hint Review — helper functions (Steps 5 + 6)
# ---------------------------------------------------------------------------

def _compute_exonerations(trigger_events) -> tuple:
    """Return (exoneration_counts, flag_counts) dicts keyed by hint_id (int).

    Parses active_hint_ids / flagged_hint_ids in Python — never via SQL LIKE
    to avoid false matches (id=1 would match ids 12, 13, 18, etc.).
    """
    exoneration_counts: dict = defaultdict(int)
    flag_counts: dict = defaultdict(int)
    for row in trigger_events:
        # active_hint_ids / flagged_hint_ids are jsonb → already parsed lists.
        active = row["active_hint_ids"] or []
        flagged = row["flagged_hint_ids"] or []
        flagged_set = set(flagged)
        for hint_id in active:
            if hint_id in flagged_set:
                flag_counts[hint_id] += 1
            else:
                exoneration_counts[hint_id] += 1
    return dict(exoneration_counts), dict(flag_counts)


def _build_review_prompt(
    decision_hints,
    exoneration_counts: dict,
    flag_counts: dict,
    disable_audit: dict | None = None,
    context_global_hints=None,
    failure_associations: dict | None = None,
) -> str:
    """Build the LLM hint review prompt for one chunk.

    Args:
        decision_hints: hint rows the LLM must produce decisions for.
        exoneration_counts: {hint_id: int} from _compute_exonerations.
        flag_counts: {hint_id: int} from _compute_exonerations.
        disable_audit: {hint_id: {"action": str, "actor": str, "reason": str}}
            for disabled hints — most-recent disable-class audit event per hint.
        context_global_hints: global hint rows shown as read-only context in
            domain chunks so the LLM can detect cross-scope duplicates.
            None or empty → no context block (used for the globals chunk itself).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if disable_audit is None:
        disable_audit = {}

    # Source label for disabled hints — derived from audit action + actor.
    _SOURCE_LABELS = {
        "auto_disable": "auto-rule",
        "llm_review_disable": "admin_review",
        "retract": "admin_manual_retract",
    }

    hint_blocks = []
    for h in decision_hints:
        h = dict(h)
        h["failure_associations"] = (failure_associations or {}).get(h["id"], 0)
        status_str = "Active"
        if h["is_active"] == 0 and h.get("disabled_at"):
            try:
                days_ago = (now - datetime.fromisoformat(h["disabled_at"])).days
                status_str = f"Disabled ({days_ago} days ago)"
            except Exception:
                status_str = "Disabled"

        applied = h["applied_count"] or 0
        success = h["success_count"] or 0
        failure = h["failure_count"] or 0
        unused = h.get("unused_count") or 0
        used = success + failure  # used outcomes — the quality denominator (B1)
        rate_str = f"{failure / used:.0%}" if used > 0 else "n/a"

        exon = exoneration_counts.get(h["id"], 0)
        flagged = flag_counts.get(h["id"], 0)

        lines = [
            f"ID: {h['id']}",
            f"Status: {status_str}",
            f"Text: {h['feedback_text']}",
            f"Scope: {h['scope']} | Domain: {h.get('domain') or 'n/a'}",
            f"Metrics: applied={applied}, success={success}, failure={failure}, "
            f"unused={unused}, failure_rate={rate_str} (of used outcomes)",
        ]

        # Step 5b: surface the category-related failure-association count when
        # present — a never-succeeded hint repeatedly injected into failing
        # tests whose failure category relates to its own. Inform-only.
        if h.get("failure_associations"):
            lines.append(
                "Failure associations (category-related failing runs, 30d): "
                f"{h['failure_associations']}"
            )

        if used == 0:
            lines.append(
                "NO USED-OUTCOME DATA (success+failure=0) — text and duplicate "
                "review only. Valid recommendations: keep, disable, flag_review. "
                "Do NOT use reactivate or unflag."
            )

        lines.append(f"Trigger 1+2 history: exonerated {exon} times, flagged {flagged} times")

        if h.get("original_failure_category"):
            lines.append(f"Original failure category: {h['original_failure_category']}")

        # Disabled hint: show source and reason so LLM can judge reactivate evidence.
        if h["is_active"] == 0:
            audit = disable_audit.get(h["id"])
            if audit:
                source = _SOURCE_LABELS.get(audit.get("action", ""), "unknown")
                lines.append(f"  Disable source: {source}")
                raw_reason = audit.get("reason") or ""
                if raw_reason:
                    truncated = raw_reason[:200]
                    lines.append(f'  Disable reason: "{truncated}"')

        # Suspended hint: show flag evidence so LLM can judge unflag evidence.
        if h.get("conflict_flagged"):
            lines.append("Currently suspended by conflict detection")
            if h.get("conflict_flag_reason"):
                lines.append(f'  Flag reason: "{h["conflict_flag_reason"]}"')
            if h.get("conflict_flagged_at"):
                try:
                    flagged_at = datetime.fromisoformat(h["conflict_flagged_at"])
                    if flagged_at.tzinfo is None:
                        flagged_at = flagged_at.replace(tzinfo=timezone.utc)
                    flag_days = (now - flagged_at).days
                    lines.append(f"  Flagged {flag_days} days ago")
                except Exception:
                    pass

        hint_blocks.append("\n".join(lines))

    hints_section = "\n\n".join(hint_blocks)

    # Build optional globals context block for domain chunks.
    context_block = ""
    context_instruction = ""
    if context_global_hints:
        context_lines = [
            f"[G{g['id']}]  {g['feedback_text']}"
            for g in context_global_hints
        ]
        context_block = (
            "\n\nGLOBAL HINTS (context-only — DO NOT produce decisions for these,\n"
            "they appear here for cross-scope duplicate detection):\n"
            + "\n".join(context_lines)
        )
        context_instruction = (
            "For hints in the GLOBAL HINTS context block, do not produce decisions — "
            "they appear only so you can detect duplicates between this chunk's hints and globals. "
            "Globals are reviewed separately in their own chunk.\n\n"
        )

    return (
        "You are reviewing learning hints for a Robot Framework test automation system.\n"
        "These hints are injected into code generation to improve test quality.\n\n"
        "HINTS TO REVIEW (decisions required):\n"
        f"{hints_section}"
        f"{context_block}\n\n"
        f"{context_instruction}"
        "DECISION OPTIONS:\n"
        "- keep: hint is appropriate, no change needed\n"
        "- disable: hint is consistently harmful or semantically duplicate of another active hint\n"
        "- reactivate: hint is currently disabled but appears incorrectly so\n"
        "- unflag: hint is suspended by Trigger 1/2 conflict detection but the flag appears wrong\n"
        "          (requires evidence that the specific Trigger LLM judgment was wrong —\n"
        "           read the Flag reason and judge whether that reasoning holds up)\n"
        "- flag_review: ambiguous — needs human attention but no clear action\n"
        "               (example: hint has 18/20 success rate AND was recently Trigger-flagged\n"
        "                with a plausible-sounding reason — metrics support keep, but the flag\n"
        "                suggests recent doubt; admin should look at this specific case)\n"
        "Note: state-invalid actions (e.g., reactivating an already-active hint, unflagging a\n"
        "non-flagged hint, disabling an already-disabled hint) are filtered by the parser.\n\n"
        "RULES:\n"
        "1. A hint is a DUPLICATE if another active hint gives the same advice in different words.\n"
        "   For duplicates, recommend disabling the one with fewer applications or lower success rate.\n"
        "1a. Cross-scope duplicates (global vs domain vs url): recommend disabling the NARROWER scope.\n"
        "    Priority: global > domain > url. The broader scope applies to more workflows;\n"
        "    removing it loses coverage. Removing the narrower scope only removes redundancy.\n"
        "1b. If a narrower-scoped hint adds specifics the broader one does not have (e.g., names\n"
        "    a specific locator while the global only describes the principle), it is a\n"
        "    SPECIALIZATION — not a duplicate. Recommend keep for both.\n"
        "2. Reactivate (from disabled): only recommend if the original disable looks wrong.\n"
        "   For auto-rule disabled hints, this means (success+failure) >= 5 AND\n"
        "   success/(success+failure) > 0.5 (the used-outcome track record was fine).\n"
        "   For admin_review or admin_manual_retract, require strong contrary evidence\n"
        "   and a stated reason that appears no longer applicable.\n"
        "3. Unflag (from suspended): only recommend if the Trigger flag itself looks wrong.\n"
        "   Read the Flag reason and judge whether that specific LLM judgment had strong\n"
        "   evidence. A high overall success rate is corroborating but not sufficient —\n"
        "   the primary question is whether the flag's stated reasoning holds up.\n"
        "4. A hint with (success+failure) < 5 used outcomes cannot be evaluated for\n"
        "   effectiveness — only review the text for quality and duplicates.\n"
        "5. Do NOT recommend disabling a hint solely because it has a high failure rate if it\n"
        "   was also exonerated multiple times by Trigger 1+2 (execution context matters).\n"
        "6. For hints with (success+failure) = 0 (no used-outcome data), valid recommendations\n"
        "   are: keep, disable, flag_review. Do not recommend reactivate or unflag — there is no\n"
        "   used-outcome track record to judge whether the original disable or flag was wrong.\n"
        "7. unused counts injections where the hint's advice was NOT used in passing code. High\n"
        "   unused with zero success = over-surfaced dead weight (distinct from harmful, which\n"
        "   is used-and-failed); such a hint is a disable candidate on text/duplicate grounds.\n\n"
        "Respond with ONLY valid JSON — no markdown, no explanation outside the JSON:\n"
        '{\n'
        '  "decisions": [\n'
        '    {"id": <int>, "recommendation": "<keep|disable|reactivate|unflag|flag_review>", "reason": "<str>"},\n'
        '    ...\n'
        '  ],\n'
        '  "summary": "<brief overall assessment>"\n'
        '}'
    )


def _compute_warning(decisions: list, hints: list) -> str | None:
    """Return a warning string if >30% of active hints are recommended for disable, else None."""
    active_hint_ids = {h["id"] for h in hints if h["is_active"] == 1}
    if not active_hint_ids:
        return None
    active_disable_count = sum(
        1 for d in decisions
        if d["recommendation"] == "disable" and d["id"] in active_hint_ids
    )
    if active_disable_count / len(active_hint_ids) > 0.30:
        pct = active_disable_count * 100 // len(active_hint_ids)
        return (
            f"LLM recommended disabling {active_disable_count} of "
            f"{len(active_hint_ids)} active hints ({pct}%). "
            f"Review each carefully before approving."
        )
    return None


def _run_hint_review(session_id: int, feedback_loop) -> None:
    """Run LLM hint review in daemon thread.  Session status updated in finally — always.

    Multi-layer LLM design — each layer has an exclusive failure mode (B5):
    - Trigger 1 (workflow execution): catches hints that were injected but the test still
      failed — per-execution evidence tied to the actual failure category and error output.
    - Trigger 2 (user feedback submission): catches text contradictions between new feedback
      and existing hints — the only layer that can see the incoming feedback before storage.
    - Auto-disable (metrics): catches hints with consistently poor track records across many
      executions — pure metrics-based, no LLM, responds to accumulated evidence over time.
    - Hint review (this function): the only layer that sees ALL hints simultaneously and can
      detect cross-hint duplicates, cross-scope redundancy (global vs domain vs url), and
      holistic quality issues that per-hint single-trigger detection misses.
    """
    conn = None
    error_to_report: str | None = None

    try:
        conn = _admin_conn()

        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        hints = conn.execute(
            "SELECT * FROM nl_feedback_corrections "
            "WHERE is_active = 1 "
            "   OR (is_active = 0 AND disabled_at IS NOT NULL AND disabled_at >= ?) "
            "ORDER BY is_active DESC, applied_count DESC",
            (thirty_days_ago,),
        ).fetchall()

        conn.execute(
            "UPDATE hint_review_sessions SET hint_count=? WHERE id=?",
            (len(hints), session_id),
        )
        conn.commit()

        if not hints:
            conn.execute(
                "UPDATE hint_review_sessions "
                "SET status='completed', completed_at=? WHERE id=?",
                (_now(), session_id),
            )
            conn.commit()
            return

        # Fetch most-recent disable-class audit row per disabled hint.
        disable_audit: dict = {}
        disabled_ids = [h["id"] for h in hints if h["is_active"] == 0]
        if disabled_ids:
            placeholders = ",".join("?" * len(disabled_ids))
            audit_rows = conn.execute(
                f"SELECT hint_id, action, actor, reason FROM hint_audit "
                f"WHERE hint_id IN ({placeholders}) "
                f"  AND action IN ('auto_disable','llm_review_disable','retract') "
                f"ORDER BY created_at DESC",
                disabled_ids,
            ).fetchall()
            for row in audit_rows:
                hid = row["hint_id"]
                if hid not in disable_audit:  # first = most recent (ORDER BY DESC)
                    disable_audit[hid] = dict(row)

        # Trigger-event data computed once over the full hint set.
        # CRITICAL (Part 2): restrict to the FLAGGING triggers. A standalone
        # usage_attribution row is all-active / none-flagged, so
        # _compute_exonerations would count every injected hint as "exonerated"
        # and bias the review against disabling bad hints. Filter by trigger_type
        # — NOT by flag-presence, which would wrongly drop legitimate trigger_1
        # rows where every hint was genuinely exonerated.
        trigger_events = conn.execute(
            "SELECT active_hint_ids, flagged_hint_ids FROM trigger_events "
            "WHERE created_at >= ? "
            "AND trigger_type IN ('trigger_1', 'trigger_2')",
            (thirty_days_ago,),
        ).fetchall()
        exoneration_counts, flag_counts = _compute_exonerations(trigger_events)
        # Step 5b: category-related failure associations for never-succeeded
        # hints — inform-only evidence in the review prompt (mirrors the M3
        # dashboard panel).
        failure_associations = _compute_failure_associations(
            conn, [h["id"] for h in hints if (h["success_count"] or 0) == 0]
        )

        # Build chunk plan: 1 globals chunk + 1 per distinct domain, PER ORG.
        # F2: two orgs with identical hint text (mandatory, not accidental,
        # under T10's copy-on-promote design) must never share a chunk — one
        # org's LLM verdict must never decide another org's hint lifecycle,
        # and one org's correction text must never enter a prompt about
        # another org. Group by org_id FIRST, then split each org's hints
        # into a global chunk + per-domain chunks exactly as before.
        # org_id IS NULL hints (pre-org-partitioning legacy rows) get their
        # own group — they are unreachable by any org's read anyway (T9/P6:
        # org_id = NULL yields NULL for every row), so they must never be
        # merged into a real org's chunk.
        org_groups: dict = {}
        for h in hints:
            org_groups.setdefault(h["org_id"], []).append(h)

        chunks: list[dict] = []
        for org_key in sorted(org_groups.keys(), key=lambda o: o or ""):
            org_hints = org_groups[org_key]
            global_hints = [h for h in org_hints if h["scope"] == "global"]
            non_global_hints = [h for h in org_hints if h["scope"] != "global"]

            domain_groups: dict = {}
            for h in non_global_hints:
                domain_groups.setdefault(h["domain"], []).append(h)

            if global_hints:
                chunks.append({
                    "org_id": org_key,
                    "scope_type": "global",
                    "scope_value": None,
                    "decision_hints": global_hints,
                    "context_global_hints": None,
                })
            for domain_key in sorted(domain_groups.keys(), key=lambda d: d or ""):
                chunks.append({
                    "org_id": org_key,
                    "scope_type": "domain",
                    "scope_value": domain_key,
                    "decision_hints": domain_groups[domain_key],
                    # This org's globals only — never another org's.
                    "context_global_hints": global_hints if global_hints else None,
                })

        # Pre-insert all page rows so the UI sees the full chunk plan immediately.
        page_start = _now()
        page_ids: list[int] = []
        for chunk in chunks:
            conn.execute(
                "INSERT INTO hint_review_pages "
                "(session_id, scope_type, scope_value, status, hint_count, created_at, org_id) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (session_id, chunk["scope_type"], chunk["scope_value"],
                 len(chunk["decision_hints"]), page_start, chunk["org_id"]),
            )
            page_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()

        model_string = _get_conflict_detection_model()
        extra_kwargs = _get_conflict_detection_completion_kwargs()

        all_decisions: list = []
        total_latency_ms: int = 0
        any_succeeded = False

        for chunk, page_id in zip(chunks, page_ids):
            decision_hints = chunk["decision_hints"]

            # Chunk-scoped parser arguments prevent cross-chunk ID bleed.
            chunk_known_ids = {h["id"] for h in decision_hints}
            # S2 (Part 2): "no data to evaluate" is now NO USED OUTCOMES
            # (success + failure == 0), not applied == 0 — a hint can have
            # applied > 0 yet zero used outcomes (all-unused, or legacy
            # uncategorized fails). This set drops reactivate/unflag for such
            # hints in _parse_review_response.
            chunk_zero_app_ids = {
                h["id"] for h in decision_hints
                if (h["success_count"] or 0) + (h["failure_count"] or 0) == 0
            }
            chunk_hint_states = {
                h["id"]: {
                    "is_active": h["is_active"],
                    "conflict_flagged": h["conflict_flagged"],
                }
                for h in decision_hints
            }

            prompt = _build_review_prompt(
                decision_hints,
                exoneration_counts,
                flag_counts,
                disable_audit=disable_audit,
                context_global_hints=chunk["context_global_hints"],
                failure_associations=failure_associations,
            )

            chunk_error: str | None = None
            parsed: dict | None = None
            retry_count = 0
            chunk_start = time.monotonic()

            for attempt in range(2):
                try:
                    response = _call_conflict_detection_llm(
                        model_string,
                        [{"role": "user", "content": prompt}],
                        extra_kwargs,
                        timeout=120,
                    )
                    content = response.choices[0].message.content
                    if not content:
                        raise ValueError("Empty LLM response")
                    parsed = _parse_review_response(
                        content, chunk_known_ids, chunk_zero_app_ids, chunk_hint_states
                    )
                    break
                except Exception as exc:
                    if attempt == 0:
                        retry_count = 1
                        logger.warning(
                            "[REVIEW] Session %d chunk '%s' attempt 1 failed: %s — retrying",
                            session_id,
                            chunk["scope_value"] or chunk["scope_type"],
                            exc,
                        )
                    else:
                        chunk_error = str(exc)[:500]
                        logger.error(
                            "[REVIEW] Session %d chunk '%s' failed after retry: %s",
                            session_id,
                            chunk["scope_value"] or chunk["scope_type"],
                            exc,
                        )

            llm_latency_ms = int((time.monotonic() - chunk_start) * 1000)
            total_latency_ms += llm_latency_ms
            chunk_now = _now()

            if parsed is not None:
                for decision in parsed["decisions"]:
                    conn.execute(
                        "INSERT INTO hint_review_recommendations "
                        "(session_id, hint_id, recommendation, reason, exoneration_count, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            session_id, decision["id"], decision["recommendation"],
                            decision["reason"],
                            exoneration_counts.get(decision["id"], 0),
                            chunk_now,
                        ),
                    )
                conn.execute(
                    "UPDATE hint_review_pages "
                    "SET status='succeeded', llm_latency_ms=?, retry_count=?, completed_at=? "
                    "WHERE id=?",
                    (llm_latency_ms, retry_count, chunk_now, page_id),
                )
                conn.commit()
                all_decisions.extend(parsed["decisions"])
                any_succeeded = True
            else:
                conn.execute(
                    "UPDATE hint_review_pages "
                    "SET status='failed', llm_latency_ms=?, retry_count=?, "
                    "    error_message=?, completed_at=? WHERE id=?",
                    (llm_latency_ms, retry_count, chunk_error, chunk_now, page_id),
                )
                conn.commit()

        session_status = "pending_review" if any_succeeded else "failed"
        warning = _compute_warning(all_decisions, hints) if any_succeeded else None
        conn.execute(
            "UPDATE hint_review_sessions "
            "SET status=?, completed_at=?, llm_model=?, llm_latency_ms=?, warning=? WHERE id=?",
            (session_status, _now(), model_string, total_latency_ms, warning, session_id),
        )
        conn.commit()

    except Exception as e:
        error_to_report = str(e)[:500]
        logger.error("[REVIEW] Session %d failed: %s", session_id, e)

    finally:
        if error_to_report is not None and conn is not None:
            try:
                conn.rollback()
                conn.execute(
                    "UPDATE hint_review_sessions "
                    "SET status='failed', error_message=? WHERE id=?",
                    (error_to_report, session_id),
                )
                conn.commit()
            except Exception as report_err:
                logger.error(
                    "[REVIEW] Failed to record session %d failure: %s",
                    session_id, report_err,
                )

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass




# ---------------------------------------------------------------------------
# 13. LLM Admin Hint Review — API endpoints (Step 9)
# ---------------------------------------------------------------------------

@router.post("/review-hints/start")
def start_hint_review(
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    # DB check replaces in-memory lock: restart-safe and consistent with DB state.
    # _run_hint_review updates the session to 'pending_review' or 'failed', which
    # clears the pending_llm row and allows future reviews to start.
    #
    # KNOWN, OPEN: the COUNT-then-INSERT pair below is NOT serialised on
    # Postgres. BEGIN IMMEDIATE is swallowed by pg_compat._is_noop, and a
    # COUNT has no row to lock, so two simultaneous clicks can both observe
    # count=0, both INSERT a pending_llm row and both spawn _run_hint_review
    # — two sessions and duplicated LLM spend. Closing it needs a unique partial
    # index on status='pending_llm' (a migration) or an advisory lock;
    # apply_review_session's sibling race, which corrupts the audit trail, was
    # closed with FOR UPDATE because it had a row to lock. This one only costs
    # a duplicate session, both of which are visible in the sessions list.
    conn = _admin_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT COUNT(*) FROM hint_review_sessions WHERE status='pending_llm'"
        ).fetchone()[0]
        if pending > 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail="A review is already in progress")
        conn.execute(
            "INSERT INTO hint_review_sessions (status, hint_count, created_at) "
            "VALUES ('pending_llm', 0, ?)", (_now(),)
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    threading.Thread(
        target=_run_hint_review, args=(session_id, fb), daemon=True
    ).start()
    return {"session_id": session_id, "status": "pending_llm"}


@router.get("/review-hints/sessions")
def list_review_sessions(
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    conn = fb.execution_memory.get_read_connection()
    try:
        rows = conn.execute(
            "SELECT id, status, hint_count, llm_latency_ms, warning, "
            "       created_at, completed_at "
            "FROM hint_review_sessions ORDER BY created_at DESC"
        ).fetchall()
        is_any_running = any(r["status"] == "pending_llm" for r in rows)
        return {
            "is_any_running": is_any_running,
            "sessions": [_row_to_dict(r) for r in rows],
        }
    finally:
        conn.close()


@router.get("/review-hints/sessions/{session_id}")
def get_review_session(
    session_id: int,
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    conn = fb.execution_memory.get_read_connection()
    try:
        session = conn.execute(
            "SELECT * FROM hint_review_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        recs = conn.execute(
            "SELECT r.*, h.feedback_text, h.scope, h.domain, h.applied_count, "
            "       h.success_count, h.failure_count, h.is_active, h.conflict_flagged "
            "FROM hint_review_recommendations r "
            "JOIN nl_feedback_corrections h ON h.id = r.hint_id "
            "WHERE r.session_id = ? ORDER BY r.id",
            (session_id,),
        ).fetchall()

        pages = conn.execute(
            "SELECT * FROM hint_review_pages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        return {
            "session": _row_to_dict(session),
            "recommendations": [_row_to_dict(r) for r in recs],
            "pages": [_row_to_dict(p) for p in pages],
        }
    finally:
        conn.close()


@router.patch("/review-hints/sessions/{session_id}/recommendations/{rec_id}")
def decide_recommendation(
    session_id: int,
    rec_id: int,
    request: ReviewDecisionRequest,
    fb=Depends(_require_feedback_loop),
    _platform: dict = Depends(require_admin),
):
    if request.admin_decision not in {"approved", "rejected"}:
        raise HTTPException(
            status_code=400,
            detail="admin_decision must be 'approved' or 'rejected'",
        )

    conn = _admin_conn()
    try:
        session = conn.execute(
            "SELECT status FROM hint_review_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        if session["status"] != "pending_review":
            raise HTTPException(
                status_code=409,
                detail=f"Session is '{session['status']}', not 'pending_review'",
            )

        rec = conn.execute(
            "SELECT * FROM hint_review_recommendations WHERE id = ? AND session_id = ?",
            (rec_id, session_id),
        ).fetchone()
        if not rec:
            raise HTTPException(status_code=404, detail=f"Recommendation {rec_id} not found")

        conn.execute(
            "UPDATE hint_review_recommendations "
            "SET admin_decision=?, admin_notes=?, decided_at=? WHERE id=?",
            (request.admin_decision, request.admin_notes, _now(), rec_id),
        )
        conn.commit()

        updated = conn.execute(
            "SELECT * FROM hint_review_recommendations WHERE id = ?", (rec_id,)
        ).fetchone()
        return {"recommendation": _row_to_dict(updated)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[REVIEW] decide_recommendation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update recommendation")
    finally:
        conn.close()


@router.post("/review-hints/sessions/{session_id}/apply")
def apply_review_session(
    session_id: int,
    fb=Depends(_require_feedback_loop),
    admin: dict | None = Depends(require_user),
    _platform: dict = Depends(require_admin),
):
    conn = _admin_conn()
    try:
        # FOR UPDATE is what makes the check-then-apply sequence atomic, NOT
        # the BEGIN IMMEDIATE below it: pg_compat._is_noop swallows any
        # statement starting with BEGIN, so on Postgres that line reaches no
        # connection and takes no lock. Without the row lock two concurrent
        # "Apply" clicks both observe status='pending_review' and both run the
        # whole loop — measured, on a real stack and in
        # TestConcurrentApplyIsSerialised: both returned applied_count and
        # hint_audit held two llm_review_* rows per approved recommendation.
        # Hint state survives that (the UPDATEs are idempotent) and so does the
        # engagement KPI (its clauses are EXISTS, not COUNT); the audit trail
        # does not, and that is the record this endpoint exists to keep honest.
        #
        # The loser blocks here, then re-reads the committed row under READ
        # COMMITTED, sees 'completed' and takes the 409 below. The BEGIN line
        # stays for SQLite-dialect fidelity, as elsewhere in this module.
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM hint_review_sessions WHERE id = ? FOR UPDATE",
            (session_id,),
        ).fetchone()
        if not session:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        if session["status"] != "pending_review":
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Session is '{session['status']}', not 'pending_review'",
            )

        approved_recs = conn.execute(
            "SELECT r.*, h.feedback_text, h.scope, h.domain, h.applied_count, "
            "       h.success_count, h.failure_count, h.is_active, h.conflict_flagged, "
            "       h.org_id "
            "FROM hint_review_recommendations r "
            "JOIN nl_feedback_corrections h ON h.id = r.hint_id "
            "WHERE r.session_id = ? AND r.admin_decision = 'approved' AND r.applied = 0",
            (session_id,),
        ).fetchall()

        now = _now()
        applied_count = 0

        for rec in approved_recs:
            rec = dict(rec)
            hint_id = rec["hint_id"]
            recommendation = rec["recommendation"]

            before = {
                "is_active": rec["is_active"],
                "conflict_flagged": rec["conflict_flagged"],
                "applied_count": rec["applied_count"],
                "success_count": rec["success_count"],
                "failure_count": rec["failure_count"],
            }

            # Surface the human's decision in the audit trail: their note (if
            # any) leads the reason, the LLM's recommendation follows as context,
            # and the actor reflects the person who reviewed — not the LLM run.
            note = (rec.get("admin_notes") or "").strip()
            llm_reason = rec.get("reason") or ""
            audit_reason = f"{note}  —  [LLM: {llm_reason}]" if note else llm_reason
            actor = _audit_actor(admin)

            # Defence in depth (F2), all three UPDATEs below: constrain to the
            # hint's own org, captured from the JOIN above. Not the
            # correctness fix — the chunk-plan grouping in _run_hint_review
            # is — on correct data this predicate changes nothing; it only
            # guards against a hint whose org no longer matches what this
            # approved recommendation was selected against.
            #
            # guard_blocked tracks whether that predicate actually filtered
            # the row out (rowcount==0) so the code below can stop treating
            # a write the guard refused as though it happened: no audit row
            # asserting a state change that didn't occur, no applied=1, no
            # applied_count credit. Fix round 1 (reviewer Important finding):
            # without this, a guard-blocked UPDATE still fabricated its audit
            # trail and marked the recommendation permanently applied.
            hint_org = rec["org_id"]
            guard_blocked = False

            if recommendation == "disable":
                cur = conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET is_active=0, disabled_at=? WHERE id=? AND org_id IS NOT DISTINCT FROM ?",
                    (now, hint_id, hint_org),
                )
                if cur.rowcount == 0:
                    guard_blocked = True
                else:
                    _write_hint_audit(conn, hint_id, "llm_review_disable", actor,
                                      audit_reason, before,
                                      {**before, "is_active": 0, "disabled_at": now})

            elif recommendation == "reactivate":
                cur = conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET is_active=1, conflict_flagged=0, conflict_flagged_at=NULL, "
                    # Step 4b: LLM-review reactivation is a fresh chance — reset
                    # unused_count so the hint is not immediately re-retired.
                    "    conflict_flag_reason=NULL, disabled_at=NULL, unused_count=0 "
                    "WHERE id=? AND org_id IS NOT DISTINCT FROM ?",
                    (hint_id, hint_org),
                )
                if cur.rowcount == 0:
                    guard_blocked = True
                else:
                    _write_hint_audit(conn, hint_id, "llm_review_reactivate", actor,
                                      audit_reason, before,
                                      {**before, "is_active": 1, "conflict_flagged": 0,
                                       "conflict_flagged_at": None, "conflict_flag_reason": None,
                                       "disabled_at": None})

            elif recommendation == "unflag":
                cur = conn.execute(
                    "UPDATE nl_feedback_corrections "
                    "SET conflict_flagged=0, conflict_flagged_at=NULL, "
                    "    conflict_flag_reason=NULL WHERE id=? AND org_id IS NOT DISTINCT FROM ?",
                    (hint_id, hint_org),
                )
                if cur.rowcount == 0:
                    guard_blocked = True
                else:
                    _write_hint_audit(conn, hint_id, "llm_review_unflag", actor,
                                      audit_reason, before,
                                      {**before, "conflict_flagged": 0,
                                       "conflict_flagged_at": None, "conflict_flag_reason": None})

            elif recommendation == "keep":
                _write_hint_audit(conn, hint_id, "llm_review_keep", actor,
                                  audit_reason, before, before)

            elif recommendation == "flag_review":
                _write_hint_audit(conn, hint_id, "llm_review_flagged", actor,
                                  audit_reason, before, before)

            if guard_blocked:
                # Leave applied=0 deliberately — the recommendation stays
                # visible as unapplied rather than silently disappearing.
                # There is no retry, and this comment used to claim one: the
                # only writers of hint_review_sessions.status are this
                # function's 'completed' below, _run_hint_review's
                # 'completed' on an empty page and its 'pending_review' /
                # 'failed' at creation. Nothing moves a session back to
                # 'pending_review', and the guard above 409s on any other
                # status, so the approved action is stranded — an admin has to
                # apply it to the hint directly through the hints UI.
                logger.warning(
                    "[REVIEW] Session %d: hint %d's org no longer matches "
                    "the org this recommendation was approved against "
                    "(expected %r) — %s skipped, recommendation %d left unapplied",
                    session_id, hint_id, hint_org, recommendation, rec["id"],
                )
                continue

            conn.execute(
                "UPDATE hint_review_recommendations SET applied=1 WHERE id=?",
                (rec["id"],),
            )
            applied_count += 1

        conn.execute(
            "UPDATE hint_review_sessions SET status='completed', completed_at=? WHERE id=?",
            (now, session_id),
        )
        conn.commit()

        return {"applied_count": applied_count}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[REVIEW] apply_review_session %d failed: %s", session_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to apply review session")
    finally:
        conn.close()
