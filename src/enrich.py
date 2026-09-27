"""LLM enrichment: brand, category, explanation only (never numbers).

The one place the model touches a charge, and it touches only three fields. It is
shown minimized inputs (descriptor, VPA, channel, one sample amount, the cadence
label) and returns, per charge, a brand name, a category from a fixed set, and a
one-line explanation. It is structurally numbers-blind: apply_enrichment reads
only those three fields off the response, so even if the model returned an amount
or a cadence, the deterministic detection numbers are never overwritten.

Categories are fixed: subscription_bill, investment_commitment, personal_p2p,
vendor_noise. Detection's `category_hint` remains as the fallback and pre-fill
when the model is unavailable, so investments stay protected either way.

See recurring-charge-auditor-SPEC.md sections 3, 4, 12 (module 4).
"""

from __future__ import annotations

import json

from src import schema

CATEGORIES = ("subscription_bill", "investment_commitment", "personal_p2p", "vendor_noise")

_SYSTEM = (
    "You categorize recurring bank charges for an Indian personal-finance audit "
    "tool. Each charge has an id, a raw bank descriptor, an optional UPI VPA, the "
    "channel, an amount range, the cadence, and how many times and distinct months "
    "it was seen. For each, return brand_name (the real merchant/brand, e.g. "
    "'Spotify', 'Axis Mutual Fund', 'Swiggy'), category (exactly one of the four "
    "below), and explanation (one short plain sentence, no financial advice).\n"
    "Categories, decide carefully:\n"
    "- subscription_bill: a company/service billed on a schedule, e.g. streaming, "
    "SaaS, telecom, broadband, electricity, gas, insurance, a gym, a news app.\n"
    "- investment_commitment: a SIP, mutual fund, recurring deposit, NPS, PPF, "
    "stocks, or a clearing-corporation debit. Never call these a subscription.\n"
    "- personal_p2p: a transfer to an individual person (a personal name or VPA), "
    "especially when the amount varies. Rent to a landlord also belongs here.\n"
    "- vendor_noise: everyday spending that happens to repeat but is NOT a "
    "subscription, e.g. food delivery (Swiggy, Zomato), groceries, fuel, autos, "
    "cabs, railway or flight tickets, restaurants.\n"
    "Use the amount range and cadence as evidence: a stable amount on a clean "
    "monthly or annual schedule points to a subscription; a varying amount or "
    "everyday-spend merchant points to vendor_noise or personal_p2p. Do NOT output "
    "any numbers, amounts, dates, or cadences, and do NOT recompute anything. "
    "Return ONLY a JSON array of objects with keys id, brand_name, category, explanation."
)


def minimize(charge) -> dict:
    """The minimized, PII-light view of a charge that may reach the model. Amounts
    are allowed to reach the LLM; what the LLM returns is still only labels."""
    amounts = [float(o.amount) for o in getattr(charge, "occurrences", [])]
    lo = round(min(amounts), 2) if amounts else float(charge.representative_amount)
    hi = round(max(amounts), 2) if amounts else float(charge.representative_amount)
    return {
        "id": charge.id,
        "descriptor": charge.raw_descriptor,
        "vpa": charge.vpa,
        "channel": charge.channel,
        "amount_min": lo,
        "amount_max": hi,
        "cadence": charge.cadence,
        "times_seen": charge.occurrence_count,
        "distinct_months": charge.distinct_months,
        "amount_stable": charge.amount_stable,
    }


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def enrich_charges(charges, *, complete=None, batch_size: int = 25) -> int:
    """Enrich charges in place. Returns how many were enriched. On any model
    failure for a batch, those charges are left unenriched (category_hint stands),
    so enrichment degrades gracefully rather than blocking the pipeline."""
    if not charges:
        return 0
    if complete is None:
        from src.model import complete_json as complete

    enriched = 0
    for batch in _chunks(charges, batch_size):
        try:
            results = _enrich_batch(batch, complete)
        except Exception:
            continue
        enriched += apply_enrichment(batch, results)
    return enriched


def _enrich_batch(batch, complete) -> list:
    items = [minimize(c) for c in batch]
    user = ("Charges:\n" + json.dumps(items, ensure_ascii=False)
            + "\n\nReturn the JSON array of {id, brand_name, category, explanation}.")
    # Judgment tier: categorizing subscription vs vendor vs personal is the call
    # that most affects how tight the primary list is, so it is worth the stronger model.
    raw = complete(system=_SYSTEM, user=user, tier="judgment", max_tokens=3000)
    if isinstance(raw, dict) and "charges" in raw:   # tolerate a wrapped array
        raw = raw["charges"]
    if not isinstance(raw, list):
        raise ValueError("enrichment response was not a JSON array")
    return raw


def apply_enrichment(charges, results) -> int:
    """Apply ONLY brand_name/category/explanation, matched by id. Extra fields in a
    response item are ignored (never read), so numbers can never be overwritten."""
    by_id = {c.id: c for c in charges}
    applied = 0
    for item in results or []:
        if not isinstance(item, dict):
            continue
        clean = {k: item.get(k) for k in ("id", "brand_name", "category", "explanation")}
        if not schema.is_valid(clean, "enrichment"):
            continue
        c = by_id.get(clean["id"])
        if c is None:
            continue
        c.brand_name = clean["brand_name"]
        c.category = clean["category"]
        c.explanation = clean["explanation"]
        applied += 1
    return applied


def effective_category(charge) -> str | None:
    """The category to display and split lenses on: the confirmed/LLM category if
    present, else the deterministic hint. May be None (truly unclassified)."""
    return charge.category or charge.category_hint
