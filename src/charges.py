"""Charge persistence and the review/confirmation loop (deterministic, no model).

Sits between the detection engine and the UI. It persists the derived charge
list (and only that, plus the user's confirmations) into the per-user encrypted
Store, and provides the small mutations the review UI needs: keep or dismiss a
charge, correct its category, tag an internal transfer. It also splits the
charges into the two lenses.

The raw statement and normalized transactions are never persisted here; only the
derived charge dicts are. See recurring-charge-auditor-SPEC.md sections 8, 10, 12.
"""

from __future__ import annotations

from src import enrich

CATEGORIES = enrich.CATEGORIES
_CHARGES = "charges"

# review_status values
PENDING = "pending"
CONFIRMED = "confirmed"
DISMISSED = "dismissed"


def persist_detection(store, charges) -> None:
    """Save a freshly detected + enriched list of Charge objects, adding the
    review fields the confirmation loop uses."""
    for c in charges:
        d = c.to_dict()
        d.setdefault("review_status", PENDING)
        d.setdefault("is_internal_transfer", False)
        store.save(_CHARGES, d["id"], d)


def list_charges(store) -> list[dict]:
    return store.list(_CHARGES)


def get(store, charge_id: str) -> dict | None:
    try:
        return store.load(_CHARGES, charge_id)
    except Exception:
        return None


def _update(store, charge_id: str, **fields) -> dict | None:
    d = get(store, charge_id)
    if d is None:
        return None
    d.update(fields)
    store.save(_CHARGES, charge_id, d)
    return d


def set_status(store, charge_id: str, status: str) -> dict | None:
    return _update(store, charge_id, review_status=status)


def set_category(store, charge_id: str, category: str) -> dict | None:
    return _update(store, charge_id, category=category)


def set_internal_transfer(store, charge_id: str, flag: bool) -> dict | None:
    return _update(store, charge_id, is_internal_transfer=bool(flag))


def delete_charge(store, charge_id: str) -> bool:
    return store.delete(_CHARGES, charge_id)


# --- lens split -------------------------------------------------------------

def effective_category(d: dict) -> str | None:
    """The category to display and split on: the confirmed/LLM category, else the
    deterministic hint. May be None (unclassified)."""
    return d.get("category") or d.get("category_hint")


def split_lenses(charges: list[dict]) -> tuple[list[dict], list[dict]]:
    """(lens_a, lens_b). Lens B is investments/commitments; Lens A is everything
    else reviewed for leaks. Dismissed charges and tagged internal transfers are
    excluded from both."""
    active = [c for c in charges
              if c.get("review_status") != DISMISSED and not c.get("is_internal_transfer")]
    lens_b = [c for c in active if effective_category(c) == "investment_commitment"]
    lens_a = [c for c in active if effective_category(c) != "investment_commitment"]
    return lens_a, lens_b


def dismissed(charges: list[dict]) -> list[dict]:
    return [c for c in charges if c.get("review_status") == DISMISSED]


def internal_transfers(charges: list[dict]) -> list[dict]:
    return [c for c in charges if c.get("is_internal_transfer")]
