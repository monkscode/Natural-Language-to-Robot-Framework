"""authorize_report_access denies a user from another org, allows the owner.

Org-dimension test strategy:
- owner (org A) creates a run. Same-org org_admin (org A, different user) should
  be allowed by caller_can_read (org_admin of the run's org) but is DENIED by the
  old bare user_id == owner check — so this assertion fails RED before the wiring.
- stranger (org B) should be denied under both old and new code (not a coincidence:
  the same-org assertion is the genuine RED signal).

Referenced by: src/backend/auth/jwt_utils.py (authorize_report_access).
Depends on: tests/test_auth/conftest.py (auth_isolated_schema).
"""

import uuid

import pytest
from fastapi.responses import JSONResponse

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


class _Req:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.cookies = {}


def test_owner_allowed_other_org_denied():
    """Primary org-dimension test.

    Owner -> allowed (trivial).
    Stranger (different org) -> 403 (also trivial, but needed for completeness).
    Same-org org_admin (not owner) -> allowed by caller_can_read, denied by old
      bare user_id check.  This third case is the genuine RED gate.
    """
    from src.backend.auth.repository import UserRepository
    from src.backend.auth.org_repository import OrgRepository
    from src.backend.auth.jwt_utils import create_access_token, authorize_report_access
    from src.backend.core.run_registry import get_run_registry

    users, orgs = UserRepository(), OrgRepository()

    # Owner — creates the run
    owner = users.create_user(f"ro-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    users.set_status(str(owner["id"]), "active")
    o_org = orgs.ensure_personal_org(str(owner["id"]), owner["email"])

    # Same-org org_admin — different user, same org_id (simulate org member)
    peer = users.create_user(f"rp-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    users.set_status(str(peer["id"]), "active")

    # Stranger — different org entirely
    stranger = users.create_user(f"rs-{uuid.uuid4().hex[:8]}@e.com", "S3cretpw!")
    users.set_status(str(stranger["id"]), "active")
    s_org = orgs.ensure_personal_org(str(stranger["id"]), stranger["email"])

    rid = str(uuid.uuid4())
    get_run_registry().record_start(
        rid, {"user_id": str(owner["id"]), "email": owner["email"], "org_id": o_org},
        "report run", "passed",
    )

    owner_tok = create_access_token(
        {"id": str(owner["id"]), "email": owner["email"], "role": "user",
         "display_name": "", "org_id": o_org, "org_role": "org_admin"})

    # Peer is an org_admin of the SAME org as the owner — caller_can_read
    # allows this (org_admin of the run's org), but the old bare user_id == owner
    # comparison denies it.  This is the RED gate.
    peer_tok = create_access_token(
        {"id": str(peer["id"]), "email": peer["email"], "role": "user",
         "display_name": "", "org_id": o_org, "org_role": "org_admin"})

    stranger_tok = create_access_token(
        {"id": str(stranger["id"]), "email": stranger["email"], "role": "user",
         "display_name": "", "org_id": s_org, "org_role": "org_admin"})

    assert authorize_report_access(_Req(owner_tok), rid) is None          # allow: owner

    # RED before implementation: old code compares peer's user_id to owner's
    # user_id — they differ, so old code returns 403.  After wiring
    # caller_can_read, peer is org_admin of the run's org and is allowed.
    assert authorize_report_access(_Req(peer_tok), rid) is None            # allow: same-org admin

    denied = authorize_report_access(_Req(stranger_tok), rid)
    assert isinstance(denied, JSONResponse) and denied.status_code == 403  # deny: wrong org
