"""An admin-created hint names the admin who created it.

The engine records an author for a hint born from user feedback. POST /hints
is the other way a hint comes into existence, and it must key its author the
same way — otherwise the Author permission tier sees two kinds of hint and
only one of them has an owner.

The id has no fallback on purpose. `actor` (the email) legitimately degrades
to a label when no token is present, but the tier compares ids for equality,
so a placeholder id would make every unidentified creator each other's author.

Depends on: src/backend/api/learning_endpoints.py (create_hint)
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.backend.crew_ai.optimization import pg_compat


def _author_of(dsn: str, hint_id: int) -> tuple:
    conn = pg_compat.connect(dsn)
    try:
        row = conn.execute(
            "SELECT created_by_user_id, created_by_email, org_id "
            "FROM nl_feedback_corrections WHERE id = ?", (hint_id,)
        ).fetchone()
        return row["created_by_user_id"], row["created_by_email"], row["org_id"]
    finally:
        conn.close()


def test_admin_created_hint_records_the_creating_admin(dash_client):
    r = dash_client.post(
        "/api/learning/hints",
        json={
            "feedback_text": "prefer role selectors over nth-child",
            "anchor_query": "open the dashboard",
            "scope": "global",
            "actor": "ignored-client-value@example.com",
            "org_id": dash_client.org_a,
        },
        headers={"Authorization": f"Bearer {dash_client.platform_admin_token}"},
    )
    assert r.status_code == 201, r.text
    hint_id = r.json()["hint"]["id"]

    uid, email, org_id = _author_of(dash_client._dsn, hint_id)
    assert uid == dash_client.platform_admin_uid
    # The client-supplied actor is never trusted — the token's email wins.
    assert email == "admin@test.local"
    assert org_id == dash_client.org_a



def test_an_author_id_that_decodes_to_an_empty_string_is_stored_as_null(
        dash_client, api_pg_em):
    """decode_token defaults the `sub` claim to '' (jwt_utils), so a token
    without one yields user_id = '' rather than None. "Unattributed" must have
    exactly ONE representation in created_by_user_id — the engine's own write
    path already normalises with `or None`, and this one now matches it.

    require_user is overridden rather than a token being minted, because
    _revalidate_active_user rejects '' as a user id before the route is
    reached; the point here is the column's contract, not the reachability of
    that particular token.
    """
    from src.backend.api.learning_endpoints import router
    from src.backend.auth.jwt_utils import require_user

    _, dsn = api_pg_em
    app = FastAPI()
    app.include_router(router, prefix="/api/learning")
    app.dependency_overrides[require_user] = lambda: {
        "user_id": "", "email": "no-sub@test.local", "role": "user",
        "org_id": dash_client.org_a, "org_role": "org_admin",
    }
    with TestClient(app) as client:
        r = client.post(
            "/api/learning/hints",
            json={
                "feedback_text": "an author id must not be an empty string",
                "anchor_query": "open the dashboard",
                "scope": "global",
                "actor": "ignored@example.com",
                "org_id": dash_client.org_a,
            },
        )
    assert r.status_code == 201, r.text

    uid, _email, _org = _author_of(dsn, r.json()["hint"]["id"])
    assert uid is None, f"stored {uid!r}; unattributed must be NULL, never ''"
