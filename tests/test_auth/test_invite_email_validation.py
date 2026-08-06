"""The invite email validator must agree with the registration one.

An invitation is matched at signup by comparing it to the address the user
actually REGISTERED with (provisioning.match_invite_on_signup ->
find_open_by_email). Registration runs every address through
auth.endpoints._normalize_email, whose regex requires a local part, an `@`, and
a dotted domain. So an invitation the registration validator would reject can
never be matched by anyone: it is a dead row that shows up in the org owner's
open-invitation list forever.

These tests pin the two validators together. They need no database — the
Pydantic model is the unit under test.
"""
import pytest
from pydantic import ValidationError

from src.backend.auth.endpoints import _normalize_email
from src.backend.auth.org_endpoints import _InviteBody

# Addresses registration rejects. Every one of these previously produced an
# invitation that no signup could ever consume.
UNMATCHABLE = [
    "a@",                # no domain
    "@b.com",            # no local part
    "_bad_",             # no @ at all is caught, but see below
    "a@b",               # no dot in the domain
    "a b@c.com",         # inner whitespace
    "a@b.com\nX-Injected: 1",   # control character
]

VALID = ["Bob@Example.COM", "  bob@example.com  "]


@pytest.mark.parametrize("addr", UNMATCHABLE)
def test_an_address_registration_rejects_cannot_be_invited(addr):
    """RED before the fix for every case except the bare `_bad_`: the old
    validator asked only for an `@` and a length bound."""
    with pytest.raises(ValidationError):
        _InviteBody(email=addr)


@pytest.mark.parametrize("addr", VALID)
def test_a_valid_address_is_normalised_exactly_as_registration_normalises_it(addr):
    assert _InviteBody(email=addr).email == _normalize_email(addr)


def test_the_two_validators_agree_on_every_case():
    """The point of the change: one rule, not two that drift apart."""
    for addr in UNMATCHABLE:
        with pytest.raises(ValueError):
            _normalize_email(addr)
