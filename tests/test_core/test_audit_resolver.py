"""Audit identity resolver + safe bearer decode (no DB, no app)."""

from src.backend.core import audit_log
from src.backend.auth.jwt_utils import create_access_token


def test_resolve_actor_email_none_is_unknown():
    assert audit_log.resolve_actor_email(None) == "unknown"


def test_resolve_actor_email_empty_dict_is_unknown():
    assert audit_log.resolve_actor_email({}) == "unknown"


def test_resolve_actor_email_blank_email_is_unknown():
    assert audit_log.resolve_actor_email({"email": ""}) == "unknown"


def test_resolve_actor_email_returns_email():
    assert audit_log.resolve_actor_email({"email": "a@b.com"}) == "a@b.com"


def test_actor_from_bearer_none_header():
    assert audit_log.actor_from_bearer(None) is None


def test_actor_from_bearer_wrong_scheme():
    assert audit_log.actor_from_bearer("Basic abc") is None


def test_actor_from_bearer_malformed_token_does_not_raise():
    assert audit_log.actor_from_bearer("Bearer not-a-jwt") is None


def test_actor_from_bearer_valid_token_returns_claims():
    tok = create_access_token(
        {"id": "u1", "email": "a@b.com", "role": "user", "display_name": ""}
    )
    user = audit_log.actor_from_bearer(f"Bearer {tok}")
    assert user is not None
    assert user["email"] == "a@b.com"
    assert user["user_id"] == "u1"
