"""Encrypted collection store and per-user isolation via user_store()."""

from src import crypto, store
from src.storage import InMemoryBackend


def test_plaintext_store_round_trip():
    s = store.Store(backend=InMemoryBackend())
    s.save("charges", "chg_1", {"id": "chg_1", "brand": "Spotify"})
    assert s.exists("charges", "chg_1")
    assert s.load("charges", "chg_1")["brand"] == "Spotify"
    assert [c["id"] for c in s.list("charges")] == ["chg_1"]
    assert s.delete("charges", "chg_1")
    assert not s.exists("charges", "chg_1")


def test_encrypted_store_holds_ciphertext_only():
    backend = InMemoryBackend()
    cipher = crypto.Cipher(crypto.new_data_key())
    s = store.Store(backend=backend, cipher=cipher)
    s.save("charges", "chg_1", {"id": "chg_1", "brand": "Netflix"})

    raw = backend.get("charges/chg_1.json")
    assert b"Netflix" not in raw            # at rest it is ciphertext
    assert s.load("charges", "chg_1")["brand"] == "Netflix"


def test_wrong_key_cannot_decode_and_list_skips_it():
    backend = InMemoryBackend()
    writer = store.Store(backend=backend, cipher=crypto.Cipher(crypto.new_data_key()))
    writer.save("charges", "chg_1", {"id": "chg_1", "brand": "Netflix"})

    reader = store.Store(backend=backend, cipher=crypto.Cipher(crypto.new_data_key()))
    try:
        reader.load("charges", "chg_1")
        assert False, "expected a crypto failure"
    except crypto.CryptoError:
        pass
    assert reader.list("charges") == []     # undecryptable records are skipped


def test_user_store_isolation_over_shared_backend():
    base = InMemoryBackend()
    asha = store.user_store("asha", crypto.new_data_key(), base=base)
    ravi = store.user_store("ravi", crypto.new_data_key(), base=base)

    asha.save("charges", "chg_1", {"id": "chg_1", "brand": "Asha-Sub"})
    ravi.save("charges", "chg_1", {"id": "chg_1", "brand": "Ravi-Sub"})

    assert asha.load("charges", "chg_1")["brand"] == "Asha-Sub"
    assert ravi.load("charges", "chg_1")["brand"] == "Ravi-Sub"
    # Each user's listing sees only their own single record.
    assert len(asha.list("charges")) == 1
    assert len(ravi.list("charges")) == 1
    # Data physically lives under the per-user prefix, encrypted.
    assert base.exists("u/asha/charges/chg_1.json")
    assert b"Asha-Sub" not in base.get("u/asha/charges/chg_1.json")
