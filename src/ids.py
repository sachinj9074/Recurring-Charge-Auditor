"""Short, collision-resistant record ids (deterministic aside from randomness)."""

from __future__ import annotations

import secrets


def new_id(prefix: str) -> str:
    """A URL-safe id like 'chg_9f3a1c2b7d8e'. The prefix names the entity."""
    return f"{prefix}_{secrets.token_hex(6)}"
