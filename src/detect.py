"""Deterministic recurrence detection (no model calls, ever).

This module owns every money-sensitive decision: merchant identity, grouping,
cadence, amount stability, price creep, and duplicates. The LLM never touches any
of it, because a model that invents a pattern or answers differently run to run is
fatal in a money tool (recurring-charge-auditor-SPEC.md sections 4, 6).

Pipeline, per bank account (a mandate is tied to one account and never hops):
  1. Keep debits only (money-out). Credits are salary/interest, not charges.
  2. Merchant key:
       - UPI  : the VPA (the '@' token), lower-cased. Descriptor text drifts
                ('UPI-...' vs 'UPI-AUTOPAY-...'); the VPA does not.
       - ACH  : the descriptor, combined with amount and day-of-month, so that an
                aggregator name (RAZORPAYSOFTWAREPRIV) hiding many mandates splits
                cleanly into one series per mandate.
       - card : the normalized descriptor.
  3. Recurrence: group into series; a price change on the SAME identity and day is
     merged back into one charge and flagged as price creep (so a hike does not
     look like two weaker subscriptions), while a genuinely different mandate
     (different amount AND day, overlapping in time) stays separate.
  4. Cadence, confidence tiers, price-creep, duplicate (same-account and
     cross-account), and a weak internal-transfer hint.

Over-inclusive by design: thresholds are loose (a two-hit repeat is surfaced as
LOW) so a real charge is never silently dropped. False positives are dismissed by
the user; misses are invisible.
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from src.ids import new_id

# --- channels ---------------------------------------------------------------

UPI = "upi"
ACH = "ach"
CARD = "card"
OTHER = "other"

# A VPA is one delimited token 'local@handle'. Narrations delimit fields with
# hyphens/spaces ('UPI-SPOTIFY INDIA-spotify.bdsi@hdfcbank-HDFC000-528310-SUB'),
# so we tokenize and take the token containing '@' rather than letting a greedy
# regex swallow the merchant prefix or the trailing reference number (which would
# make the "VPA" drift every month and defeat VPA keying entirely).
_VPA_DELIM = re.compile(r"[\s\-/,;|]+")
_VPA_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._]*@[a-zA-Z][a-zA-Z0-9.]*$")
_ACH_MARK = re.compile(r"(?i)\b(ach|nach|e-?nach|ecs|mandate|si|auto\s*debit|autopay)\b")
_CARD_MARK = re.compile(r"(?i)\b(pos|ecom|card|vps|imps|neft)\b")
# The rail explicitly marking a standing instruction (a strong signal it is a real
# recurring mandate, e.g. 'UPI-AUTOPAY-...' or '...autopay.rzp@...' or 'ACH ... MANDATE').
_MANDATE_MARK = re.compile(
    r"(?i)(autopay|auto\s?-?pay|auto\s?-?debit|autodebit|e-?nach|\bnach\b|\becs\b"
    r"|e-?mandate|\bmandate\b|standing\s+instruction)")


def _is_mandate(text: str) -> bool:
    return bool(_MANDATE_MARK.search(text or ""))


def _extract_vpa(narration: str) -> str | None:
    """The UPI VPA token in a narration, lower-cased, or None. A VPA rarely
    contains a hyphen, and narrations hyphen-delimit their fields, so tokenizing
    on hyphens/spaces isolates the true, stable VPA."""
    for tok in _VPA_DELIM.split((narration or "").strip()):
        tok = tok.strip(".")
        if "@" in tok and _VPA_TOKEN.match(tok):
            return tok.lower()
    return None

# tokens dropped when normalizing a descriptor into a stable identity
_STOP = {
    "ach", "d", "dr", "cr", "nach", "enach", "ecs", "upi", "neft", "imps", "pos",
    "mandate", "si", "collect", "autopay", "auto", "debit", "payment", "pmt",
    "txn", "ref", "sub", "subscription", "wdl", "atm", "vps", "ecom", "card",
}


def merchant_identity(narration: str) -> tuple[str, str, str | None]:
    """Return (channel, identity, vpa). identity is the stable grouping key: the
    VPA for UPI, else a normalized descriptor."""
    vpa = _extract_vpa(narration)
    if vpa:
        return UPI, vpa, vpa
    ident = _descriptor_identity(narration)
    if _ACH_MARK.search(narration or ""):
        return ACH, ident, None
    if _CARD_MARK.search(narration or ""):
        return CARD, ident, None
    return OTHER, ident, None


def _descriptor_identity(narration: str) -> str:
    """Normalize a non-UPI narration to a stable merchant identity: split on
    delimiters, drop reference/mandate codes (any token containing a digit) and rail
    stop-words, and keep the alphabetic merchant name.

    Dropping a whole digit-bearing token (not just its digits) is essential. An
    aggregator such as INDIAN CLEARING CORP tags every debit with a unique
    alphanumeric reference (0000GMGO1HON, 0000FWJFIUDE, ...); splitting on non-letters
    would keep 'GMGO'/'HON' and give each mandate a different identity, so they would
    never group and every one would be dropped as a singleton."""
    tokens = re.split(r"[\s\-/,;:|]+", (narration or "").strip())
    kept = []
    for tok in tokens:
        if any(ch.isdigit() for ch in tok):
            continue                                 # reference / mandate code
        word = re.sub(r"[^a-z]", "", tok.lower())
        if len(word) > 1 and word not in _STOP:
            kept.append(word)
    return " ".join(kept) or (narration or "").strip().lower()


# --- category hints (deterministic fallback; the LLM/user can override) ------

@functools.lru_cache(maxsize=1)
def _category_hints() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "category_hints.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def category_hint(text: str) -> str | None:
    """A provisional category from keyword match, or None. Investments are checked
    first so a SIP is never mistaken for a subscription."""
    t = (text or "").lower()
    hints = _category_hints()
    for category in ("investment_commitment", "subscription_bill"):
        for kw in hints.get(category, []):
            if kw.strip() and kw.lower() in t:
                return category
    return None


# --- amount / cadence helpers ----------------------------------------------

def _amount_bucket(amount: Decimal) -> int:
    return int(Decimal(amount).quantize(Decimal("1")))


_ABS_TOL = 5.0      # rupees: amounts within this of each other are "the same"
_REL_TOL = 0.05     # or within 5% of the level


def _stable(amounts: list[Decimal]) -> bool:
    if len(amounts) <= 1:
        return True
    med = statistics.median(float(a) for a in amounts)
    tol = max(3.0, med * 0.03)
    return (max(float(a) for a in amounts) - min(float(a) for a in amounts)) <= tol


def _cluster(amounts) -> bool:
    """True when all amounts sit at one level (equal, or a slight variation)."""
    vals = [float(a) for a in amounts]
    if not vals:
        return False
    med = statistics.median(vals)
    return (max(vals) - min(vals)) <= max(_ABS_TOL, med * _REL_TOL)


def _levels_differ(a, b) -> bool:
    ma, mb = statistics.median(map(float, a)), statistics.median(map(float, b))
    return abs(ma - mb) > max(_ABS_TOL, min(ma, mb) * _REL_TOL)


def _segment(occ) -> dict:
    """A price level: its representative amount, its date range, and how many hits."""
    amts = [float(o.amount) for o in occ]
    ds = [o.date for o in occ]
    return {"amount": round(statistics.median(amts), 2),
            "from": min(ds).isoformat(), "to": max(ds).isoformat(), "count": len(occ)}


def _amount_profile(occ):
    """Judge one merchant identity's amounts over time (occ sorted by date):

      'stable'   - one consistent level: a fixed subscription amount.
      'creep'    - exactly one sustained step between two stable levels, each level
                   seen at least twice: a genuine price change, kept and flagged.
      'variable' - amounts scatter (variable spend or personal P2P, not a
                   subscription). This is the key guard: unrelated one-off amounts to
                   the same person or QR code must never look like a stable charge.

    Returns (profile, segments) for display."""
    amts = [o.amount for o in occ]
    if _cluster(amts):
        return "stable", [_segment(occ)]
    n = len(occ)
    for k in range(2, n - 1):                     # each side needs at least two hits
        left, right = occ[:k], occ[k:]
        if (_cluster([o.amount for o in left]) and _cluster([o.amount for o in right])
                and _levels_differ([o.amount for o in left], [o.amount for o in right])):
            return "creep", [_segment(left), _segment(right)]
    return "variable", [_segment(occ)]


def _median_gap_days(dates: list[datetime.date]) -> float | None:
    if len(dates) < 2:
        return None
    ds = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    return statistics.median(gaps)


_CADENCE_BANDS = [
    ("daily", 0, 3),
    ("weekly", 5, 9),
    ("fortnightly", 12, 18),
    ("monthly", 26, 35),
    ("bi-monthly", 55, 70),
    ("quarterly", 85, 100),
    ("annual", 350, 380),
]


def cadence_of(median_gap: float | None) -> str:
    if median_gap is None:
        return "irregular"
    for name, lo, hi in _CADENCE_BANDS:
        if lo <= median_gap <= hi:
            return name
    return "irregular"


def _distinct_months(dates: list[datetime.date]) -> int:
    return len({(d.year, d.month) for d in dates})


def _circular_spread(values: list[int], period: int) -> int:
    """Smallest arc on a cyclic scale of `period` that contains all values, so a
    charge on the 30th and one on the 1st read as one day apart, not 29."""
    vals = sorted(set(values))
    if len(vals) < 2:
        return 0
    gaps = [b - a for a, b in zip(vals, vals[1:])]
    gaps.append(period - vals[-1] + vals[0])   # the wrap-around gap
    return period - max(gaps)


def _dom_consistent(dates: list[datetime.date], tol: int = 7) -> bool:
    """A monthly (or longer) charge hits roughly the same day-of-month. Weekend,
    holiday, and retry drift is tolerated (tol days), and month-end wrap is handled.
    Kept generous on purpose: a real subscription that slips a few days must not be
    mistaken for random spend (the fail-safe funnel also protects known subscriptions)."""
    if len(dates) < 2:
        return True
    return _circular_spread([d.day for d in dates], 31) <= tol


def _dow_consistent(dates: list[datetime.date], min_share: float = 0.6) -> bool:
    """A weekly charge lands on the same weekday most of the time (a genuine weekly
    subscription, not scattered spending that averages out to about seven days)."""
    if len(dates) < 2:
        return True
    dows = [d.weekday() for d in dates]
    return max(dows.count(x) for x in set(dows)) / len(dows) >= min_share


def _gap_consistent(dates: list[datetime.date], max_cv: float = 0.15) -> bool:
    """The intervals between occurrences are near-uniform (a steady schedule), not
    scattered. Used for cadences with no natural day anchor (daily, fortnightly,
    and the longer ones): e.g. hits 47 and 74 days apart average to 'bi-monthly'
    but are really irregular. Needs three points to judge; fewer is not penalized."""
    if len(dates) < 3:
        return True
    ds = sorted(dates)
    gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return False
    return (statistics.pstdev(gaps) / mean) <= max_cv


# --- data model -------------------------------------------------------------

@dataclass
class Occurrence:
    date: datetime.date
    amount: Decimal

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "amount": float(self.amount)}


@dataclass
class Charge:
    id: str
    bank_account_id: str | None
    channel: str
    merchant_key: str
    raw_descriptor: str
    vpa: str | None
    occurrences: list[Occurrence]
    representative_amount: Decimal
    cadence: str
    confidence: str
    distinct_months: int
    median_gap_days: float | None
    amount_stable: bool
    dom_consistent: bool
    dow_consistent: bool
    gap_consistent: bool
    price_creep: bool
    price_segments: list[dict]
    status: str                     # 'active' | 'stopped' | 'irregular'
    missed_payment: bool
    total_amount: Decimal
    first_seen: datetime.date
    last_seen: datetime.date
    occurrence_count: int
    internal_transfer_hint: bool
    category_hint: str | None
    mandate: bool = False
    duplicate: bool = False
    cross_account_duplicate: bool = False
    duplicate_group: str | None = None
    category: str | None = None      # authoritative category (LLM enrichment, M3)
    brand_name: str | None = None    # LLM enrichment (M3)
    explanation: str | None = None   # LLM enrichment (M3)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "bank_account_id": self.bank_account_id,
            "channel": self.channel,
            "merchant_key": self.merchant_key,
            "raw_descriptor": self.raw_descriptor,
            "vpa": self.vpa,
            "occurrences": [o.to_dict() for o in self.occurrences],
            "representative_amount": float(self.representative_amount),
            "cadence": self.cadence,
            "confidence": self.confidence,
            "distinct_months": self.distinct_months,
            "median_gap_days": self.median_gap_days,
            "amount_stable": self.amount_stable,
            "dom_consistent": self.dom_consistent,
            "dow_consistent": self.dow_consistent,
            "gap_consistent": self.gap_consistent,
            "price_creep": self.price_creep,
            "price_segments": self.price_segments,
            "status": self.status,
            "missed_payment": self.missed_payment,
            "total_amount": float(self.total_amount),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "occurrence_count": self.occurrence_count,
            "internal_transfer_hint": self.internal_transfer_hint,
            "category_hint": self.category_hint,
            "mandate": self.mandate,
            "duplicate": self.duplicate,
            "cross_account_duplicate": self.cross_account_duplicate,
            "duplicate_group": self.duplicate_group,
            "category": self.category,
            "brand_name": self.brand_name,
            "explanation": self.explanation,
        }


_INTERNAL = re.compile(r"(?i)\b(self|own\s*acc|own\s*a/c|to\s+self|internal\s*transfer)\b")


# --- detection --------------------------------------------------------------

def detect_charges(transactions, *, window_end: datetime.date | None = None) -> list[Charge]:
    """Detect recurring charges from normalized transactions (any accounts).

    Runs per bank account, then a cross-charge pass flags duplicates across all of
    them. `window_end` (default: the latest transaction date) anchors the
    stopped/active status.
    """
    debits = [t for t in transactions if t.direction == "debit"]
    if not debits:
        return []
    if window_end is None:
        window_end = max(t.date for t in transactions)

    charges: list[Charge] = []
    by_account: dict[str | None, list] = {}
    for t in debits:
        by_account.setdefault(t.bank_account_id, []).append(t)
    for account_id, txns in by_account.items():
        charges.extend(_detect_one_account(txns, window_end))

    _flag_duplicates(charges)
    return charges


def _detect_one_account(txns, window_end) -> list[Charge]:
    """Group a single account's debits into recurring charges.

    UPI/card/other are grouped by merchant identity alone (one VPA or descriptor is
    one counterparty), and all the amounts to that counterparty are judged together
    via _amount_profile: a consistent amount is a subscription, one sustained step is
    a price change, and scattered amounts are variable spend, not a subscription. ACH
    is different: an aggregator descriptor (RAZORPAYSOFTWAREPRIV) hides many mandates,
    so it stays separated by amount and day, with only a genuine sustained price
    change merged back."""
    ach_subseries: dict[tuple, list] = {}
    identity_groups: dict[tuple, list] = {}
    for t in txns:
        channel, ident, vpa = merchant_identity(t.description)
        if channel == ACH:
            ach_subseries.setdefault((ident, _amount_bucket(t.amount), t.date.day), []) \
                .append((t, channel, ident, vpa))
        else:
            identity_groups.setdefault((channel, ident), []).append((t, channel, ident, vpa))

    charges: list[Charge] = []

    # ACH: keep aggregator mandates separate (amount + day), merging only a genuine
    # sustained price change (each level seen twice), never unrelated one-off amounts.
    merge_groups: dict[tuple, list] = {}
    for (ident, _bucket, day), items in ach_subseries.items():
        merge_groups.setdefault((ident, day), []).append(items)
    for series_list in merge_groups.values():
        for chain in _chain_price_creep(series_list):
            c = _build_chain_charge(chain, window_end)
            if c is not None:
                charges.append(c)

    # UPI / card / other: one charge per merchant identity, amounts judged together.
    for items in identity_groups.values():
        c = _build_identity_charge(items, window_end)
        if c is not None:
            charges.append(c)
    return charges


def _chain_price_creep(series_list: list[list]) -> list[list[list]]:
    """Chain ACH sub-series that form a genuine price change: different amounts on the
    same mandate, each level SUSTAINED (>=2 occurrences), running one after another in
    time. Single-occurrence sub-series and overlapping ones are never merged, so
    unrelated one-off amounts can't be manufactured into a fake recurring charge."""
    sustained = [items for items in series_list if len(items) >= 2]
    singletons = [items for items in series_list if len(items) < 2]

    described = []
    for items in sustained:
        dates = sorted(t.date for (t, *_rest) in items)
        described.append((dates[0], dates[-1], items))
    described.sort(key=lambda d: d[0])

    chains: list[list[list]] = []
    current: list[list] = []
    current_last: datetime.date | None = None
    for first, last, items in described:
        if current and current_last is not None and first > current_last:
            current.append(items)          # sequential -> same charge (price change)
            current_last = max(current_last, last)
        elif not current:
            current = [items]
            current_last = last
        else:
            chains.append(current)          # overlaps -> separate charge
            current = [items]
            current_last = last
    if current:
        chains.append(current)
    chains.extend([s] for s in singletons)   # each singleton alone -> dropped (<2 hits)
    return chains


def _assemble_charge(txns, occ, segments, amount_stable, price_creep,
                     channel, ident, vpa, window_end) -> Charge:
    """Build a Charge from occurrences plus a decided amount profile (the shared tail
    of both the ACH-chain and the identity-group builders)."""
    dates = [o.date for o in occ]
    amounts = [o.amount for o in occ]
    median_gap = _median_gap_days(dates)
    cadence = cadence_of(median_gap)
    distinct_months = _distinct_months(dates)
    descriptor = _representative_descriptor(txns)
    return Charge(
        id=new_id("chg"),
        bank_account_id=txns[0].bank_account_id,
        channel=channel,
        merchant_key=ident,
        raw_descriptor=descriptor,
        vpa=vpa,
        occurrences=occ,
        representative_amount=Decimal(str(segments[-1]["amount"])),
        cadence=cadence,
        confidence=_confidence(cadence, distinct_months, len(occ), amount_stable),
        distinct_months=distinct_months,
        median_gap_days=median_gap,
        amount_stable=amount_stable,
        dom_consistent=_dom_consistent(dates),
        dow_consistent=_dow_consistent(dates),
        gap_consistent=_gap_consistent(dates),
        price_creep=price_creep,
        price_segments=segments,
        status=_status(dates[-1], window_end, median_gap),
        missed_payment=_has_missed(dates, median_gap),
        total_amount=sum(amounts, Decimal(0)),
        first_seen=dates[0],
        last_seen=dates[-1],
        occurrence_count=len(occ),
        internal_transfer_hint=bool(_INTERNAL.search(descriptor)),
        category_hint=category_hint(descriptor),
        mandate=_is_mandate(f"{descriptor} {ident} {vpa or ''}"),
    )


def _build_chain_charge(chain: list[list], window_end) -> Charge | None:
    """ACH: build one Charge from a chain of one-or-more sub-series (price segments)."""
    all_items = [it for series in chain for it in series]
    txns = [it[0] for it in all_items]
    if len(txns) < 2:
        return None                          # a single hit is not recurring
    channel, ident, vpa = all_items[0][1], all_items[0][2], all_items[0][3]
    occ = sorted((Occurrence(t.date, t.amount) for t in txns), key=lambda o: o.date)
    segments = []
    for series in sorted(chain, key=lambda s: min(t.date for (t, *_r) in s)):
        seg_occ = sorted((Occurrence(t.date, t.amount) for (t, *_r) in series), key=lambda o: o.date)
        segments.append(_segment(seg_occ))
    price_creep = len(segments) > 1
    amount_stable = all(_stable([t.amount for (t, *_r) in series]) for series in chain)
    return _assemble_charge(txns, occ, segments, amount_stable, price_creep,
                            channel, ident, vpa, window_end)


def _build_identity_charge(items: list, window_end) -> Charge | None:
    """UPI/card/other: build one Charge for a merchant identity, deciding amount
    stability from all its occurrences together (stable / price-creep / variable)."""
    txns = [it[0] for it in items]
    if len(txns) < 2:
        return None                          # a single hit is not recurring
    channel, ident, vpa = items[0][1], items[0][2], items[0][3]
    occ = sorted((Occurrence(t.date, t.amount) for t in txns), key=lambda o: o.date)
    profile, segments = _amount_profile(occ)
    return _assemble_charge(txns, occ, segments, profile == "stable", profile == "creep",
                            channel, ident, vpa, window_end)


def _representative_descriptor(txns) -> str:
    """The most common raw narration in the series (stable display + LLM input)."""
    counts: dict[str, int] = {}
    for t in txns:
        counts[t.description] = counts.get(t.description, 0) + 1
    return max(counts, key=counts.get)


def _confidence(cadence, distinct_months, occ_count, amount_stable) -> str:
    if cadence == "monthly" and distinct_months >= 3 and amount_stable:
        return "HIGH"
    if cadence == "daily" and occ_count >= 28 and amount_stable:
        return "HIGH"
    if cadence == "monthly" and distinct_months == 2:
        return "MEDIUM"
    if (cadence in ("daily", "weekly", "fortnightly", "bi-monthly", "quarterly", "annual")
            and occ_count >= 3):
        return "MEDIUM"
    return "LOW"


def _status(last_seen, window_end, median_gap) -> str:
    if median_gap is None:
        return "irregular"
    if (window_end - last_seen).days > 1.6 * median_gap:
        return "stopped"
    return "active"


def _has_missed(dates, median_gap) -> bool:
    if median_gap is None or len(dates) < 3:
        return False
    ds = sorted(dates)
    return any((b - a).days > 1.6 * median_gap for a, b in zip(ds, ds[1:]))


# --- duplicates (cross-charge pass) -----------------------------------------

def _flag_duplicates(charges: list[Charge]) -> None:
    """Group charges by (channel, base identity, amount bucket). A group of 2+ is
    a duplicate; if it spans more than one account it is a cross-account duplicate.
    For ACH the day-of-month is part of the key, so two distinct aggregator mandates
    at the same amount but on different days (e.g. two INDIAN CLEARING CORP SIPs) are
    not mistaken for duplicates of each other."""
    groups: dict[tuple, list[Charge]] = {}
    for c in charges:
        base = c.vpa if c.channel == UPI else c.merchant_key
        key = (c.channel, base, _amount_bucket(c.representative_amount))
        if c.channel == ACH and c.occurrences:
            key = key + (c.occurrences[0].date.day,)
        groups.setdefault(key, []).append(c)

    for key, group in groups.items():
        if len(group) < 2:
            continue
        gid = new_id("dup")
        accounts = {c.bank_account_id for c in group}
        cross = len(accounts) > 1
        for c in group:
            c.duplicate = True
            c.duplicate_group = gid
            c.cross_account_duplicate = cross
