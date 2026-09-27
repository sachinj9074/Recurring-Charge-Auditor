"""Build the zero-cost demo cache and the demo user list (run to (re)build them).

The public demo must not spend the operator's API budget, so each demo profile's
statement is detected and enriched once, here, and the result is committed under
demo_cache/users/<id>/. In the app, demo mode reads these files directly and never
calls the model.

Enrichment for the committed demo uses a curated offline map for the known
synthetic merchants (so this script needs no API key). Pass --live to enrich with
the real model instead.

Usage:
  python scripts/build_demo_cache.py            # curated (offline), default
  python scripts/build_demo_cache.py --live     # enrich via the Anthropic API
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import auth, detect, enrich, ingest, normalize   # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO, "samples")
DEMO_USERS = os.path.join(REPO, "demo_cache", "users")
CONFIG = os.path.join(REPO, "config")

PROFILES = {
    "rahul": {
        "name": "Rahul Mehta",
        "accounts": [
            {"id": "bank_hdfc", "label": "HDFC Salary", "bank_name": "HDFC Bank", "file": "sample_hdfc.csv"},
            {"id": "bank_icici", "label": "ICICI Savings", "bank_name": "ICICI Bank", "file": "sample_icici.csv"},
        ],
    },
    "meera": {
        "name": "Meera Nair",
        "accounts": [
            {"id": "bank_meera", "label": "HDFC Savings", "bank_name": "HDFC Bank", "file": "sample_signed.csv"},
        ],
    },
}

# (needle in merchant_key/descriptor, amount or None, brand, category, explanation)
CURATED = [
    ("spotify",        None, "Spotify",                    "subscription_bill",
     "Music streaming subscription billed by UPI AutoPay."),
    ("google.yt",      None, "YouTube Premium",            "subscription_bill",
     "Ad-free YouTube and YouTube Music subscription."),
    ("netflix",        None, "Netflix",                    "subscription_bill",
     "Video streaming subscription (price increased during the window)."),
    ("apple.in",       None, "Apple Services",             "subscription_bill",
     "Apple subscription (iCloud/Music/One)."),
    ("razorpaysoftwarepriv", 149, "News subscription (via Razorpay)", "subscription_bill",
     "A subscription collected through the Razorpay aggregator."),
    ("razorpaysoftwarepriv", 599, "SaaS tool (via Razorpay)", "subscription_bill",
     "A software subscription collected through the Razorpay aggregator."),
    ("axis mutual",    None, "Axis Mutual Fund",           "investment_commitment",
     "Monthly SIP into an Axis mutual fund."),
    ("aditya birla",   None, "Aditya Birla Sun Life MF",   "investment_commitment",
     "Monthly SIP into an Aditya Birla Sun Life mutual fund."),
    ("indian clearing", None, "Equity (Clearing Corp)",    "investment_commitment",
     "Equity purchase settled via the clearing corporation."),
    ("groww",          None, "Groww",                      "investment_commitment",
     "SIP set up through the Groww platform."),
    ("recurring deposit", None, "HDFC Recurring Deposit",  "investment_commitment",
     "Monthly recurring deposit contribution."),
    ("smallcase",      None, "smallcase",                  "subscription_bill",
     "Flat monthly fee to the smallcase investing platform (you may prefer to tag this as an investment)."),
    # Noise merchants (labelled for a tidy demo; the funnel sets them aside deterministically).
    ("rahul.mehta@okaxis", None, "Transfer to own account", "vendor_noise",
     "A transfer to your own linked account, not a subscription."),
    ("dailyhunt",      None, "Dailyhunt",                  "subscription_bill",
     "A low-value news-app charge, below the tracking threshold."),
    ("irctc",          None, "IRCTC",                      "vendor_noise",
     "Railway ticket booking, billed irregularly rather than on a schedule."),
]


def _load_txns(file: str, account_id: str):
    grid = ingest.read_table(open(os.path.join(SAMPLES, file), "rb").read(), file)
    m = normalize.infer_mapping(grid)
    return grid, normalize.apply_mapping(grid, m, account_id)


def _curated_results(charges) -> list[dict]:
    out = []
    for c in charges:
        hay = f"{c.merchant_key} {c.raw_descriptor}".lower()
        rep = round(float(c.representative_amount))
        for needle, amount, brand, category, expl in CURATED:
            if needle in hay and (amount is None or amount == rep):
                out.append({"id": c.id, "brand_name": brand,
                            "category": category, "explanation": expl})
                break
    return out


def build(live: bool = False) -> None:
    os.makedirs(DEMO_USERS, exist_ok=True)
    demo_users = []
    for uid, prof in PROFILES.items():
        txns, accounts, self_names = [], [], set()
        for a in prof["accounts"]:
            grid, t = _load_txns(a["file"], a["id"])
            txns += t
            self_names.update(normalize.extract_account_holder(grid))
            accounts.append({"id": a["id"], "label": a["label"], "bank_name": a["bank_name"],
                             "account_type": None, "created": "2026-09-01T00:00:00"})
        charges = detect.detect_charges(txns)
        if live:
            enrich.enrich_charges(charges)
        else:
            enrich.apply_enrichment(charges, _curated_results(charges))

        outdir = os.path.join(DEMO_USERS, uid)
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "accounts.json"), "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        with open(os.path.join(outdir, "charges.json"), "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in charges], f, indent=2, ensure_ascii=False)
        with open(os.path.join(outdir, "self_ids.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "self_ids", "names": sorted(self_names)}, f, indent=2, ensure_ascii=False)

        demo_users.append({
            "user_id": uid, "name": prof["name"],
            "password_hash": auth.hash_password("demo"), "password_hint": "demo",
        })
        print(f"  {uid}: {len(accounts)} account(s), {len(charges)} charges")

    os.makedirs(CONFIG, exist_ok=True)
    with open(os.path.join(CONFIG, "demo_users.json"), "w", encoding="utf-8") as f:
        json.dump(demo_users, f, indent=2, ensure_ascii=False)
    print(f"Wrote demo cache for {len(demo_users)} profiles and config/demo_users.json")


if __name__ == "__main__":
    build(live="--live" in sys.argv)
