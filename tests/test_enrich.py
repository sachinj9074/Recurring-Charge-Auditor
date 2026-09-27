"""LLM enrichment: the numbers-blind contract and graceful degradation."""

import datetime
from decimal import Decimal

from src import detect, enrich
from src.normalize import Transaction


def _charge(desc="UPI-SPOTIFY-spotify@hdfcbank", amt=119):
    txns = [Transaction(datetime.date(2026, m, 10), desc, Decimal(str(amt)), "debit", "a")
            for m in (3, 4, 5)]
    return detect.detect_charges(txns)[0]


def test_minimize_sends_only_allowed_fields():
    m = enrich.minimize(_charge())
    assert set(m) == {"id", "descriptor", "vpa", "channel", "sample_amount", "cadence"}
    # Occurrences, totals, and dates are never handed to the model.
    assert "occurrences" not in m and "total_amount" not in m


def test_apply_enrichment_sets_three_fields():
    c = _charge()
    n = enrich.apply_enrichment([c], [{
        "id": c.id, "brand_name": "Spotify",
        "category": "subscription_bill", "explanation": "Music streaming subscription.",
    }])
    assert n == 1
    assert c.brand_name == "Spotify" and c.category == "subscription_bill"


def test_enrichment_is_numbers_blind():
    c = _charge(amt=119)
    original_amount = c.representative_amount
    # A malicious/confused response tries to smuggle numbers in.
    enrich.apply_enrichment([c], [{
        "id": c.id, "brand_name": "Spotify", "category": "subscription_bill",
        "explanation": "sub", "representative_amount": 999, "amount": 1, "cadence": "weekly",
    }])
    assert c.representative_amount == original_amount    # untouched
    assert c.cadence == "monthly"                        # untouched
    assert c.brand_name == "Spotify"                     # only labels applied


def test_invalid_category_is_rejected():
    c = _charge()
    n = enrich.apply_enrichment([c], [{
        "id": c.id, "brand_name": "X", "category": "not_a_category", "explanation": "y",
    }])
    assert n == 0 and c.category is None


def test_missing_field_is_rejected():
    c = _charge()
    n = enrich.apply_enrichment([c], [{"id": c.id, "brand_name": "X"}])
    assert n == 0 and c.brand_name is None


def test_enrich_charges_batches_and_calls_model():
    charges = [_charge(f"UPI-M{i}-m{i}@ybl", 100 + i) for i in range(3)]
    calls = {"n": 0}

    def fake_complete(**kw):
        calls["n"] += 1
        # Echo back a valid enrichment for each charge in the batch payload.
        import json, re
        ids = re.findall(r'"id": "([^"]+)"', kw["user"])
        return [{"id": i, "brand_name": "Brand", "category": "vendor_noise",
                 "explanation": "e"} for i in ids]

    n = enrich.enrich_charges(charges, complete=fake_complete, batch_size=2)
    assert n == 3 and calls["n"] == 2          # 3 charges, batch of 2 -> 2 calls


def test_enrich_degrades_on_model_error():
    charges = [_charge()]

    def boom(**kw):
        raise RuntimeError("model down")

    n = enrich.enrich_charges(charges, complete=boom)
    assert n == 0 and charges[0].category is None       # left unenriched, no crash


def test_effective_category_prefers_llm_then_hint():
    c = _charge("ACH D- AXIS MUTUAL FUND SIP", 5000)
    assert c.category is None and c.category_hint == "investment_commitment"
    assert enrich.effective_category(c) == "investment_commitment"   # falls back to hint
    c.category = "subscription_bill"
    assert enrich.effective_category(c) == "subscription_bill"       # LLM/user wins
