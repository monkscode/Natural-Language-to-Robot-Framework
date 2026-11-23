"""Integration tests for the encrypted per-org secret store (live Postgres, isolated schema)."""
import uuid
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.backend.auth import secrets as store
from src.backend.auth.db import get_pool
from src.backend.auth.org_repository import OrgRepository
from src.backend.auth.repository import UserRepository
from src.backend.auth.secrets import SecretRuleError, SecretsUnavailable
from src.backend.core.config import settings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("auth_isolated_schema")]

MARKER = "pw_marker_131"


@pytest.fixture
def key():
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()):
        yield


@pytest.fixture
def two_orgs(key):
    store.init_secrets_db()
    users, orgs = UserRepository(), OrgRepository()
    emails = [f"sec-{uuid.uuid4().hex[:8]}@x.com" for _ in range(2)]
    try:
        u1 = users.create_user(emails[0], "password123", "One")
        u2 = users.create_user(emails[1], "password123", "Two")
        o1 = orgs.create_team_org("SecA", str(u1["id"]))
        o2 = orgs.create_team_org("SecB", str(u2["id"]))
        yield o1, str(u1["id"]), o2, str(u2["id"])
    finally:
        with get_pool().connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
            conn.commit()


def test_set_list_get_and_encrypted_at_rest(two_orgs):
    o1, u1, _, _ = two_orgs
    added, replaced = store.set_many(o1, u1, [("SAUCE_PASSWORD", MARKER)])
    assert (added, replaced) == (["SAUCE_PASSWORD"], [])
    listed = store.list_names(o1)
    assert [r["name"] for r in listed] == ["SAUCE_PASSWORD"]
    assert MARKER not in repr(listed)
    with get_pool().connection() as conn:
        raw = conn.execute("SELECT value_enc FROM org_secrets WHERE org_id = %s", (o1,)).fetchone()
    assert MARKER.encode() not in bytes(raw["value_enc"])
    assert store.get_values(o1, ["SAUCE_PASSWORD", "MISSING"]) == {"SAUCE_PASSWORD": MARKER}


def test_second_set_reports_replaced(two_orgs):
    o1, u1, _, _ = two_orgs
    store.set_many(o1, u1, [("A", "one")])
    added, replaced = store.set_many(o1, u1, [("A", "two"), ("B", "b")])
    assert (added, replaced) == (["B"], ["A"])
    assert store.get_values(o1, ["A"]) == {"A": "two"}


def test_orgs_keep_separate_values(two_orgs):
    o1, u1, o2, u2 = two_orgs
    store.set_many(o1, u1, [("SAME", "val-one")])
    store.set_many(o2, u2, [("SAME", "val-two")])
    assert store.get_values(o1, ["SAME"]) == {"SAME": "val-one"}
    assert store.get_values(o2, ["SAME"]) == {"SAME": "val-two"}


def test_delete_true_then_false(two_orgs):
    o1, u1, _, _ = two_orgs
    store.set_many(o1, u1, [("GONE", "x1")])
    assert store.delete(o1, "GONE") is True
    assert store.delete(o1, "GONE") is False
    assert store.list_names(o1) == []


def test_201st_name_refused_and_nothing_saved(two_orgs):
    o1, u1, _, _ = two_orgs
    store.set_many(o1, u1, [(f"N{i}", "v") for i in range(200)])
    with pytest.raises(SecretRuleError) as exc:
        store.set_many(o1, u1, [("N0", "changed"), ("EXTRA", "v")])
    assert "200" in str(exc.value)
    assert len(store.list_names(o1)) == 200
    assert store.get_values(o1, ["N0", "EXTRA"]) == {"N0": "v"}


def test_org_of(two_orgs):
    o1, u1, _, _ = two_orgs
    assert store.org_of(u1) == o1
    assert store.org_of(None) is None
    assert store.org_of(str(uuid.uuid4())) is None


@pytest.mark.parametrize("bad_key", ["", "not-a-fernet-key"])
def test_unset_or_malformed_key_is_unavailable(two_orgs, bad_key):
    o1, u1, _, _ = two_orgs
    with patch.object(settings, "SECRETS_ENCRYPTION_KEY", bad_key):
        for call in (
            lambda: store.list_names(o1),
            lambda: store.set_many(o1, u1, [("A", "v")]),
            lambda: store.delete(o1, "A"),
            lambda: store.get_values(o1, ["A"]),
        ):
            with pytest.raises(SecretsUnavailable) as exc:
                call()
            assert "SECRETS_ENCRYPTION_KEY" in str(exc.value)
