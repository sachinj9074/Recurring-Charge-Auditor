"""Pluggable byte storage backends (deterministic, no model calls).

Store (store.py) is a thin records layer; this module is the raw key/value
substrate underneath it, so the same Store code serves a private local vault and
a hosted instance by pointing at a different backend:

  - LocalBackend    : files under a root directory.
  - R2Backend       : Cloudflare R2 (S3-compatible), for the hosted deployment.
  - InMemoryBackend : a dict, for tests.

A backend is a flat map from a "/"-separated key (for example
"charges/chg_abc.json") to bytes. It knows nothing about records, encryption, or
users: Store layers those on top, and per-user isolation is enforced above by
never listing outside the logged-in user's key prefix (PrefixedBackend).

The boto3 import in R2Backend is lazy, and a client can be injected, so nothing
here requires boto3 unless a real R2 backend is actually constructed.

See recurring-charge-auditor-SPEC.md sections 9, 10.
"""

from __future__ import annotations

import glob
import os
import tempfile


class StorageError(RuntimeError):
    """A backend could not read or write a key."""


# --- local filesystem -------------------------------------------------------

class LocalBackend:
    """Keys map to files under `root`, using the OS separator on disk but always
    "/" in keys. Writes are atomic (temp file + rename)."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, key: str) -> str:
        return os.path.join(self.root, *key.split("/"))

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)  # atomic on the same filesystem
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def get(self, key: str) -> bytes:
        try:
            with open(self._path(key), "rb") as f:
                return f.read()
        except FileNotFoundError as e:
            raise KeyError(key) from e

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        out = []
        for p in glob.glob(os.path.join(base, "**"), recursive=True):
            if os.path.isfile(p):
                out.append(os.path.relpath(p, self.root).replace(os.sep, "/"))
        return sorted(out)

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


# --- per-user namespace -----------------------------------------------------

class PrefixedBackend:
    """Wraps a backend so every key is transparently confined to one prefix.

    This is the per-user isolation primitive for a shared bucket: a user's Store
    is bound to PrefixedBackend(base, "u/<id>/"), so it can only ever read or
    write under that user's own prefix. list() strips the prefix so the wrapped
    Store still sees plain "charges/..." / "banks/..." keys.
    """

    def __init__(self, inner, prefix: str):
        if not prefix.endswith("/"):
            prefix += "/"
        self.inner = inner
        self.prefix = prefix
        self.root = None

    def put(self, key: str, data: bytes) -> None:
        self.inner.put(self.prefix + key, data)

    def get(self, key: str) -> bytes:
        try:
            return self.inner.get(self.prefix + key)
        except KeyError:
            raise KeyError(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(self.prefix + key)

    def list(self, prefix: str) -> list[str]:
        n = len(self.prefix)
        return [k[n:] for k in self.inner.list(self.prefix + prefix)]

    def delete(self, key: str) -> None:
        self.inner.delete(self.prefix + key)


# --- in-memory (tests) ------------------------------------------------------

class InMemoryBackend:
    def __init__(self):
        self._data: dict[str, bytes] = {}
        self.root = None

    def put(self, key: str, data: bytes) -> None:
        self._data[key] = bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return self._data[key]
        except KeyError:
            raise KeyError(key)

    def exists(self, key: str) -> bool:
        return key in self._data

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


# --- Cloudflare R2 (S3-compatible) ------------------------------------------

class R2Backend:
    """Cloudflare R2 over its S3-compatible API.

    Pass an already-built boto3 S3 client as `client` (the app and tests do
    this), or the connection parameters to have one built lazily. Credentials
    come from Streamlit secrets on the deploy, never the repo.
    """

    def __init__(self, bucket: str, *, client=None, endpoint_url: str | None = None,
                 access_key_id: str | None = None, secret_access_key: str | None = None,
                 region: str = "auto"):
        self.bucket = bucket
        self.root = None
        if client is not None:
            self._client = client
        else:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover - needs the dep to hit
                raise StorageError("R2 storage requires the 'boto3' package") from e
            self._client = boto3.client(
                "s3", endpoint_url=endpoint_url, region_name=region,
                aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
            )

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as e:
            if _is_missing(e):
                raise KeyError(key) from e
            raise StorageError(f"R2 get failed for {key!r}: {e}") from e
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            if _is_missing(e):
                return False
            raise StorageError(f"R2 head failed for {key!r}: {e}") from e

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kw)
            keys.extend(obj["Key"] for obj in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


def _is_missing(err: Exception) -> bool:
    """True when an S3/R2 error means 'no such key' rather than a real failure."""
    resp = getattr(err, "response", None)
    if isinstance(resp, dict):
        code = str(resp.get("Error", {}).get("Code", ""))
        status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("NoSuchKey", "404", "NotFound") or status == 404:
            return True
    return err.__class__.__name__ in ("NoSuchKey", "404")
