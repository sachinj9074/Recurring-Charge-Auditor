"""Password hashing, demo identity, and the access-code gate (no model calls).

This module holds the deterministic, Streamlit-free pieces of identity so they
stay unit-testable:

  - Password hashing/verification (PBKDF2-HMAC-SHA256, constant-time check).
    Real user accounts live in users.py, which composes this with the crypto
    keyset; the hash here is only the login check and reveals nothing about the
    encryption key.
  - Demo profiles loaded from config/demo_users.json. Their passwords are shown
    on the login screen (the data is synthetic), so a reviewer can log in as one
    profile, then another, and see the isolation.
  - The shared REAL_ACCESS_CODE invite gate: on a public deploy it keeps unknown
    visitors off the operator's API key. Each user still has their own account
    password on top of it.

See recurring-charge-auditor-SPEC.md sections 10, 12.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_USERS_PATH = os.path.join(_ROOT, "config", "demo_users.json")

_PBKDF2_ITERATIONS = 120_000
_ALGO = "pbkdf2_sha256"


# --- password hashing -------------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None,
                  iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Return a self-describing hash string: 'pbkdf2_sha256$<iter>$<salt>$<hash>'."""
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash string."""
    if not isinstance(stored, str) or stored.count("$") != 3:
        return False
    algo, iter_s, salt_hex, hash_hex = stored.split("$")
    if algo != _ALGO:
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(got, expected)


# --- user model -------------------------------------------------------------

@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    is_demo: bool
    password_hint: str | None = None  # shown on the demo login screen only

    def public(self) -> dict:
        """The small, serialisable shape stored in the session."""
        return {"id": self.user_id, "name": self.name, "is_demo": self.is_demo}


# --- demo accounts ----------------------------------------------------------

def load_demo_users() -> list[dict]:
    """Raw demo-account records from config/demo_users.json (empty if missing)."""
    try:
        with open(DEMO_USERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def demo_users() -> list[User]:
    """The seeded demo profiles, in file order, without their password hashes."""
    out = []
    for u in load_demo_users():
        if isinstance(u, dict) and u.get("user_id") and u.get("name"):
            out.append(User(u["user_id"], u["name"], is_demo=True,
                            password_hint=u.get("password_hint")))
    return out


def _demo_record(user_id: str) -> dict | None:
    for u in load_demo_users():
        if isinstance(u, dict) and u.get("user_id") == user_id:
            return u
    return None


def authenticate_demo(user_id: str, password: str) -> User | None:
    """Return the demo User if the password matches its stored hash, else None."""
    rec = _demo_record(user_id)
    if not rec or not verify_password(password or "", rec.get("password_hash", "")):
        return None
    return User(rec["user_id"], rec["name"], is_demo=True, password_hint=rec.get("password_hint"))


# --- access-code gate -------------------------------------------------------

def access_code_required() -> bool:
    """True when a shared invite gate is configured (REAL_ACCESS_CODE set)."""
    return bool(os.getenv("REAL_ACCESS_CODE"))


def check_access_code(code: str) -> bool:
    """Constant-time check of a typed invite code. Open when none is configured."""
    expected = os.getenv("REAL_ACCESS_CODE", "")
    if not expected:
        return True
    return hmac.compare_digest(code or "", expected)
