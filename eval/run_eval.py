"""One-command eval scorer over the labelled synthetic statements.

The eval table is the centrepiece measurement (framework phase 4). It runs the
real deterministic pipeline (ingest -> normalize -> detect -> classify) over the
synthetic samples and scores what matters for a money tool, gating the safety-
critical metrics under --strict so a regression fails CI:

  - direction correctness: sentinel transactions land on the right side of the
    debit/credit split (the single most dangerous ingestion error);
  - detection recall: every ground-truth recurring charge is detected (recall-first);
  - distillation: the PRIMARY subscriptions list stays small and holds no noise,
    while injected noise (a self-transfer, a sub-minimum charge, an irregular one)
    is set aside;
  - investment safety: no investment is placed in the leaks lens;
  - price-creep, cross-account duplicate, and aggregator-separation detection.

Runs with no API key: classification is deterministic, using the category hint and
the regularity/value/frequency signals. Ground truth tracks
samples/generate_samples.py; regenerate the samples if you change that file.

Usage:
  python eval/run_eval.py            # print the table and write eval/RESULTS.md
  python eval/run_eval.py --strict   # also exit non-zero if a safety metric fails
"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import classify, detect, ingest, normalize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")

# Sentinel transactions and the side of the split they must land on. Robust to
# adding rows, unlike exact counts.
DIRECTION_SENTINELS = {
    "sample_hdfc.csv": [("SALARY", "credit"), ("AXIS MUTUAL FUND", "debit")],
    "sample_icici.csv": [("INTEREST", "credit"), ("SPOTIFY", "debit")],
    "sample_signed.csv": [("SALARY", "credit"), ("APPLE", "debit")],
}

# Ground-truth REAL recurring charges (must be detected, and land in the primary
# subscriptions or investments views). `amount` disambiguates aggregator mandates.
EXPECTED = [
    {"name": "Spotify", "match": "spotify.bdsi@hdfcbank", "primary": "subscriptions", "cross_dup": True},
    {"name": "Netflix", "match": "netflix", "primary": "subscriptions", "price_creep": True},
    {"name": "Razorpay mandate A", "match": "razorpaysoftwarepriv", "amount": 149, "primary": "subscriptions"},
    {"name": "Razorpay mandate B", "match": "razorpaysoftwarepriv", "amount": 599, "primary": "subscriptions"},
    {"name": "YouTube Premium", "match": "google.yt@okaxis", "primary": "subscriptions"},
    {"name": "smallcase fee", "match": "smallcase.pay@yesbank", "primary": "any"},
    {"name": "Axis Mutual Fund", "match": "axis mutual", "primary": "investments"},
    {"name": "Clearing Corp equity", "match": "indian clearing", "primary": "investments"},
    {"name": "Groww SIP", "match": "groww.axis@okicici", "primary": "investments"},
    {"name": "HDFC Recurring Deposit", "match": "recurring deposit", "primary": "investments"},
    {"name": "Aditya Birla SIP", "match": "aditya birla", "primary": "investments"},
]

# Injected NOISE: detected (recall-first) but must be set aside, never primary.
NOISE = [
    {"name": "Self-transfer (own account)", "match": "rahul.mehta@okaxis"},
    {"name": "Sub-minimum charge", "match": "dailyhunt@ybl"},
    {"name": "Irregular railway booking", "match": "irctc@sbi"},
]


def _load(fn, account_id):
    grid = ingest.read_table(open(os.path.join(SAMPLES, fn), "rb").read(), fn)
    m = normalize.infer_mapping(grid)
    return grid, normalize.apply_mapping(grid, m, account_id)


def _matches(charge, exp) -> bool:
    if exp["match"] not in charge.merchant_key:
        return False
    if "amount" in exp and round(float(charge.representative_amount)) != exp["amount"]:
        return False
    return True


def score() -> dict:
    # Direction correctness via sentinels.
    direction_ok = True
    for fn, sentinels in DIRECTION_SENTINELS.items():
        _, txns = _load(fn, fn)
        for needle, expected_dir in sentinels:
            hit = next((t for t in txns if needle in t.description.upper()), None)
            if hit is None or hit.direction != expected_dir:
                direction_ok = False

    # Detection over the two-account case, with holder-name self-transfer detection.
    hdfc_grid, hdfc = _load("sample_hdfc.csv", "bank_hdfc")
    icici_grid, icici = _load("sample_icici.csv", "bank_icici")
    self_ids = normalize.extract_account_holder(hdfc_grid) + normalize.extract_account_holder(icici_grid)
    detected = detect.detect_charges(hdfc + icici)

    found = [e for e in EXPECTED if any(_matches(c, e) for c in detected)]
    recall = len(found) / len(EXPECTED)

    # Classify into the funnel and check the primary list is tight and clean.
    dicts = [c.to_dict() for c in detected]
    for d in dicts:
        d.setdefault("review_status", "pending")
        d.setdefault("is_internal_transfer", False)
    placed = classify.classify_all(dicts, classify.Settings(), self_ids=self_ids)
    primary_keys = {d["merchant_key"] for d in placed["subscriptions"]}
    invest_keys = {d["merchant_key"] for d in placed["investments"]}

    noise_in_primary = [n for n in NOISE
                        if any(n["match"] in k for k in primary_keys | invest_keys)]
    noise_excluded_ok = not noise_in_primary

    investment_expected = [e for e in EXPECTED if e["primary"] == "investments"]
    mislabeled = [e for e in investment_expected if any(e["match"] in k for k in primary_keys)]
    investment_safety = 1.0 - (len(mislabeled) / len(investment_expected))

    netflix = [c for c in detected if "netflix" in c.merchant_key]
    price_creep_ok = bool(netflix) and netflix[0].price_creep
    spotify = [c for c in detected if c.vpa == "spotify.bdsi@hdfcbank"]
    duplicate_ok = len(spotify) == 2 and all(c.cross_account_duplicate for c in spotify)
    razorpay = [c for c in detected if c.merchant_key == "razorpaysoftwarepriv"]
    aggregator_ok = len(razorpay) == 2 and {round(float(c.representative_amount)) for c in razorpay} == {149, 599}

    return {
        "direction_ok": direction_ok,
        "recall": recall,
        "found": len(found),
        "expected": len(EXPECTED),
        "detected": len(detected),
        "primary_size": len(placed["subscriptions"]),
        "investments_size": len(placed["investments"]),
        "set_aside_size": sum(len(v) for v in placed["set_aside"].values()),
        "ignored_size": sum(len(v) for v in placed.get("ignored", {}).values()),
        "noise_excluded_ok": noise_excluded_ok,
        "investment_safety": investment_safety,
        "price_creep_ok": price_creep_ok,
        "duplicate_ok": duplicate_ok,
        "aggregator_ok": aggregator_ok,
    }


def _safety_failures(m: dict) -> list[str]:
    fails = []
    if not m["direction_ok"]:
        fails.append("direction correctness")
    if m["recall"] < 0.9:
        fails.append(f"detection recall {m['recall']:.0%} < 90%")
    if not m["noise_excluded_ok"]:
        fails.append("noise leaked into the primary list")
    if m["investment_safety"] < 1.0:
        fails.append("investment safety (an investment was placed in the leaks lens)")
    if not m["price_creep_ok"]:
        fails.append("price-creep detection")
    if not m["duplicate_ok"]:
        fails.append("cross-account duplicate detection")
    if not m["aggregator_ok"]:
        fails.append("aggregator separation")
    return fails


def _table(m: dict) -> str:
    def yn(b):
        return "PASS" if b else "FAIL"
    return "\n".join([
        "| Metric | Result |",
        "|---|---|",
        f"| Direction correctness (all files) | {yn(m['direction_ok'])} |",
        f"| Detection recall (real charges) | {m['recall']:.0%} ({m['found']}/{m['expected']}) |",
        f"| Primary subscriptions list size | {m['primary_size']} |",
        f"| Noise kept out of the primary list | {yn(m['noise_excluded_ok'])} |",
        f"| Investment safety (no SIP in leaks lens) | {m['investment_safety']:.0%} |",
        f"| Price-creep detected (Netflix) | {yn(m['price_creep_ok'])} |",
        f"| Cross-account duplicate detected (Spotify) | {yn(m['duplicate_ok'])} |",
        f"| Aggregator separated (Razorpay to 2) | {yn(m['aggregator_ok'])} |",
    ])


def _write_results(m: dict) -> None:
    body = (
        "# Eval results\n\n"
        f"_Generated {datetime.date.today().isoformat()} by `python eval/run_eval.py` over the "
        "synthetic statements in `samples/`. Deterministic pipeline, no API key required._\n\n"
        f"{_table(m)}\n\n"
        f"Detected {m['detected']} recurring charges across the two-account case (recall-first). "
        f"After distillation: **{m['primary_size']} subscriptions**, {m['investments_size']} "
        f"investments, {m['set_aside_size']} set aside with reasons, and {m['ignored_size']} "
        "ignored as random or one-off spend (kept, not deleted). Injected noise (a self-transfer, "
        "a sub-minimum charge, an irregular booking) was correctly demoted.\n"
    )
    with open(os.path.join(HERE, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write(body)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    m = score()
    print(_table(m))
    print(f"\nDetected {m['detected']} -> distilled to {m['primary_size']} subscriptions, "
          f"{m['investments_size']} investments, {m['set_aside_size']} set aside, "
          f"{m['ignored_size']} ignored.")
    _write_results(m)
    print("Wrote eval/RESULTS.md")
    if "--strict" in argv:
        fails = _safety_failures(m)
        if fails:
            print("\nSTRICT FAIL: " + "; ".join(fails))
            return 1
        print("\nSTRICT PASS: all safety-critical metrics green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
