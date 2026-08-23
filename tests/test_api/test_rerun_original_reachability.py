"""A "Re-run of …" control must not offer a run the caller cannot open.

`rerun_of` is written when a run is cloned, and it stays true forever: the row
IS a re-run. Whether the ORIGINAL is still reachable is a different question,
and it changes underneath the row — a peer re-runs a published run under D5,
its owner then takes it out of the folder, and the link the peer is looking at
is dead. Clicking it answered 404 and the drawer rendered "Pasted code run"
over "Run not found", which reads as "the original's code is missing" rather
than "you have no access to it".

So the row carries BOTH: `rerun_of` (still true) and `rerun_of_accessible`
(computed for this caller, this request). The flag has to equal what
`GET /api/history/{original}` really answers — the pairing is the contract, and
both are asserted in the same test. If they can disagree the pill simply lies
in the other direction.

Cost is part of the requirement, not a nicety (owner ruling R4): ONE extra
query for a whole page however many re-run rows it holds, and NONE at all when
it holds none. That is asserted by spying on psycopg's Connection.execute and
counting the statements the batched lookup issues, not by reading the code.

Referenced by: src/backend/api/history_endpoints.py,
               src/backend/core/run_registry.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/test_groups.py (_auth, _register, _seed_run_for,
            _team_of_two).
"""

import contextlib
import uuid
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


from tests.test_api.test_groups import (  # noqa: E402
    _auth, _register, _seed_run_for, _team_of_two,
)


def _seed_rerun_for(client, tok, original, query="a re-run") -> str:
    """A test_runs row owned by the token's user, linked to `original`."""
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.core.run_registry import get_run_registry

    claims = decode_token(tok)
    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid,
        {"user_id": claims["user_id"], "email": claims["email"],
         "org_id": claims["org_id"]},
        query,
        "passed",
        rerun_of=original,
    )
    return rid


@contextlib.contextmanager
def _sql_spy():
    """Record every statement psycopg executes, delegating to the real method.

    The registry issues all of its reads through `conn.execute`, so this counts
    QUERIES, not method calls — a per-row implementation hidden behind one
    helper call would still be caught."""
    import psycopg

    seen: list[str] = []
    real = psycopg.Connection.execute

    def _spy(self, query, params=None, **kw):
        seen.append(str(query))
        return real(self, query, params, **kw)

    with patch.object(psycopg.Connection, "execute", _spy):
        yield seen


def _original_lookups(seen: list[str]) -> list[str]:
    """The statements the batched original-run lookup issues, and only those.

    It is the one read that asks test_runs for a SET of ids; the list's own
    COUNT/SELECT carry no `= ANY(`, and assign_runs' UPDATE carries no
    `FROM test_runs t`."""
    return [s for s in seen if "FROM test_runs t" in s and "= ANY(" in s]


@pytest.fixture
def unfiled(client):
    """Bob's re-run of a run Alice has since taken back out of the folder.

    The exact live sequence: Alice files her run, which publishes it to the
    org; Bob re-runs it (D5 lets him); Alice unfiles it, which un-publishes it.
    Bob's row keeps a link to a run he can no longer open.
    """
    tok_a, tok_b, org_id = _team_of_two(client)
    original = _seed_run_for(client, tok_a, "alice's checkout test")
    gid = client.post("/api/groups", json={"name": f"Shared {uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [original], "group_id": gid},
                      headers=_auth(tok_a)).status_code == 200
    rerun = _seed_rerun_for(client, tok_b, original, "bob's re-run of alice's test")
    assert client.put("/api/groups/assignments",
                      json={"run_ids": [original], "group_id": None},
                      headers=_auth(tok_a)).status_code == 200
    return {"tok_a": tok_a, "tok_b": tok_b, "org_id": org_id,
            "original": original, "rerun": rerun}


# ---------------------------------------------------------------------------
# The contract: the flag equals what GET /api/history/{original} answers
# ---------------------------------------------------------------------------

def test_a_rerun_whose_original_was_unfiled_says_so(client, unfiled):
    """`rerun_of` stays — the row really is a re-run — and the new flag says
    the original is out of reach. The 404 in the same test is the pairing: the
    flag is not a guess about the detail endpoint, it is its answer."""
    peer = _auth(unfiled["tok_b"])
    rows = client.get("/api/history", headers=peer).json()["runs"]
    row = next(r for r in rows if r["run_id"] == unfiled["rerun"])

    assert row["rerun_of"] == unfiled["original"], (
        "the re-run link was dropped instead of qualified")
    assert row.get("rerun_of_accessible") is False
    assert client.get(f"/api/history/{unfiled['original']}",
                      headers=peer).status_code == 404

    drawer = client.get(f"/api/history/{unfiled['rerun']}", headers=peer)
    assert drawer.status_code == 200, drawer.text
    assert drawer.json()["rerun_of"] == unfiled["original"]
    assert drawer.json().get("rerun_of_accessible") is False, (
        "the drawer header still offers a link the list already refuses")


def test_a_rerun_of_a_run_the_caller_owns_is_reachable(client, unfiled):
    """The other direction, or the fix is just a way of hiding every pill."""
    peer = _auth(unfiled["tok_b"])
    own = _seed_run_for(client, unfiled["tok_b"], "bob's own run")
    rerun = _seed_rerun_for(client, unfiled["tok_b"], own, "bob's re-run of his own")

    rows = client.get("/api/history", headers=peer).json()["runs"]
    row = next(r for r in rows if r["run_id"] == rerun)
    assert row.get("rerun_of_accessible") is True
    assert client.get(f"/api/history/{own}", headers=peer).status_code == 200

    drawer = client.get(f"/api/history/{rerun}", headers=peer)
    assert drawer.json().get("rerun_of_accessible") is True


def test_a_still_published_original_stays_reachable_for_a_peer(client):
    """A peer's re-run of a run that is STILL in the org's folder keeps a live
    link — the flag follows publication, not authorship."""
    tok_a, tok_b, _org = _team_of_two(client)
    original = _seed_run_for(client, tok_a, "alice's published test")
    gid = client.post("/api/groups", json={"name": f"Live {uuid.uuid4().hex[:6]}"},
                      headers=_auth(tok_a)).json()["group_id"]
    client.put("/api/groups/assignments",
               json={"run_ids": [original], "group_id": gid}, headers=_auth(tok_a))
    rerun = _seed_rerun_for(client, tok_b, original, "bob's re-run")

    rows = client.get("/api/history", headers=_auth(tok_b)).json()["runs"]
    row = next(r for r in rows if r["run_id"] == rerun)
    assert row.get("rerun_of_accessible") is True
    assert client.get(f"/api/history/{original}",
                      headers=_auth(tok_b)).status_code == 200


# ---------------------------------------------------------------------------
# Cost (owner ruling R4): one query a page, none when there is nothing to ask
# ---------------------------------------------------------------------------

def test_a_page_of_reruns_costs_exactly_one_extra_query(client, unfiled):
    """Four re-run rows over two distinct originals — still ONE lookup. A
    per-row implementation issues four, and a non-deduplicating one issues a
    four-id query where two would do."""
    peer = _auth(unfiled["tok_b"])
    own = _seed_run_for(client, unfiled["tok_b"], "bob's own run")
    _seed_rerun_for(client, unfiled["tok_b"], own, "r1")
    _seed_rerun_for(client, unfiled["tok_b"], own, "r2")
    _seed_rerun_for(client, unfiled["tok_b"], unfiled["original"], "r3")

    with _sql_spy() as seen:
        page = client.get("/api/history", headers=peer).json()

    assert len([r for r in page["runs"] if r["rerun_of"]]) == 4
    lookups = _original_lookups(seen)
    assert len(lookups) == 1, f"expected one batched lookup, got {len(lookups)}"


def test_a_page_with_no_reruns_costs_no_extra_query(client):
    """Most pages carry no re-run at all, so most pages must pay nothing."""
    tok = _register(client, f"nr-{uuid.uuid4().hex[:8]}@e.com")
    _seed_run_for(client, tok, "one")
    _seed_run_for(client, tok, "two")

    with _sql_spy() as seen:
        page = client.get("/api/history", headers=_auth(tok)).json()

    assert page["total"] == 2
    assert all(r["rerun_of"] is None for r in page["runs"])
    assert _original_lookups(seen) == []


def test_the_drawer_of_a_plain_run_costs_no_extra_query(client):
    tok = _register(client, f"dp-{uuid.uuid4().hex[:8]}@e.com")
    rid = _seed_run_for(client, tok, "not a re-run")

    with _sql_spy() as seen:
        r = client.get(f"/api/history/{rid}", headers=_auth(tok))

    assert r.status_code == 200 and r.json()["rerun_of"] is None
    assert _original_lookups(seen) == []


def test_the_drawer_of_a_rerun_costs_exactly_one_extra_query(client, unfiled):
    with _sql_spy() as seen:
        r = client.get(f"/api/history/{unfiled['rerun']}",
                       headers=_auth(unfiled["tok_b"]))

    assert r.status_code == 200, r.text
    lookups = _original_lookups(seen)
    assert len(lookups) == 1, f"expected one batched lookup, got {len(lookups)}"
