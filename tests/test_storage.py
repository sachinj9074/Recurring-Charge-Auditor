"""Storage backends and the per-user prefix isolation primitive."""

from src.storage import InMemoryBackend, LocalBackend, PrefixedBackend


def test_local_backend_round_trip(tmp_path):
    b = LocalBackend(str(tmp_path))
    assert not b.exists("a/b.json")
    b.put("a/b.json", b"data")
    assert b.exists("a/b.json")
    assert b.get("a/b.json") == b"data"
    assert b.list("a/") == ["a/b.json"]
    b.delete("a/b.json")
    assert not b.exists("a/b.json")


def test_local_backend_get_missing_raises_keyerror(tmp_path):
    b = LocalBackend(str(tmp_path))
    try:
        b.get("missing.json")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_inmemory_backend_round_trip():
    b = InMemoryBackend()
    b.put("x/1.json", b"1")
    b.put("x/2.json", b"2")
    b.put("y/3.json", b"3")
    assert b.get("x/1.json") == b"1"
    assert b.list("x/") == ["x/1.json", "x/2.json"]
    b.delete("x/1.json")
    assert b.list("x/") == ["x/2.json"]


def test_prefixed_backend_confines_keys():
    base = InMemoryBackend()
    pb = PrefixedBackend(base, "u/asha/")
    pb.put("charges/c1.json", b"asha-charge")
    # Physically stored under the prefixed key on the base backend.
    assert base.exists("u/asha/charges/c1.json")
    # Seen as the plain key through the wrapper, prefix stripped.
    assert pb.list("charges/") == ["charges/c1.json"]
    assert pb.get("charges/c1.json") == b"asha-charge"


def test_prefixed_backends_isolate_users():
    base = InMemoryBackend()
    asha = PrefixedBackend(base, "u/asha/")
    ravi = PrefixedBackend(base, "u/ravi/")
    asha.put("charges/c1.json", b"asha-only")
    ravi.put("charges/c1.json", b"ravi-only")

    # Same logical key, different physical namespace: no cross-contamination.
    assert asha.get("charges/c1.json") == b"asha-only"
    assert ravi.get("charges/c1.json") == b"ravi-only"
    # Neither user's listing can see the other's keys.
    assert asha.list("") == ["charges/c1.json"]
    assert ravi.list("") == ["charges/c1.json"]
    assert not asha.exists("../ravi/charges/c1.json")
