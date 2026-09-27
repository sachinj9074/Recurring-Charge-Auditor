"""The deterministic detection engine: keying, recurrence, tiers, creep, dupes."""

import datetime
from decimal import Decimal

from src import detect, ingest, normalize, schema
from src.normalize import Transaction


def tx(y, m, d, desc, amt, direction="debit", acct="a"):
    return Transaction(datetime.date(y, m, d), desc, Decimal(str(amt)), direction, acct)


# --- merchant keying --------------------------------------------------------

def test_vpa_stable_across_descriptor_drift_and_refs():
    a = "UPI-SPOTIFY INDIA-spotify.bdsi@hdfcbank-HDFC0000001-528310-SUBSCRIPTION"
    b = "UPI-AUTOPAY-SPOTIFY INDIA-spotify.bdsi@hdfcbank-HDFC0000001-719201-MANDATE"
    ca, ka, va = detect.merchant_identity(a)
    cb, kb, vb = detect.merchant_identity(b)
    assert ca == cb == detect.UPI
    assert ka == kb == "spotify.bdsi@hdfcbank"     # stable despite drift + ref change


def test_ach_identity_and_channel():
    ch, ident, vpa = detect.merchant_identity("ACH D- NETFLIX ENTERTAINMENT SERVICES-MANDATE 4471")
    assert ch == detect.ACH and vpa is None and "netflix" in ident


def test_category_hint():
    assert detect.category_hint("axis mutual fund sip") == "investment_commitment"
    assert detect.category_hint("spotify india") == "subscription_bill"
    assert detect.category_hint("random merchant xyz") is None


def test_cadence_bands():
    assert detect.cadence_of(30) == "monthly"
    assert detect.cadence_of(7) == "weekly"
    assert detect.cadence_of(14) == "fortnightly"
    assert detect.cadence_of(90) == "quarterly"
    assert detect.cadence_of(365) == "annual"
    assert detect.cadence_of(None) == "irregular"
    assert detect.cadence_of(45) == "irregular"


# --- recurrence + confidence ------------------------------------------------

def _monthly(desc, amt, months, day=10, acct="a"):
    return [tx(2026, m, day, desc, amt, acct=acct) for m in months]


def test_high_confidence_monthly():
    charges = detect.detect_charges(_monthly("UPI-X-x@ybl", 199, [3, 4, 5, 6]))
    assert len(charges) == 1
    assert charges[0].confidence == "HIGH" and charges[0].cadence == "monthly"
    assert charges[0].distinct_months == 4


def test_medium_confidence_two_months():
    charges = detect.detect_charges(_monthly("UPI-X-x@ybl", 199, [3, 4]))
    assert charges[0].confidence == "MEDIUM"


def test_medium_confidence_weekly():
    dates = [datetime.date(2026, 3, 1) + datetime.timedelta(days=7 * i) for i in range(4)]
    txns = [Transaction(d, "UPI-GYM-gym@ybl", Decimal("50"), "debit", "a") for d in dates]
    charges = detect.detect_charges(txns)
    assert charges[0].cadence == "weekly" and charges[0].confidence == "MEDIUM"


def test_singleton_is_not_a_charge():
    assert detect.detect_charges([tx(2026, 3, 1, "UPI-ONE-one@ybl", 500)]) == []


def test_credits_are_not_charges():
    txns = _monthly("NEFT CR-SALARY", 120000, [3, 4, 5])
    for t in txns:
        t.direction = "credit"
    assert detect.detect_charges(txns) == []


# --- aggregator separation, price creep, duplicates -------------------------

def test_aggregator_mandates_stay_separate():
    txns = (_monthly("ACH D- RAZORPAYSOFTWAREPRIV-COLLECT", 149, [3, 4, 5, 6], day=5)
            + _monthly("ACH D- RAZORPAYSOFTWAREPRIV-COLLECT", 599, [3, 4, 5, 6], day=18))
    charges = detect.detect_charges(txns)
    amounts = sorted(float(c.representative_amount) for c in charges)
    assert amounts == [149.0, 599.0]
    assert all(not c.price_creep for c in charges)
    assert all(not c.duplicate for c in charges)     # different mandates, not dupes


def test_aggregator_reference_codes_do_not_shatter_identity():
    # Regression: an aggregator tags each debit with a unique alphanumeric reference
    # (INDIAN CLEARING CORP-0000GMGO1HON). The identity must ignore the code so the
    # mandates group, instead of every debit becoming its own dropped singleton.
    a = detect.merchant_identity("ACH D- INDIAN CLEARING CORP-0000GMGO1HON")
    b = detect.merchant_identity("ACH D- INDIAN CLEARING CORP-0000FWJFIUDE")
    assert a[0] == detect.ACH and a[1] == b[1] == "indian clearing corp"
    # A recurring same-amount, same-day mandate is detected across those refs.
    txns = [tx(2026, m, 3, f"ACH D- INDIAN CLEARING CORP-0000REF{m}XY", 500) for m in (3, 4, 5, 6)]
    charges = detect.detect_charges(txns)
    assert len(charges) == 1 and charges[0].occurrence_count == 4


def test_ach_same_amount_different_day_not_duplicate():
    # Two aggregator mandates at the same amount on different days are distinct SIPs,
    # not duplicates of each other. The digit-bearing refs drop, so both normalize to
    # the same identity ('indian clearing corp') and only the day separates them.
    txns = ([tx(2026, m, 13, "ACH D- INDIAN CLEARING CORP-0000AA13", 2000) for m in (4, 5, 6)]
            + [tx(2026, m, 5, "ACH D- INDIAN CLEARING CORP-0000BB05", 2000) for m in (4, 5, 6)])
    charges = detect.detect_charges(txns)
    assert len(charges) == 2
    assert {c.merchant_key for c in charges} == {"indian clearing corp"}
    assert all(not c.duplicate for c in charges)


def test_price_creep_merges_into_one_charge():
    txns = (_monthly("ACH D- NETFLIX-MANDATE", 499, [3, 4, 5], day=15)
            + _monthly("ACH D- NETFLIX-MANDATE", 649, [6, 7, 8], day=15))
    charges = detect.detect_charges(txns)
    assert len(charges) == 1
    c = charges[0]
    assert c.price_creep and c.occurrence_count == 6
    assert [s["amount"] for s in c.price_segments] == [499.0, 649.0]
    assert float(c.representative_amount) == 649.0        # current price
    assert c.confidence == "HIGH"                         # still clearly recurring


def test_variable_amount_upi_is_not_a_stable_charge():
    # Regression: scattered amounts to one VPA (personal P2P / QR spend) must NOT be
    # manufactured into a stable, recurring charge by chaining single payments.
    txns = [tx(2026, 3, 9, "UPI-AKSHAY-akshay@okhdfcbank", 456),
            tx(2026, 5, 4, "UPI-AKSHAY-akshay@okhdfcbank", 810),
            tx(2026, 7, 7, "UPI-AKSHAY-akshay@okhdfcbank", 470),
            tx(2026, 8, 22, "UPI-AKSHAY-akshay@okhdfcbank", 560)]
    charges = detect.detect_charges(txns)
    assert len(charges) == 1                       # one merchant identity, one charge
    c = charges[0]
    assert not c.amount_stable and not c.price_creep   # variable, not a subscription


def test_upi_sustained_price_step_is_creep():
    # A real price change on a UPI subscription (each level sustained) is one charge.
    txns = (_monthly("UPI-NEWS-news@ybl", 199, [3, 4, 5])
            + _monthly("UPI-NEWS-news@ybl", 249, [6, 7, 8]))
    charges = detect.detect_charges(txns)
    assert len(charges) == 1
    c = charges[0]
    assert c.price_creep and c.occurrence_count == 6
    assert float(c.representative_amount) == 249.0


def test_two_different_single_payments_do_not_recur():
    # Two one-off payments of different amounts to one VPA is not a price change and
    # not a subscription: it is variable spend, left for the funnel to set aside.
    txns = [tx(2026, 5, 8, "UPI-X-x@ybl", 30000), tx(2026, 7, 10, "UPI-X-x@ybl", 80000)]
    charges = detect.detect_charges(txns)
    assert len(charges) == 1 and not charges[0].amount_stable and not charges[0].price_creep


def test_mandate_flag_detected_from_descriptor():
    # The rail marking a standing instruction is captured as a flag for the funnel.
    m = detect.detect_charges(_monthly("UPI-AUTOPAY-NOVI-noviautopay.rzp@hdfcbank", 149, [3, 4]))
    assert m[0].mandate is True
    plain = detect.detect_charges(_monthly("UPI-SPOTIFY-spotify@hdfcbank", 119, [3, 4]))
    assert plain[0].mandate is False


def test_cross_account_duplicate():
    txns = (_monthly("UPI-AUTOPAY-SPOTIFY-spotify@hdfcbank", 119, [3, 4, 5], acct="hdfc")
            + _monthly("UPI-AUTOPAY-SPOTIFY-spotify@hdfcbank", 119, [3, 4, 5], acct="icici"))
    charges = detect.detect_charges(txns)
    assert len(charges) == 2
    assert all(c.duplicate and c.cross_account_duplicate for c in charges)
    assert len({c.duplicate_group for c in charges}) == 1


# --- status / missed --------------------------------------------------------

def test_stopped_status():
    txns = _monthly("UPI-X-x@ybl", 99, [3, 4, 5])
    active = detect.detect_charges(txns, window_end=datetime.date(2026, 5, 20))
    stopped = detect.detect_charges(txns, window_end=datetime.date(2026, 8, 20))
    assert active[0].status == "active"
    assert stopped[0].status == "stopped"


def test_missed_payment_gap():
    txns = _monthly("UPI-X-x@ybl", 99, [3, 4, 6, 7])   # April..May gap (May missing)
    charges = detect.detect_charges(txns)
    assert charges[0].missed_payment


def test_internal_transfer_hint():
    txns = _monthly("NEFT-SELF-OWN ACCOUNT TRANSFER", 10000, [3, 4, 5])
    assert detect.detect_charges(txns)[0].internal_transfer_hint


# --- end-to-end on the samples + schema -------------------------------------

def _load(fn, acct):
    grid = ingest.read_table(open(f"samples/{fn}", "rb").read(), fn)
    m = normalize.infer_mapping(grid)
    return normalize.apply_mapping(grid, m, acct)


def test_samples_end_to_end():
    txns = _load("sample_hdfc.csv", "bank_hdfc") + _load("sample_icici.csv", "bank_icici")
    charges = detect.detect_charges(txns)

    by_key = {}
    for c in charges:
        by_key.setdefault(c.merchant_key, []).append(c)

    # Spotify: one charge per account, both cross-account duplicates.
    spot = by_key["spotify.bdsi@hdfcbank"]
    assert len(spot) == 2 and all(c.cross_account_duplicate for c in spot)

    # Netflix price creep merged.
    netflix = [c for c in charges if "netflix" in c.merchant_key][0]
    assert netflix.price_creep and float(netflix.representative_amount) == 649.0

    # Razorpay: two separate mandates.
    rzp = [c for c in charges if c.merchant_key == "razorpaysoftwarepriv"]
    assert len(rzp) == 2

    # Investments detected and hinted, never dropped.
    invest = [c for c in charges if c.category_hint == "investment_commitment"]
    assert len(invest) >= 4

    # Every charge validates against the schema.
    for c in charges:
        schema.validate(c.to_dict(), "charge")


def test_signed_sample_detects_apple():
    charges = detect.detect_charges(_load("sample_signed.csv", "bank_signed"))
    apple = [c for c in charges if c.vpa == "apple.in@hdfcbank"]
    assert len(apple) == 1 and apple[0].occurrence_count == 3
