"""Integration tests for tools.make_admin (the host command that makes a platform admin).

Drives main() with sys.argv and getpass patched, on the auth isolated schema.
Skipped automatically when Postgres is unreachable.
"""

import json
import uuid
from unittest.mock import patch

import pytest

from src.backend.auth import db as auth_db
from src.backend.auth.repository import UserRepository
from src.backend.core.audit_log import init_audit_log
from tools import make_admin

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]


@pytest.fixture(autouse=True)
def _audit_table():
    init_audit_log()


def _email():
    return f"mkadmin-{uuid.uuid4().hex[:8]}@x.com"


def _cleanup(email):
    with auth_db.get_pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))
        conn.execute("DELETE FROM audit_log WHERE path = 'tools.make_admin' AND detail LIKE %s",
                     (f"%{email}%",))
        conn.commit()


def _run(email, passwords=()):
    """Run main() for `email`; returns the getpass mock."""
    with patch("sys.argv", ["make_admin", email]), \
            patch("getpass.getpass", side_effect=list(passwords)) as gp:
        make_admin.main()
    return gp


def _audit_rows(email):
    with auth_db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT source, detail FROM audit_log WHERE path = 'tools.make_admin' AND detail LIKE %s",
            (f"%{email}%",),
        ).fetchall()


def test_creates_a_missing_account_as_active_admin():
    repo, email = UserRepository(), _email()
    try:
        _run(email, ["password-one", "password-one"])
        row = repo.get_by_email(email)
        assert (row["role"], row["status"], row["is_active"]) == ("admin", "active", True)
        assert repo.verify_credentials(email, "password-one") is not None
        rows = _audit_rows(email)
        assert [r["source"] for r in rows] == ["cli"]
        detail = json.loads(rows[0]["detail"])
        assert detail["from_role"] is None
        assert detail["from_status"] is None
    finally:
        _cleanup(email)


def test_resets_the_password_of_an_account_someone_else_registered():
    repo, email = UserRepository(), _email()
    try:
        repo.create_user(email, "password-A1")  # user/pending
        before = repo.get_by_email(email)["token_version"]
        _run(email, ["password-B2", "password-B2"])
        row = repo.get_by_email(email)
        assert (row["role"], row["status"]) == ("admin", "active")
        assert repo.verify_credentials(email, "password-A1") is None
        assert repo.verify_credentials(email, "password-B2") is not None
        assert row["token_version"] >= before + 1
        assert len(_audit_rows(email)) == 1
    finally:
        _cleanup(email)


def test_promotes_a_google_account_without_asking_a_password():
    repo, email = UserRepository(), _email()
    try:
        repo.get_or_create_google_user(f"sub-{uuid.uuid4().hex}", email, "G")
        before = repo.get_by_email(email)["token_version"]
        gp = _run(email)
        gp.assert_not_called()
        row = repo.get_by_email(email)
        assert (row["role"], row["status"]) == ("admin", "active")
        assert row["token_version"] >= before + 1  # the old pending token is revoked
        assert row["hashed_password"] is None
        assert len(_audit_rows(email)) == 1
    finally:
        _cleanup(email)


def test_resets_the_password_a_google_account_also_holds():
    repo, email = UserRepository(), _email()
    try:
        repo.get_or_create_google_user(f"sub-{uuid.uuid4().hex}", email, "G")
        repo.reset_password(email, "password-A1")  # tools.reset_user_password on a Google account
        gp = _run(email, ["password-B2", "password-B2"])
        assert gp.call_count == 2
        row = repo.get_by_email(email)
        assert (row["role"], row["status"]) == ("admin", "active")
        assert repo.verify_credentials(email, "password-A1") is None
        assert repo.verify_credentials(email, "password-B2") is not None
        assert json.loads(_audit_rows(email)[0]["detail"])["password"] == "reset, every session revoked"
    finally:
        _cleanup(email)


def test_reactivates_a_suspended_account():
    repo, email = UserRepository(), _email()
    try:
        user = repo.create_user(email, "password-A1")
        repo.set_status(str(user["id"]), "suspended")
        _run(email, ["password-B2", "password-B2"])
        row = repo.get_by_email(email)
        assert (row["role"], row["status"], row["is_active"]) == ("admin", "active", True)
    finally:
        _cleanup(email)


def test_a_row_deleted_mid_run_exits_1_and_grants_nothing():
    repo, email = UserRepository(), _email()
    try:
        repo.create_user(email, "password-A1")  # user/pending
        with patch.object(UserRepository, "set_platform_role", return_value=None), \
                pytest.raises(SystemExit) as exc:
            _run(email, ["password-B2", "password-B2"])
        assert exc.value.code == 1
        assert _audit_rows(email) == []
        assert repo.get_by_email(email)["role"] == "user"
    finally:
        _cleanup(email)


def test_mismatched_confirmation_exits_2_and_creates_nothing():
    repo, email = UserRepository(), _email()
    try:
        with pytest.raises(SystemExit) as exc:
            _run(email, ["password-one", "password-two"])
        assert exc.value.code == 2
        assert repo.get_by_email(email) is None
        assert _audit_rows(email) == []
    finally:
        _cleanup(email)


def test_invalid_address_exits_2_and_writes_nothing():
    with pytest.raises(SystemExit) as exc:
        _run("not-an-email")
    assert exc.value.code == 2
    assert UserRepository().get_by_email("not-an-email") is None
