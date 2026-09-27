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


def persist_detection(store, charges, *, replace_bank_account_id=None) -> None:
    """Save a freshly detected + enriched list of Charge objects, adding the review
    fields the confirmation loop uses.

    Detection assigns a fresh id per run, so persisting must be a REPLACE, not an
    append, or re-uploading the same statement doubles every charge. When
    replace_bank_account_id is given, that account's existing charges are cleared
    first, so re-detecting a statement is idempotent."""
    if replace_bank_account_id is not None:
        clear_charges(store, bank_account_id=replace_bank_account_id)
    for c in charges:
        d = c.to_dict()
        d.setdefault("review_status", PENDING)
        d.setdefault("is_internal_transfer", False)
        store.save(_CHARGES, d["id"], d)


def clear_charges(store, *, bank_account_id=None) -> int:
    """Delete stored charges and return how many were removed. With
    bank_account_id, only that account's charges (used to replace one statement's
    detection); otherwise every charge (the user's 'start over')."""
    n = 0
    for d in list_charges(store):
        if bank_account_id is None or d.get("bank_account_id") == bank_account_id:
            if delete_charge(store, d["id"]):
                n += 1
    return n


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
    # Stamp the source so the classifier treats a human correction as authoritative
    # (it overrides the auto-funnel), unlike the LLM's category guess.
    return _update(store, charge_id, category=category, category_source="user")


def set_internal_transfer(store, charge_id: str, flag: bool) -> dict | None:
    return _update(store, charge_id, is_internal_transfer=bool(flag))


def set_note(store, charge_id: str, note: str) -> dict | None:
    """The user's own free-text note for a charge (what it is really for), so a
    cryptic VPA becomes recognisable. Stored per user (encrypted); never sent to
    the LLM."""
    return _update(store, charge_id, note=(note or "").strip())


def recategorize(store, charge_id: str, category: str) -> dict | None:
    """Promote/move a charge to a category authoritatively: stamps the user source,
    clears any dismissal and internal-transfer tag. Used by the Set-aside move
    buttons so an item lands firmly in Subscriptions or Investments."""
    return _update(store, charge_id, category=category, category_source="user",
                   review_status=CONFIRMED, is_internal_transfer=False)


def delete_charge(store, charge_id: str) -> bool:
    return store.delete(_CHARGES, charge_id)


# --- per-user self identifiers (for internal-transfer detection) -------------

_META = "meta"
_SELF_IDS = "self_ids"


def load_self_ids(store) -> list[str]:
    """The user's own name tokens, used to spot transfers between their accounts.
    Stored per user (encrypted), merged across the statements they upload."""
    try:
        return list(store.load(_META, _SELF_IDS).get("names", []))
    except Exception:
        return []


def add_self_ids(store, names: list[str]) -> None:
    current = set(load_self_ids(store))
    current.update(n for n in (names or []) if n)
    store.save(_META, _SELF_IDS, {"id": _SELF_IDS, "names": sorted(current)})


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
