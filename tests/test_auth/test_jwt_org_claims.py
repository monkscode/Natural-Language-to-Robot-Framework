"""JWT now carries org_id + org_role; decode surfaces them (None for legacy tokens)."""

from src.backend.auth.jwt_utils import create_access_token, decode_token


def test_token_roundtrips_org_claims():
    token = create_access_token({
        "id": "u-1", "email": "a@b.com", "role": "user", "display_name": "A",
        "org_id": "org-1", "org_role": "org_admin",
    })
    decoded = decode_token(token)
    assert decoded["user_id"] == "u-1"
    assert decoded["org_id"] == "org-1"
    assert decoded["org_role"] == "org_admin"


def test_token_without_org_decodes_to_none():
    # A token minted the old way (no org keys) must still decode, org_* = None.
    token = create_access_token({
        "id": "u-2", "email": "c@d.com", "role": "user", "display_name": "C",
    })
    decoded = decode_token(token)
    assert decoded["org_id"] is None
    assert decoded["org_role"] is None
