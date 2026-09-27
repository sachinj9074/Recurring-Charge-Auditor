"""Charge persistence and the review/confirmation loop."""

import datetime
from decimal import Decimal

from src import charges, detect, store
from src.normalize import Transaction
from src.storage import InMemoryBackend


def _store():
    return store.Store(backend=InMemoryBackend())


def _detected():
    txns, key = [], "UPI-AUTOPAY-SPOTIFY-spotify@hdfcbank"
    for m in (3, 4, 5):
        txns.append(Transaction(datetime.date(2026, m, 10), key, Decimal("119"), "debit", "a"))
    txns.append(Transaction(datetime.date(2026, 3, 3), "ACH D- AXIS MUTUAL FUND SIP", Decimal("5000"), "debit", "a"))
    txns.append(Transaction(datetime.date(2026, 4, 3), "ACH D- AXIS MUTUAL FUND SIP", Decimal("5000"), "debit", "a"))
    txns.append(Transaction(datetime.date(2026, 5, 3), "ACH D- AXIS MUTUAL FUND SIP", Decimal("5000"), "debit", "a"))
    return detect.detect_charges(txns)


def test_persist_adds_review_fields():
    s = _store()
    charges.persist_detection(s, _detected())
    saved = charges.list_charges(s)
    assert len(saved) == 2
    assert all(c["review_status"] == charges.PENDING for c in saved)
    assert all(c["is_internal_transfer"] is False for c in saved)


def test_redetecting_replaces_instead_of_appending():
    # The bug this guards against: re-uploading the same statement doubled every
    # charge because each run gets fresh ids. Replacing by account keeps it idempotent.
    s = _store()
    charges.persist_detection(s, _detected(), replace_bank_account_id="a")
    first = charges.list_charges(s)
    assert len(first) == 2
    # Re-detect the identical statement for the same account.
    charges.persist_detection(s, _detected(), replace_bank_account_id="a")
    again = charges.list_charges(s)
    assert len(again) == 2                       # not 4
    # A different account's charges are left untouched.
    charges.persist_detection(s, _detected())    # account 'a', append (no replace)
    charges.persist_detection(s, _detected(), replace_bank_account_id="a")
    assert len(charges.list_charges(s)) == 2


def test_clear_charges_all_and_by_account():
    s = _store()
    charges.persist_detection(s, _detected())
    assert len(charges.list_charges(s)) == 2
    assert charges.clear_charges(s, bank_account_id="zzz") == 0   # no match, nothing cleared
    assert charges.clear_charges(s, bank_account_id="a") == 2     # this account cleared
    assert charges.list_charges(s) == []


def test_status_category_and_transfer_mutations():
    s = _store()
    charges.persist_detection(s, _detected())
    cid = charges.list_charges(s)[0]["id"]

    charges.set_status(s, cid, charges.DISMISSED)
    assert charges.get(s, cid)["review_status"] == charges.DISMISSED

    charges.set_category(s, cid, "vendor_noise")
    assert charges.get(s, cid)["category"] == "vendor_noise"

    charges.set_internal_transfer(s, cid, True)
    assert charges.get(s, cid)["is_internal_transfer"] is True


def test_split_lenses_uses_effective_category():
    s = _store()
    charges.persist_detection(s, _detected())
    lens_a, lens_b = charges.split_lenses(charges.list_charges(s))
    # Axis SIP -> Lens B via the investment hint; Spotify -> Lens A.
    assert [c["category_hint"] for c in lens_b] == ["investment_commitment"]
    assert all(charges.effective_category(c) != "investment_commitment" for c in lens_a)


def test_dismissed_and_internal_excluded_from_lenses():
    s = _store()
    charges.persist_detection(s, _detected())
    all_c = charges.list_charges(s)
    charges.set_status(s, all_c[0]["id"], charges.DISMISSED)
    charges.set_internal_transfer(s, all_c[1]["id"], True)
    lens_a, lens_b = charges.split_lenses(charges.list_charges(s))
    assert lens_a == [] and lens_b == []            # both excluded now
    assert len(charges.dismissed(charges.list_charges(s))) == 1
    assert len(charges.internal_transfers(charges.list_charges(s))) == 1
