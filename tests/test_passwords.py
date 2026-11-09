"""Password policy and hashing (FY-PW-*)."""

import pytest

from forgeyard.security import passwords


@pytest.mark.parametrize("pw, fragment", [
    ("short-one", "at least 12"),
    ("passwordpassword", "too common"),
    ("Password1234!!", "too common"),         # common word + digits/symbols
    ("aaaaaaaaaaaaaaab", "two different"),
    ("xx-alice-xx-9876", "username"),
])
def test_fy_pw_001_refused(pw, fragment):
    """FY-PW-001"""
    assert fragment in (passwords.policy_error(pw, "alice") or "")


def test_fy_pw_002_accepted():
    """FY-PW-002"""
    assert passwords.policy_error("harbour-lantern-71", "alice") is None


def test_fy_pw_003_argon2id_hash_and_verify():
    """FY-PW-003"""
    h = passwords.hash_password("harbour-lantern-71")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, "harbour-lantern-71") is True
    assert passwords.verify_password(h, "harbour-lantern-72") is False
    assert passwords.verify_password(None, "anything") is False
    assert passwords.verify_password("not-a-hash", "anything") is False
