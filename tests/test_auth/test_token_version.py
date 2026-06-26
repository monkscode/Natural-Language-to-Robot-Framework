"""Unit tests for the token_version (tv) JWT claim (DB-free).

token_version lets a user invalidate all previously-minted tokens (logout-all /
compromise) by bumping the stored counter: any token whose tv no longer matches
the DB is rejected on the DB-backed paths (/auth/me, admin).
"""
import jwt as pyjwt

from src.backend.auth.jwt_utils import _ALGORITHM, create_access_token, decode_token
from src.backend.core.config import settings


def _base_user(**extra):
    user = {"id": "u1", "email": "a@b.com", "role": "user", "display_name": "A"}
    user.update(extra)
    return user


def test_create_access_token_includes_token_version():
    token = create_access_token(_base_user(token_version=7))
    payload = pyjwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[_ALGORITHM])
    assert payload["tv"] == 7
    assert decode_token(token)["token_version"] == 7


def test_token_version_defaults_to_zero_when_absent():
    token = create_access_token(_base_user())
    assert decode_token(token)["token_version"] == 0
