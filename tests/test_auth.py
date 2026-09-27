"""Password hashing, demo identity, and the access-code gate."""

import json

from src import auth


def test_hash_verify_round_trip():
    h = auth.hash_password("s3cret-password")
    assert auth.verify_password("s3cret-password", h)
    assert not auth.verify_password("wrong", h)


def test_hash_is_salted():
    assert auth.hash_password("same") != auth.hash_password("same")   # random salt


def test_verify_rejects_malformed_hash():
    assert not auth.verify_password("pw", "not-a-hash")
    assert not auth.verify_password("pw", "")
    assert not auth.verify_password("pw", "bogus$1$2$3$4")


def test_demo_authentication(tmp_path, monkeypatch):
    path = tmp_path / "demo_users.json"
    path.write_text(json.dumps([
        {"user_id": "asha", "name": "Asha", "password_hash": auth.hash_password("demo-pass"),
         "password_hint": "demo-pass"},
    ]), encoding="utf-8")
    monkeypatch.setattr(auth, "DEMO_USERS_PATH", str(path))

    assert [u.user_id for u in auth.demo_users()] == ["asha"]
    ok = auth.authenticate_demo("asha", "demo-pass")
    assert ok is not None and ok.is_demo and ok.name == "Asha"
    assert auth.authenticate_demo("asha", "nope") is None
    assert auth.authenticate_demo("ghost", "demo-pass") is None


def test_demo_users_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DEMO_USERS_PATH", str(tmp_path / "nope.json"))
    assert auth.demo_users() == []


def test_access_code_gate(monkeypatch):
    monkeypatch.delenv("REAL_ACCESS_CODE", raising=False)
    assert not auth.access_code_required()
    assert auth.check_access_code("anything")          # open when unset

    monkeypatch.setenv("REAL_ACCESS_CODE", "invite-42")
    assert auth.access_code_required()
    assert auth.check_access_code("invite-42")
    assert not auth.check_access_code("invite-99")
    assert not auth.check_access_code("")
