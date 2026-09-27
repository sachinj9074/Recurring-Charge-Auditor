"""Derived-record persistence (deterministic, no model calls).

What this project stores is deliberately small: the *derived* charge list, user
confirmations, and bank-account metadata. The raw uploaded statement and the
normalized transactions are never persisted; they live only in the session that
processes them and are discarded afterwards. So a breach exposes "this person
pays for Spotify", not their full transaction history and balances.

Store is a thin JSON-collection layer over two pluggable pieces:

  - a StorageBackend (local files, or Cloudflare R2 for the hosted deploy), and
  - an optional Cipher: when present, every record is encrypted before it
    reaches the backend, so a hosted store holds only ciphertext.

Logical keys are stable regardless of backend or encryption:
    banks/<bank_account_id>.json     one bank account's metadata
    charges/<charge_id>.json         one derived charge plus its confirmation

Writes are atomic (via the backend) and save() is an upsert.

See recurring-charge-auditor-SPEC.md sections 9, 10, 12.
"""

from __future__ import annotations

import json
import os

from src.storage import LocalBackend, PrefixedBackend, R2Backend

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_ROOT = os.path.join(_REPO, "local_records", "cloud")


class StoreError(RuntimeError):
    """A record could not be saved, loaded, or located."""


class Store:
    """Encrypted JSON collections over a byte backend. Collections are flat
    namespaces ('banks', 'charges'); each record is one key '<collection>/<id>.json'."""

    def __init__(self, *, backend, cipher=None):
        self.backend = backend
        self.cipher = cipher

    # --- codec -------------------------------------------------------------

    @staticmethod
    def _key(collection: str, rid: str) -> str:
        return f"{collection}/{rid}.json"

    def _encode(self, record: dict) -> bytes:
        data = json.dumps(record, indent=2, ensure_ascii=False).encode("utf-8")
        return self.cipher.encrypt(data) if self.cipher else data

    def _decode(self, blob: bytes) -> dict:
        raw = self.cipher.decrypt(blob) if self.cipher else blob
        return json.loads(raw.decode("utf-8"))

    # --- CRUD --------------------------------------------------------------

    def save(self, collection: str, rid: str, record: dict) -> dict:
        if not rid:
            raise StoreError("record id is required")
        self.backend.put(self._key(collection, rid), self._encode(record))
        return record

    def load(self, collection: str, rid: str) -> dict:
        try:
            raw = self.backend.get(self._key(collection, rid))
        except KeyError:
            raise StoreError(f"no {collection} record: {rid}")
        return self._decode(raw)

    def exists(self, collection: str, rid: str) -> bool:
        return self.backend.exists(self._key(collection, rid))

    def list(self, collection: str) -> list[dict]:
        """Every record in a collection. A corrupt or undecryptable file is skipped."""
        out = []
        for key in self.backend.list(f"{collection}/"):
            if not key.endswith(".json"):
                continue
            try:
                out.append(self._decode(self.backend.get(key)))
            except Exception:
                continue
        return out

    def delete(self, collection: str, rid: str) -> bool:
        key = self._key(collection, rid)
        if self.backend.exists(key):
            self.backend.delete(key)
            return True
        return False


# --- backend wiring ---------------------------------------------------------

def r2_configured() -> bool:
    """True when all four R2 secrets are present, so the hosted backend is used."""
    return all(os.getenv(k) for k in
               ("R2_BUCKET", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))


def build_base_backend():
    """The shared backend for both the user registry and per-user data: Cloudflare
    R2 when its secrets are set, else a git-ignored local folder."""
    if r2_configured():
        return R2Backend(
            os.environ["R2_BUCKET"],
            endpoint_url=os.environ["R2_ENDPOINT_URL"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        )
    return LocalBackend(LOCAL_ROOT)


def user_store(user_id: str, data_key: bytes, *, base=None) -> Store:
    """An encrypted Store confined to one user's prefix 'u/<user_id>/'. This is
    the per-user isolation boundary: it can only read or write that user's keys."""
    from src.crypto import Cipher
    base = base if base is not None else build_base_backend()
    return Store(backend=PrefixedBackend(base, f"u/{user_id}/"), cipher=Cipher(data_key))
