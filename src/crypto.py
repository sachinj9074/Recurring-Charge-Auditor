"""Per-user encryption primitives (deterministic, no model calls).

The trust property for the hosted store: a user's derived charge list is
encrypted before it ever leaves the app, so the storage host only ever holds
ciphertext. The scheme is envelope encryption:

  - A random 256-bit *data key* encrypts the records (AES-GCM). It is generated
    once, at signup, and never changes for the life of the account.
  - The data key is *wrapped* (encrypted) with a key-encryption key (KEK)
    derived from the user's password via scrypt. Only the wrapped key is stored.

So the host stores the wrapped key, never the data key, and cannot read any
record without the user's password. A password change re-wraps the same data
key, so records never need re-encrypting. The cost of the guarantee: a forgotten
password is unrecoverable, because nothing else can unwrap the data key.

Login (the password *check*) uses a separate PBKDF2 hash in auth.py with its own
salt, so the stored auth hash reveals nothing usable about the data key.

The `cryptography` import is lazy, so code paths that never encrypt do not need
it. AES-GCM here prepends a fresh 12-byte nonce to each ciphertext. See
recurring-charge-auditor-SPEC.md sections 4, 10.
"""

from __future__ import annotations

import hashlib
import os

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
_NONCE = 12


class CryptoError(RuntimeError):
    """Encryption or decryption failed (wrong key, corrupt data, missing dep)."""


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise CryptoError("encryption requires the 'cryptography' package") from e
    return AESGCM


# --- key material -----------------------------------------------------------

def new_salt() -> bytes:
    return os.urandom(16)


def new_data_key() -> bytes:
    return os.urandom(32)


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte key-encryption key from a password (scrypt)."""
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


# --- symmetric cipher (what Store composes) ---------------------------------

class Cipher:
    """AES-GCM over a 32-byte key. encrypt() prepends a fresh nonce; decrypt()
    reads it back and verifies the tag."""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise CryptoError("key must be 16, 24, or 32 bytes")
        self._key = key

    def encrypt(self, data: bytes) -> bytes:
        AESGCM = _aesgcm()
        nonce = os.urandom(_NONCE)
        return nonce + AESGCM(self._key).encrypt(nonce, data, None)

    def decrypt(self, blob: bytes) -> bytes:
        AESGCM = _aesgcm()
        from cryptography.exceptions import InvalidTag
        nonce, ct = blob[:_NONCE], blob[_NONCE:]
        try:
            return AESGCM(self._key).decrypt(nonce, ct, None)
        except InvalidTag as e:
            raise CryptoError("decryption failed: wrong key or corrupted data") from e


# --- envelope: wrap/unwrap the data key with a password-derived KEK ----------

def wrap_key(kek: bytes, data_key: bytes) -> bytes:
    return Cipher(kek).encrypt(data_key)


def unwrap_key(kek: bytes, wrapped: bytes) -> bytes:
    return Cipher(kek).decrypt(wrapped)


def create_keyset(password: str) -> dict:
    """Fresh account crypto material, as a JSON-serialisable dict of hex strings.

    Stored with the account (host-side). Holds only the salt and the *wrapped*
    data key: neither reveals the data key without the password.
    """
    salt_kdf = new_salt()
    kek = derive_key(password, salt_kdf)
    data_key = new_data_key()
    return {"salt_kdf": salt_kdf.hex(), "wrapped_key": wrap_key(kek, data_key).hex()}


def open_keyset(password: str, keyset: dict) -> bytes:
    """Return the account's data key, or raise CryptoError if the password is wrong.

    Unwrapping is itself a cryptographic password check: a wrong password fails
    the AES-GCM tag and raises, so this doubles as verification.
    """
    try:
        salt_kdf = bytes.fromhex(keyset["salt_kdf"])
        wrapped = bytes.fromhex(keyset["wrapped_key"])
    except (KeyError, ValueError) as e:
        raise CryptoError("malformed keyset") from e
    return unwrap_key(derive_key(password, salt_kdf), wrapped)


def rewrap_keyset(old_password: str, new_password: str, keyset: dict) -> dict:
    """Re-wrap the same data key under a new password. Records are untouched."""
    data_key = open_keyset(old_password, keyset)
    salt_kdf = new_salt()
    kek = derive_key(new_password, salt_kdf)
    return {"salt_kdf": salt_kdf.hex(), "wrapped_key": wrap_key(kek, data_key).hex()}
