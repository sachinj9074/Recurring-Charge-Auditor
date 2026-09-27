"""Encryption spine: envelope keyset and the AES-GCM cipher."""

import pytest

from src import crypto


def test_cipher_round_trip():
    key = crypto.new_data_key()
    c = crypto.Cipher(key)
    blob = c.encrypt(b"hello leak")
    assert blob != b"hello leak"        # actually encrypted
    assert c.decrypt(blob) == b"hello leak"


def test_cipher_fresh_nonce_each_time():
    c = crypto.Cipher(crypto.new_data_key())
    assert c.encrypt(b"x") != c.encrypt(b"x")   # nonce is random


def test_cipher_rejects_bad_key_length():
    with pytest.raises(crypto.CryptoError):
        crypto.Cipher(b"tooshort")


def test_wrong_key_fails_tag():
    blob = crypto.Cipher(crypto.new_data_key()).encrypt(b"secret")
    with pytest.raises(crypto.CryptoError):
        crypto.Cipher(crypto.new_data_key()).decrypt(blob)


def test_keyset_round_trip():
    ks = crypto.create_keyset("correct horse battery staple")
    dk = crypto.open_keyset("correct horse battery staple", ks)
    assert len(dk) == 32
    # The stored keyset never contains the raw data key.
    assert dk.hex() not in ks["wrapped_key"]


def test_keyset_wrong_password_raises():
    ks = crypto.create_keyset("right-password")
    with pytest.raises(crypto.CryptoError):
        crypto.open_keyset("wrong-password", ks)


def test_rewrap_preserves_data_key():
    ks = crypto.create_keyset("old-password")
    dk = crypto.open_keyset("old-password", ks)
    ks2 = crypto.rewrap_keyset("old-password", "new-password", ks)
    assert crypto.open_keyset("new-password", ks2) == dk   # same data key
    with pytest.raises(crypto.CryptoError):
        crypto.open_keyset("old-password", ks2)            # old no longer works


def test_malformed_keyset_raises():
    with pytest.raises(crypto.CryptoError):
        crypto.open_keyset("pw", {"salt_kdf": "zz", "wrapped_key": "zz"})
