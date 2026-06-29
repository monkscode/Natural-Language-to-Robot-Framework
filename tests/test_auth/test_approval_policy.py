# tests/test_auth/test_approval_policy.py
"""Unit (pure): only the platform owner may approve, today. The seam exists so
delegation can be widened later without touching call sites."""
from src.backend.auth.approval_policy import can_approve


def test_admin_can_approve():
    assert can_approve({"role": "admin"}) is True


def test_org_admin_cannot_approve_yet():
    assert can_approve({"role": "user", "org_role": "org_admin"}) is False


def test_plain_user_cannot_approve():
    assert can_approve({"role": "user"}) is False
