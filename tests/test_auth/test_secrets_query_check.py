"""require_query_secrets: the 400 before any generation (no DB; store calls patched)."""
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from src.backend.auth import secrets as secrets_mod
from src.backend.auth.secrets import SecretsUnavailable, require_query_secrets

_USER = {"user_id": "u-1"}
_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _key_set():
    with patch.object(secrets_mod.settings, "SECRETS_ENCRYPTION_KEY", _KEY):
        yield


def _refusal(query, user=_USER):
    with pytest.raises(HTTPException) as exc:
        require_query_secrets(query, user)
    assert exc.value.status_code == 400
    return exc.value.detail


def test_query_without_braces_touches_no_store():
    with patch.object(secrets_mod, "org_of") as org, \
         patch.object(secrets_mod, "list_names") as names, \
         patch.object(secrets_mod, "_fernet") as fernet:
        require_query_secrets("open the page {not a secret} and log in", None)
        require_query_secrets("open the page", _USER)
    org.assert_not_called()
    names.assert_not_called()
    fernet.assert_not_called()


def test_signed_out_query_with_a_secret_is_refused():
    with patch.object(secrets_mod, "org_of") as org:
        assert _refusal("type {{PW}}", user=None) == (
            "Secrets in a request need a signed-in user.")
    org.assert_not_called()


def test_unset_key_is_refused_with_the_not_set_up_sentence():
    sentence = "Secrets are not set up on this server (SECRETS_ENCRYPTION_KEY)"
    with patch.object(secrets_mod, "org_of", return_value="org-1"), \
         patch.object(secrets_mod, "list_names", side_effect=SecretsUnavailable(sentence)):
        assert _refusal("type {{PW}}") == sentence


def test_unset_key_is_refused_even_when_the_user_has_no_org():
    with patch.object(secrets_mod, "org_of", return_value=None), \
         patch.object(secrets_mod.settings, "SECRETS_ENCRYPTION_KEY", ""):
        assert "not set up" in _refusal("type {{PW}}")


def test_unknown_name_is_named_in_the_sentence():
    with patch.object(secrets_mod, "org_of", return_value="org-1"), \
         patch.object(secrets_mod, "list_names", return_value=[{"name": "OTHER"}]):
        assert _refusal("type {{nope}}") == (
            "Unknown secret {{NOPE}}: add it on the Secrets page first.")


def test_every_missing_name_is_listed_and_held_names_are_not():
    with patch.object(secrets_mod, "org_of", return_value="org-1"), \
         patch.object(secrets_mod, "list_names", return_value=[{"name": "HELD"}]):
        assert _refusal("{{a}} {{held}} {{B}}") == (
            "Unknown secrets {{A}}, {{B}}: add them on the Secrets page first.")


def test_user_without_an_org_holds_no_secrets():
    with patch.object(secrets_mod, "org_of", return_value=None), \
         patch.object(secrets_mod, "list_names") as names, \
         patch.object(secrets_mod.settings, "SECRETS_ENCRYPTION_KEY", _KEY):
        assert _refusal("type {{PW}}") == (
            "Unknown secret {{PW}}: add it on the Secrets page first.")
    names.assert_not_called()


def test_known_names_pass_whatever_the_letter_case():
    with patch.object(secrets_mod, "org_of", return_value="org-1"), \
         patch.object(secrets_mod, "list_names", return_value=[{"name": "PW"}]):
        require_query_secrets("type {{pw}} then {{ Pw }}", _USER)
