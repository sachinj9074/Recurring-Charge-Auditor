"""User-login registry: signup, authentication, password change, usage cap."""

import datetime

import pytest

from src import crypto
from src.storage import InMemoryBackend
from src.users import UserStore, UserError, normalize_username


def _store():
    return UserStore(InMemoryBackend())


def test_normalize_username():
    assert normalize_username("  Asha Rao ") == "asharao"
    assert normalize_username("a.b_c-1") == "a.b_c-1"
    assert normalize_username("!!!") == ""


def test_create_and_authenticate():
    us = _store()
    acct = us.create("asha", "Asha", "password123")
    assert acct["user_id"] == "asha"
    assert "auth_hash" in acct and "keyset" in acct

    result = us.authenticate("asha", "password123")
    assert result is not None
    got, data_key = result
    assert got["user_id"] == "asha"
    # The password unlocks the same data key the keyset wraps.
    assert crypto.open_keyset("password123", acct["keyset"]) == data_key


def test_authenticate_wrong_password():
    us = _store()
    us.create("asha", "Asha", "password123")
    assert us.authenticate("asha", "nope") is None
    assert us.authenticate("ghost", "password123") is None


def test_duplicate_username_rejected():
    us = _store()
    us.create("asha", "Asha", "password123")
    with pytest.raises(UserError):
        us.create("Asha", "Asha Again", "password456")


def test_weak_password_rejected():
    us = _store()
    with pytest.raises(UserError):
        us.create("asha", "Asha", "short")


def test_empty_username_rejected():
    us = _store()
    with pytest.raises(UserError):
        us.create("!!!", "Nobody", "password123")


def test_change_password_rewraps_same_key():
    us = _store()
    acct = us.create("asha", "Asha", "password123")
    original_key = crypto.open_keyset("password123", acct["keyset"])

    assert us.change_password("asha", "password123", "newpassword456")
    assert us.authenticate("asha", "password123") is None
    _, dk = us.authenticate("asha", "newpassword456")
    assert dk == original_key                # data key preserved across change


def test_change_password_wrong_old():
    us = _store()
    us.create("asha", "Asha", "password123")
    assert us.change_password("asha", "wrong", "newpassword456") is False


def test_usage_cap_counter():
    us = _store()
    us.create("asha", "Asha", "password123")
    assert us.statements_today("asha") == 0
    assert us.record_statement("asha") == 1
    assert us.record_statement("asha", 2) == 3
    assert us.statements_today("asha") == 3


def test_usage_prunes_old_days():
    us = _store()
    us.create("asha", "Asha", "password123")
    # Inject an old usage day directly, then record today and confirm the old day
    # is pruned.
    acct = us._load("asha")
    old_day = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    acct["usage"] = {old_day: 5}
    us._save(acct)
    us.record_statement("asha")
    assert old_day not in us._load("asha")["usage"]
