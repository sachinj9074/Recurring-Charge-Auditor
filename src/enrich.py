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
    "You label recurring bank charges for an Indian audit tool. For each charge "
    "you are given an id, a raw bank descriptor, an optional UPI VPA, the channel, "
    "one sample amount, and the cadence. For each, return: brand_name (the real "
    "merchant/brand the descriptor refers to, e.g. 'Spotify', 'Axis Mutual Fund'), "
    "category (exactly one of: subscription_bill, investment_commitment, "
    "personal_p2p, vendor_noise), and explanation (one short plain-language "
    "sentence, no financial advice). Rules: a SIP, mutual fund, recurring deposit, "
    "NPS, or clearing-corporation debit is investment_commitment, never a "
    "subscription. A person-to-person UPI transfer is personal_p2p. Do NOT output "
    "any numbers, amounts, dates, or cadences; do NOT recompute anything. Return "
    "ONLY a JSON array of objects with keys id, brand_name, category, explanation."
)


def minimize(charge) -> dict:
    """The minimized, PII-light view of a charge that may reach the model."""
    return {
        "id": charge.id,
        "descriptor": charge.raw_descriptor,
        "vpa": charge.vpa,
        "channel": charge.channel,
        "sample_amount": float(charge.representative_amount),
        "cadence": charge.cadence,
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
    raw = complete(system=_SYSTEM, user=user, tier="fast", max_tokens=2000)
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
