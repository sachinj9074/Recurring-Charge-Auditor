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
    prefs = load_merchant_prefs(store)
    for c in charges:
        d = c.to_dict()
        d.setdefault("review_status", PENDING)
        d.setdefault("is_internal_transfer", False)
        _apply_pref(d, prefs.get(merchant_ref(d)))   # re-apply the user's past decisions
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
    d = _update(store, charge_id, review_status=status)
    _remember(store, d, review_status=status)
    return d


def set_category(store, charge_id: str, category: str) -> dict | None:
    # Stamp the source so the classifier treats a human correction as authoritative
    # (it overrides the auto-funnel), unlike the LLM's category guess.
    d = _update(store, charge_id, category=category, category_source="user")
    _remember(store, d, category=category, category_source="user")
    return d


def set_internal_transfer(store, charge_id: str, flag: bool) -> dict | None:
    d = _update(store, charge_id, is_internal_transfer=bool(flag))
    _remember(store, d, is_internal_transfer=bool(flag))
    return d


def set_note(store, charge_id: str, note: str) -> dict | None:
    """The user's own free-text note for a charge (what it is really for), so a
    cryptic VPA becomes recognisable. Stored per user (encrypted); never sent to
    the LLM."""
    d = _update(store, charge_id, note=(note or "").strip())
    _remember(store, d, note=(note or "").strip())
    return d


def recategorize(store, charge_id: str, category: str) -> dict | None:
    """Promote/move a charge to a category authoritatively: stamps the user source,
    clears any dismissal and internal-transfer tag. Used by the Set-aside move
    buttons so an item lands firmly in Subscriptions or Investments."""
    d = _update(store, charge_id, category=category, category_source="user",
                review_status=CONFIRMED, is_internal_transfer=False)
    _remember(store, d, category=category, category_source="user",
              review_status=CONFIRMED, is_internal_transfer=False)
    return d


def apply_edits(store, edits: dict) -> int:
    """Persist a batch of staged edits at once (one write per changed charge) and
    remember each decision against its merchant, so the review can stage many changes
    and save them in a single action instead of a write per click. `edits` is
    {charge_id: {field: value}}. Returns how many charges changed."""
    n = 0
    for cid, fields in (edits or {}).items():
        d = get(store, cid)
        if d is None:
            continue
        upd = dict(fields)
        if "category" in upd:
            upd["category_source"] = "user"     # a human correction is authoritative
        d.update(upd)
        store.save(_CHARGES, cid, d)
        _remember(store, d, **{k: v for k, v in upd.items() if k in _PREF_FIELDS})
        n += 1
    return n


def delete_charge(store, charge_id: str) -> bool:
    return store.delete(_CHARGES, charge_id)


# --- per-merchant memory of the user's decisions -----------------------------
# Detection assigns a fresh id every run, so a decision tied to a charge id would be
# lost on re-upload. Keying it on the stable merchant identity instead makes a
# decision persist across re-uploads AND carry to future months of that merchant:
# tag a person's VPA as a transfer once, and it stays set aside forever.

_META = "meta"
_SELF_IDS = "self_ids"
_MERCHANT_PREFS = "merchant_prefs"
# Only the user's own decisions are remembered, never engine-computed numbers.
_PREF_FIELDS = ("category", "category_source", "is_internal_transfer", "review_status", "note")


def merchant_ref(d: dict) -> str:
    """The stable identity a preference is keyed on: the VPA for UPI, else the
    normalized descriptor. Lower-cased; empty when neither is present."""
    return (d.get("vpa") or d.get("merchant_key") or "").strip().lower()


def load_merchant_prefs(store) -> dict:
    """{merchant_ref: {field: value}} of the user's remembered decisions."""
    try:
        return dict(store.load(_META, _MERCHANT_PREFS).get("prefs", {}))
    except Exception:
        return {}


def _apply_pref(d: dict, entry: dict | None) -> None:
    """Overlay a remembered decision onto a freshly detected charge dict."""
    if not entry:
        return
    for k in _PREF_FIELDS:
        if entry.get(k) is not None:
            d[k] = entry[k]


def _remember(store, d: dict | None, **fields) -> None:
    """Record the user's decision for a charge's merchant, so it re-applies next time."""
    if not d:
        return
    ref = merchant_ref(d)
    if not ref:
        return
    prefs = load_merchant_prefs(store)
    entry = dict(prefs.get(ref, {}))
    entry.update({k: v for k, v in fields.items() if k in _PREF_FIELDS})
    prefs[ref] = entry
    store.save(_META, _MERCHANT_PREFS, {"id": _MERCHANT_PREFS, "prefs": prefs})


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
