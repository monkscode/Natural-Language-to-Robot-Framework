"""Grouped runs are the ORG's; ungrouped work is the author's alone.

The one rule every read derives from (owner decisions, 2026-08-23):

    visible_to(caller) := t.user_id = caller OR (t.user_id IS NOT NULL
                                                 AND the run is in a folder
                                                 of the CALLER'S org)

The folder half is read off the org-scoped join (g.group_id), never the raw
t.group_id column: a caller with no org resolves no folder and so gets their
own work only, and a folder in another org publishes nothing here.

So "Ungrouped" means work in progress, private to whoever is doing it, and
moving a run into a folder is the act of publishing it to the team. There is
no per-folder visibility any more — a folder is the org's, always.

What that widens, and what it deliberately does not:

  read (list, drawer, /reports)  every member, for a GROUPED run
  re-run                         every member, for a GROUPED run (D5) — the
                                 new run is attributed to whoever fired it
  move / rename / delete         the run's owner, or an org_admin (D4)
  feedback                       owner / org_admin only (D7) — it mutates the
                                 org's learning store, so D5 does not reach it

Referenced by: src/backend/core/run_registry.py, src/backend/auth/ownership.py,
               src/backend/api/history_endpoints.py,
               src/backend/api/groups_endpoints.py.
Depends on: tests/test_auth/conftest.py (auth_isolated_schema),
            tests/test_api/test_groups.py (_team_of_two, _seed_run_for).
"""

import json
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


class _Req:
    """Bare stand-in for a Starlette Request — authorize_report_access only
    reads .headers / .cookies."""

    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.cookies = {}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from src.backend.main import app
    return TestClient(app)


from tests.test_api.test_groups import (  # noqa: E402
    _auth, _login, _register, _seed_run_for, _team_of_two,
)


@pytest.fixture
def shared(client):
    """Two members of one org. A is org_admin, B is a plain member.

    A has one run filed into the shared folder and one left ungrouped; the
    grouped one is what B is supposed to see and the ungrouped one is what B
    is supposed to be denied. Returns everything the assertions need.
    """
    tok_a, tok_b, org_id = _team_of_two(client)
    grouped = _seed_run_for(client, tok_a, "A completed checkout test")
    wip = _seed_run_for(client, tok_a, "A work in progress")
    gid = client.post("/api/groups", json={"name": "Completed"},
                      headers=_auth(tok_a)).json()["group_id"]
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [grouped], "group_id": gid},
                   headers=_auth(tok_a))
    assert r.status_code == 200, r.text
    return {"tok_a": tok_a, "tok_b": tok_b, "org_id": org_id,
            "gid": gid, "grouped": grouped, "wip": wip}


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

def test_a_grouped_run_is_visible_to_every_member(client, shared):
    """The whole point: B did not write this run and is not an admin, and the
    run is still theirs to see because it sits in one of the org's folders."""
    rows = client.get("/api/history", headers=_auth(shared["tok_b"])).json()["runs"]
    assert shared["grouped"] in [r["run_id"] for r in rows], (
        "a member cannot see a completed run filed into the org's folder")


def test_an_ungrouped_run_stays_private_to_its_author(client, shared):
    """Work in progress is nobody else's business until it is filed."""
    rows = client.get("/api/history", headers=_auth(shared["tok_b"])).json()["runs"]
    assert shared["wip"] not in [r["run_id"] for r in rows], (
        "a member's unfinished work leaked to a peer")


def test_the_folder_filter_lists_every_members_run(client, shared):
    """Filtering by the shared folder shows the folder's whole contents, not
    the caller's slice of it — the defect that made sharing cosmetic."""
    b_run = _seed_run_for(client, shared["tok_b"], "B completed login test")
    assert client.put(
        "/api/groups/assignments",
        json={"run_ids": [b_run], "group_id": shared["gid"]},
        headers=_auth(shared["tok_b"])).status_code == 200

    page = client.get(f"/api/history?group={shared['gid']}",
                      headers=_auth(shared["tok_b"])).json()
    assert sorted(r["run_id"] for r in page["runs"]) == sorted(
        [shared["grouped"], b_run])
    assert page["total"] == 2


def test_the_folder_chip_equals_the_folder_table_for_a_member(client, shared):
    """The chip and the table must describe ONE set for a plain member too —
    both were 1 when the answer is 2."""
    b_run = _seed_run_for(client, shared["tok_b"], "B second test")
    client.put("/api/groups/assignments",
               json={"run_ids": [b_run], "group_id": shared["gid"]},
               headers=_auth(shared["tok_b"]))

    listed = client.get("/api/groups", headers=_auth(shared["tok_b"])).json()
    folder = next(g for g in listed["groups"] if g["group_id"] == shared["gid"])
    table = client.get(f"/api/history?group={shared['gid']}",
                       headers=_auth(shared["tok_b"])).json()
    assert folder["run_count"] == table["total"] == 2


def test_the_ungrouped_chip_counts_only_the_callers_own_work(client, shared):
    """Ungrouped is per-person even though grouped is per-org, so the chip
    must not pick up a peer's in-progress run."""
    b_wip = _seed_run_for(client, shared["tok_b"], "B work in progress")
    listed = client.get("/api/groups", headers=_auth(shared["tok_b"])).json()
    page = client.get("/api/history?group=ungrouped",
                      headers=_auth(shared["tok_b"])).json()
    assert [r["run_id"] for r in page["runs"]] == [b_wip]
    assert listed["ungrouped_count"] == page["total"] == 1


# ---------------------------------------------------------------------------
# What the rule widens: drawer, report, re-run
# ---------------------------------------------------------------------------

def test_a_member_opens_a_grouped_runs_detail(client, shared):
    r = client.get(f"/api/history/{shared['grouped']}", headers=_auth(shared["tok_b"]))
    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == shared["grouped"]


def test_a_member_is_denied_an_ungrouped_runs_detail(client, shared):
    r = client.get(f"/api/history/{shared['wip']}", headers=_auth(shared["tok_b"]))
    assert r.status_code == 404, r.text


def test_a_member_may_download_a_grouped_runs_report(shared):
    """D3: the whole team reads a published test's log, credentials included."""
    from src.backend.auth.jwt_utils import authorize_report_access
    assert authorize_report_access(_Req(shared["tok_b"]), shared["grouped"]) is None


def test_a_member_may_not_download_an_ungrouped_runs_report(shared):
    from src.backend.auth.jwt_utils import authorize_report_access
    denied = authorize_report_access(_Req(shared["tok_b"]), shared["wip"])
    assert denied is not None and denied.status_code == 403


def test_a_member_may_rerun_a_grouped_run(client, shared):
    """D5. Only the authorization gate is under test — the 404 is what the
    old rule answered, and anything past it means the gate opened."""
    r = client.post("/execute-test", json={"rerun_of": shared["grouped"]},
                    headers=_auth(shared["tok_b"]))
    assert r.status_code != 404, "a member was refused a re-run of a shared test"


def test_a_member_may_not_rerun_an_ungrouped_run(client, shared):
    r = client.post("/execute-test", json={"rerun_of": shared["wip"]},
                    headers=_auth(shared["tok_b"]))
    assert r.status_code == 404, r.text


def test_feedback_on_a_grouped_run_stays_owner_only(client, shared):
    """D7: D5 opens the re-run, NOT the learning store. Conflict detection
    fires with the RUN's org, so a peer shaping it is a different decision."""
    r = client.post("/api/feedback",
                    json={"workflow_id": shared["grouped"],
                          "feedback_text": "wrong locator",
                          "feedback_type": "completely_wrong"},
                    headers=_auth(shared["tok_b"]))
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# What the rule does NOT widen: filing authority
# ---------------------------------------------------------------------------

def test_a_member_cannot_move_a_run_they_do_not_own(client, shared):
    """D4: seeing a shared run is not authority over it. Only its owner or an
    org_admin files it."""
    other = client.post("/api/groups", json={"name": "Archive"},
                        headers=_auth(shared["tok_b"])).json()["group_id"]
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [shared["grouped"]], "group_id": other},
                   headers=_auth(shared["tok_b"]))
    assert r.status_code == 404, r.text


def test_an_org_admin_moves_any_run_in_the_org(client, shared):
    b_run = _seed_run_for(client, shared["tok_b"], "B run")
    r = client.put("/api/groups/assignments",
                   json={"run_ids": [b_run], "group_id": shared["gid"]},
                   headers=_auth(shared["tok_a"]))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Attribution (D8) — who wrote it, and who may act on it
# ---------------------------------------------------------------------------

def test_every_row_names_its_author(client, shared):
    """Once a folder is shared, an unattributed row makes the org unable to
    say who wrote what. The email ships to every caller; the internal user_id
    stays admin-only."""
    rows = client.get("/api/history", headers=_auth(shared["tok_b"])).json()["runs"]
    row = next(r for r in rows if r["run_id"] == shared["grouped"])
    assert row.get("user_email"), "a shared run reached a peer with no author"
    assert "user_id" not in row, "the internal user id leaked to a member"


def test_rows_carry_whether_the_caller_may_move_them(client, shared):
    """The SPA draws Move from this flag instead of re-deriving authority —
    a control that can only 404 is not offered."""
    rows = client.get("/api/history", headers=_auth(shared["tok_b"])).json()["runs"]
    by_id = {r["run_id"]: r for r in rows}
    assert by_id[shared["grouped"]]["can_move"] is False, (
        "a peer's run was offered a Move control that always 404s")

    own = _seed_run_for(client, shared["tok_b"], "B own run")
    rows = client.get("/api/history", headers=_auth(shared["tok_b"])).json()["runs"]
    assert next(r for r in rows if r["run_id"] == own)["can_move"] is True


# ---------------------------------------------------------------------------
# The published half is anchored to the CALLER'S org, not to the raw column
# ---------------------------------------------------------------------------

def _orgless_token(client, email):
    """A real, active user holding a token that carries no org claim.

    ownership.caller_can_access rule 4 keeps this caller a live contract, and
    the list has to agree with it. Login cannot mint one today
    (_token_payload provisions an org first), so it is minted directly —
    which is also what the harness requires, since test_api/conftest.py stubs
    the admin repo without a token_version and a re-login would 401.
    """
    import jwt as _jwt
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.core.config import settings

    tok = _register(client, email)
    claims = _jwt.decode(tok, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    orgless = create_access_token({
        "id": claims["sub"], "email": claims["email"], "role": "user",
        "display_name": "", "org_id": None, "org_role": None,
        "status": "active", "token_version": claims.get("tv", 0),
    })
    return tok, orgless


def test_a_token_with_no_org_sees_only_its_own_runs(client, shared):
    """Three layers disagreed at once: the list handed an org-less identity
    another org's grouped run — author email, query, folder name, report flag
    — while the drawer answered 404 for the same id and /api/groups showed
    nothing at all. The list was the one that was wrong: with no org there is
    no org anything could have been published to."""
    own_tok, orgless = _orgless_token(client, f"nomad-{uuid.uuid4().hex[:8]}@e.com")
    own = _seed_run_for(client, own_tok, "the org-less caller's own run")

    page = client.get("/api/history", headers=_auth(orgless)).json()
    assert [r["run_id"] for r in page["runs"]] == [own]
    assert shared["grouped"] not in [r["run_id"] for r in page["runs"]]
    assert all(r["group_name"] is None for r in page["runs"]), (
        "a foreign org's folder name reached a caller with no org")

    # …and the two layers that were already right still say the same thing.
    assert client.get(f"/api/history/{shared['grouped']}",
                      headers=_auth(orgless)).status_code == 404
    listed = client.get("/api/groups", headers=_auth(orgless)).json()
    ungrouped = client.get("/api/history?group=ungrouped",
                           headers=_auth(orgless)).json()
    assert listed["groups"] == []
    assert listed["ungrouped_count"] == ungrouped["total"] == 1


def test_the_drawer_hides_a_foreign_folder_from_a_token_with_no_org(client):
    """The list already refuses a caller with no org a foreign folder's name;
    the drawer read the same folder through an UNSCOPED join and handed it
    over. Their OWN run is the only one they can open at all (rule 4), which
    is exactly where the leak sat, and it is also the one place the drawer and
    the list could disagree about the same row."""
    from src.backend.core.run_registry import get_run_registry

    own_tok, orgless = _orgless_token(client, f"drw-{uuid.uuid4().hex[:8]}@e.com")
    rid = _seed_run_for(client, own_tok, "the org-less caller's own run")
    reg = get_run_registry()
    foreign = reg.create_group(
        f"org-{uuid.uuid4().hex[:8]}", "u-outsider", "Theirs")["group_id"]
    with reg._pool.connection() as conn:
        conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                     (foreign, rid))

    drawer = client.get(f"/api/history/{rid}", headers=_auth(orgless))
    assert drawer.status_code == 200, drawer.text
    assert drawer.json()["group_id"] is None
    assert drawer.json()["group_name"] is None, (
        "the drawer named a folder the list refuses this caller")

    row = next(r for r in client.get("/api/history", headers=_auth(orgless))
               .json()["runs"] if r["run_id"] == rid)
    assert row["group_name"] == drawer.json()["group_name"]


@pytest.fixture
def stray(client):
    """A team of three where B's ungrouped run points at a folder in ANOTHER
    org — the state the read must not treat as published.

    Not reachable through the product today (record_start's _fileable_group_id
    and assign_runs both bind the row's org, and nothing else writes either
    column), so it is forced with SQL. The point is that the READ must not
    depend on that invariant: A is org_admin, B owns the run, C is the peer.
    """
    from src.backend.auth.jwt_utils import decode_token
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.core.run_registry import get_run_registry

    tok_a, tok_b, org_id = _team_of_two(client)
    email_c = f"tc-{uuid.uuid4().hex[:8]}@e.com"
    uid_c = decode_token(_register(client, email_c))["user_id"]
    OrgRepository().add_member(org_id, uid_c)
    tok_c = _login(client, email_c)

    rid = _seed_run_for(client, tok_b, "B work in progress")
    reg = get_run_registry()
    foreign = reg.create_group(
        f"org-{uuid.uuid4().hex[:8]}", "u-outsider", "Theirs")["group_id"]
    with reg._pool.connection() as conn:
        conn.execute("UPDATE test_runs SET group_id = %s WHERE run_id = %s",
                     (foreign, rid))
    return {"tok_a": tok_a, "tok_b": tok_b, "tok_c": tok_c, "run": rid}


def test_a_foreign_folder_does_not_publish_a_run_to_the_org(client, stray):
    """A folder outside the org cannot publish anything TO the org. The row
    displayed as Ungrouped to a peer while the /reports gate let them open
    it, so an unfiled run became org-readable — log.html and whatever
    credentials its author typed included."""
    from src.backend.auth.jwt_utils import authorize_report_access

    peer = _auth(stray["tok_c"])
    rows = client.get("/api/history", headers=peer).json()["runs"]
    assert stray["run"] not in [r["run_id"] for r in rows]

    ungrouped = client.get("/api/history?group=ungrouped", headers=peer).json()
    assert stray["run"] not in [r["run_id"] for r in ungrouped["runs"]]
    listed = client.get("/api/groups", headers=peer).json()
    assert listed["ungrouped_count"] == ungrouped["total"]

    denied = authorize_report_access(_Req(stray["tok_c"]), stray["run"])
    assert denied is not None and denied.status_code == 403


def test_the_owner_of_a_foreign_folder_run_still_reads_it(client, stray):
    """The other half: it is still their own work. Closing the peer's route
    must not close the author's — the first half of the predicate is what
    answers for them, and it is untouched by the folder."""
    from src.backend.auth.jwt_utils import authorize_report_access

    rows = client.get("/api/history", headers=_auth(stray["tok_b"])).json()["runs"]
    assert stray["run"] in [r["run_id"] for r in rows]
    assert client.get(f"/api/history/{stray['run']}",
                      headers=_auth(stray["tok_b"])).status_code == 200
    assert authorize_report_access(_Req(stray["tok_b"]), stray["run"]) is None


def test_a_platform_admins_foreign_rows_cannot_be_moved(client):
    """The one caller whose table spans orgs. Their folders are their own
    org's, so every foreign row's Move control could only ever fail."""
    import jwt as _jwt
    from src.backend.auth.jwt_utils import create_access_token
    from src.backend.core.config import settings

    owner = _register(client, f"pm-{uuid.uuid4().hex[:8]}@e.com")
    foreign = _seed_run_for(client, owner, "another org's run")
    admin = _register(client, f"pm2-{uuid.uuid4().hex[:8]}@e.com")
    own = _seed_run_for(client, admin, "the admin's own run")
    claims = _jwt.decode(admin, settings.JWT_SECRET_KEY, algorithms=["HS256"])
    admin_tok = create_access_token({
        "id": claims["sub"], "email": claims["email"], "role": "admin",
        "display_name": "", "org_id": claims["org_id"],
        "org_role": claims["org_role"], "status": "active",
        "token_version": claims.get("tv", 0),
    })

    rows = {r["run_id"]: r for r in
            client.get("/api/history", headers=_auth(admin_tok)).json()["runs"]}
    assert rows[foreign]["can_move"] is False
    assert rows[own]["can_move"] is True


# ---------------------------------------------------------------------------
# Folder names are the org's (D2), and mutations say what they did (D8)
# ---------------------------------------------------------------------------

def test_a_folder_name_is_taken_for_the_whole_org(client, shared):
    """One name, one folder, whoever created it — so a testcase can only ever
    carry one folder name."""
    r = client.post("/api/groups", json={"name": "completed"},
                    headers=_auth(shared["tok_b"]))
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == 'A group named "completed" already exists'


@pytest.fixture
def audit_table():
    """The floor writes to a table main.py creates at startup; an isolated
    auth schema has none, and the floor fails open, so without this the
    mutations below succeed and record nothing."""
    from src.backend.core.audit_log import init_audit_log
    init_audit_log()


def _audit_rows(path):
    from src.backend.auth.db import get_pool
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT actor_email, detail FROM audit_log WHERE path = %s "
            "ORDER BY id DESC LIMIT 1", (path,)).fetchall()


def test_a_move_records_what_moved_and_where(client, shared, audit_table):
    """The audit floor already names the actor; without detail it cannot name
    the run or the folder, which is the half that makes it useful."""
    b_run = _seed_run_for(client, shared["tok_b"], "B filed run")
    client.put("/api/groups/assignments",
               json={"run_ids": [b_run], "group_id": shared["gid"]},
               headers=_auth(shared["tok_b"]))

    rows = _audit_rows("/api/groups/assignments")
    assert rows, "the move was not audited at all"
    detail = json.loads(rows[0]["detail"] or "null")
    assert detail and detail.get("group_id") == shared["gid"]
    assert b_run in detail.get("run_ids", [])


def test_folder_creation_and_deletion_record_the_folder(client, shared, audit_table):
    """Any member creates; only an org_admin deletes. Both land in audit_log
    naming the folder, so the org can answer who made it and who removed it."""
    created = client.post("/api/groups", json={"name": "Auditable"},
                          headers=_auth(shared["tok_b"])).json()
    rows = _audit_rows("/api/groups")
    assert rows and json.loads(rows[0]["detail"])["name"] == "Auditable"

    path = f"/api/groups/{created['group_id']}"
    assert client.delete(path, headers=_auth(shared["tok_b"])).status_code == 404, (
        "a plain member must not be able to delete a folder")
    assert client.delete(path, headers=_auth(shared["tok_a"])).status_code == 204
    rows = _audit_rows(path)
    assert rows and json.loads(rows[0]["detail"])["group_id"] == created["group_id"]


def test_a_folder_delete_records_which_runs_it_ungrouped(client, shared, audit_table):
    """D9: deleting un-publishes every run the folder held. Without the run
    ids the audit log cannot answer what stopped being shared — the folder
    id alone says a folder was removed, not what was inside it."""
    path = f"/api/groups/{shared['gid']}"
    assert client.delete(path, headers=_auth(shared["tok_a"])).status_code == 204

    rows = _audit_rows(path)
    detail = json.loads(rows[0]["detail"])
    assert detail["run_ids"] == [shared["grouped"]]
    assert detail["run_ids_truncated"] is False


def test_a_folder_delete_over_the_cap_is_bounded_and_flagged(
        client, shared, audit_table):
    """A folder can hold far more runs than any one assignment can move, so
    its audit record needs its own bound — and a truncated list must say so
    plainly, or it reads as complete when it is not. Driven through a
    stubbed registry: seeding _DELETE_AUDIT_RUN_IDS_MAX + 1 real rows would
    make this test glacial for no extra coverage of the endpoint's own
    capping arithmetic."""
    from unittest.mock import MagicMock, patch

    from src.backend.api import groups_endpoints

    cap = groups_endpoints._DELETE_AUDIT_RUN_IDS_MAX
    over_cap_ids = [str(uuid.uuid4()) for _ in range(cap + 1)]

    def fake_delete_group(org_id, user_id, is_org_admin, group_id,
                           audit_run_ids=None):
        if audit_run_ids is not None:
            audit_run_ids.extend(over_cap_ids)
        return True

    mock_reg = MagicMock()
    mock_reg.delete_group.side_effect = fake_delete_group
    gid = str(uuid.uuid4())

    with patch.object(groups_endpoints, "get_run_registry",
                      return_value=mock_reg):
        resp = client.delete(f"/api/groups/{gid}", headers=_auth(shared["tok_a"]))
    assert resp.status_code == 204

    rows = _audit_rows(f"/api/groups/{gid}")
    detail = json.loads(rows[0]["detail"])
    assert len(detail["run_ids"]) == cap
    assert detail["run_ids"] == over_cap_ids[:cap]
    assert detail["run_ids_truncated"] is True


def test_a_folder_rename_records_the_old_name_alongside_the_new(
        client, shared, audit_table):
    """The new name alone can't answer what a folder USED to be called."""
    gid = client.post("/api/groups", json={"name": "Before"},
                      headers=_auth(shared["tok_a"])).json()["group_id"]
    path = f"/api/groups/{gid}"
    assert client.patch(path, json={"name": "After"},
                        headers=_auth(shared["tok_a"])).status_code == 200

    rows = _audit_rows(path)
    detail = json.loads(rows[0]["detail"])
    assert detail["name"] == "After"
    assert detail["old_name"] == "Before"


def test_a_refused_delete_records_no_detail(client, shared, audit_table):
    """A plain member's own folder still refuses THEM at delete (org_admin
    only, D4) — the refusal must not read as a mutation that happened."""
    mine = client.post("/api/groups", json={"name": "Member owned"},
                       headers=_auth(shared["tok_b"])).json()["group_id"]
    path = f"/api/groups/{mine}"
    assert client.delete(path, headers=_auth(shared["tok_b"])).status_code == 404

    rows = _audit_rows(path)
    assert rows and rows[0]["detail"] is None


def test_a_refused_rename_records_no_detail(client, shared, audit_table):
    """Neither the creator nor an org_admin: refused, and just as silent."""
    theirs = client.post("/api/groups", json={"name": "Admin owned"},
                         headers=_auth(shared["tok_a"])).json()["group_id"]
    path = f"/api/groups/{theirs}"
    assert client.patch(path, json={"name": "Hax"},
                        headers=_auth(shared["tok_b"])).status_code == 404

    rows = _audit_rows(path)
    assert rows and rows[0]["detail"] is None


def test_only_an_org_admin_deletes_a_folder(client, shared):
    """Owner decision, 2026-08-23. Deleting returns every run inside to
    Ungrouped, which un-shares them from the whole org — an org-level
    consequence, so an org-level authority, whoever created the folder.

    The member keeps everything narrower: they still RENAME the folder they
    made, and still move their own runs out of it."""
    mine = client.post("/api/groups", json={"name": "Member folder"},
                       headers=_auth(shared["tok_b"])).json()["group_id"]

    assert client.patch(f"/api/groups/{mine}", json={"name": "Renamed"},
                        headers=_auth(shared["tok_b"])).status_code == 200
    assert client.delete(f"/api/groups/{mine}",
                         headers=_auth(shared["tok_b"])).status_code == 404
    # Refused, not half-done: the folder is still there under its new name.
    listed = client.get("/api/groups", headers=_auth(shared["tok_b"])).json()["groups"]
    assert "Renamed" in [g["name"] for g in listed]

    assert client.delete(f"/api/groups/{mine}",
                         headers=_auth(shared["tok_a"])).status_code == 204


# ---------------------------------------------------------------------------
# The fail-closed rule the published-run rule must not quietly widen
# ---------------------------------------------------------------------------

def test_an_unowned_run_stays_platform_admin_only_even_in_a_folder(client, shared):
    """A row nobody owns is readable only by a platform admin — a standing
    rule, because /reports serves credentials somebody typed and no user is
    accountable for that row. Publishing it into a folder must NOT be a way
    around it, or an org_admin could hand the org a legacy run by filing it.

    Both halves of the predicate carry the same `user_id IS NOT NULL` guard —
    the SQL in run_registry._VISIBLE_RUN_SQL and the rule in
    auth/ownership._can — so the list cannot offer a row whose drawer and
    report would then refuse it. This is what pins them together.
    """
    from src.backend.core.run_registry import get_run_registry

    reg = get_run_registry()
    rid = _seed_run_for(client, shared["tok_a"], "an unowned legacy run")
    with reg._pool.connection() as conn:
        conn.execute(
            "UPDATE test_runs SET user_id = NULL, user_email = NULL, "
            "group_id = %s WHERE run_id = %s", (shared["gid"], rid))

    for who, tok in (("author's org_admin", shared["tok_a"]),
                     ("a plain member", shared["tok_b"])):
        rows = client.get("/api/history", headers=_auth(tok)).json()["runs"]
        assert rid not in [r["run_id"] for r in rows], f"{who} listed it"
        assert client.get(f"/api/history/{rid}",
                          headers=_auth(tok)).status_code == 404, who


# ---------------------------------------------------------------------------
# org_id is read only to compute can_move (history_endpoints.py) and is not
# part of the public run shape — the list row has always popped it before the
# response goes out. run_detail must agree, and must pop it only AFTER
# can_move is computed: popping earlier would silently zero out an
# org_admin's can_move on a colleague's grouped run.
# ---------------------------------------------------------------------------

def test_history_list_and_detail_agree_on_org_id(client, shared):
    bs_run = _seed_run_for(client, shared["tok_b"], "B's own test")
    assert client.put(
        "/api/groups/assignments",
        json={"run_ids": [bs_run], "group_id": shared["gid"]},
        headers=_auth(shared["tok_b"]),
    ).status_code == 200

    rows = client.get("/api/history", headers=_auth(shared["tok_a"])).json()["runs"]
    row = next(r for r in rows if r["run_id"] == bs_run)
    assert "org_id" not in row
    assert row["can_move"] is True  # org_admin over a colleague's grouped run

    detail = client.get(f"/api/history/{bs_run}",
                        headers=_auth(shared["tok_a"])).json()
    assert "org_id" not in detail
    assert detail["can_move"] is True
