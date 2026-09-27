"""The distillation funnel: route each detected charge to a placement (no model).

Detection is deliberately over-inclusive (recall: never miss a real charge). This
module is the other half: it presents for precision, routing each charge to one of
four places so the user sees a short, confident shortlist instead of a wall of
every repeat:

  - subscriptions : the primary view. A genuine, regular, material subscription/bill.
  - investments   : SIPs and other commitments, their own view.
  - set_aside     : a short, meaningful demotion list, each with a plain reason
                    (small, internal transfer, personal/P2P, emerging, dismissed).
                    These still show a real recurring pattern.
  - ignored       : the collapsed pile of random, one-off, or irregular spend (daily
                    tea, cabs, cash, one-time payments) that merely happens to repeat.
                    Kept out of the way so it does not crowd the review.

Nothing is ever deleted: set_aside and ignored are both promotable, so a miss is
recoverable. It is deterministic and runs with no API key: the regularity,
amount-stability, value, and frequency signals do most of the filtering, and the LLM
category (when present) only refines the borderline personal/one-off calls. A human
correction (category_source == "user", an internal-transfer tag, or a dismissal) is
authoritative and overrides the auto-funnel.

Placement is computed at display time from the stored charge plus the current
Settings (so the user can change the minimum and re-see), never baked into storage.
See recurring-charge-auditor-SPEC.md sections 6, 8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src import charges as charges_mod

SUBSCRIPTIONS = "subscriptions"
INVESTMENTS = "investments"
SET_ASIDE = "set_aside"
IGNORED = "ignored"          # random / one-off / irregular; hidden by default, never deleted

_ATM = re.compile(r"(?i)\b(atm|cash\s*wdl|cash\s*withdrawal|nwd|cwdr|cash\s*at)\b")

# How much history a genuine recurring pattern needs, by cadence (Sachin's rule):
# monthly uses the user's min_months; the rest are fixed here. Two occurrences can
# never confirm a rhythm (there is only one gap), so non-monthly cadences need at
# least three, and the high-frequency ones need enough to be convincing.
_WEEKLY_MIN_HITS = 8         # 8+ weeks on the same weekday
_DAILY_MIN_HITS = 28        # 28+ days
_FORTNIGHTLY_MIN_HITS = 6   # ~3 months at a fortnightly gap
_LONG_MIN_HITS = 3          # bi-monthly / quarterly: three clean, evenly spaced hits
_ANNUAL_MIN_HITS = 2        # annual can only ever show two hits in a normal window


def recurrence_level(d: dict, settings: Settings) -> str:
    """Is this a genuine recurring pattern? Returns one of:

      - 'regular'   : steady amount and schedule, with enough history for its cadence.
      - 'emerging'  : the right pattern, but not enough cycles seen yet.
      - 'irregular' : no steady amount or schedule (random daily/one-off spend).

    A subscription is characterised by a consistent amount AND consistent timing for
    its cadence: same day-of-month (monthly), same weekday (weekly), or a clean gap.
    A flagged price rise still counts as consistent. Everything scattered is set aside
    as noise, because the goal is charges people forget they are paying, not the daily
    tea, cab, and shop spend that merely happens to repeat."""
    cadence = d.get("cadence")
    stable = bool(d.get("amount_stable", True)) or bool(d.get("price_creep"))
    count = int(d.get("occurrence_count") or 0)
    months = int(d.get("distinct_months") or 0)
    if not stable or cadence == "irregular" or cadence is None:
        return "irregular"
    # When the rail itself marks a standing instruction (autopay/mandate/NACH/ECS),
    # that is strong evidence of a real recurring charge, so two consistent hits are
    # enough to surface it (a 6-month statement shows a quarterly mandate only twice).
    # This lowers the count bar ONLY: a steady amount and steady timing are still
    # required, so a variable-amount payment can never ride in on the keyword.
    mandate = bool(d.get("mandate"))
    if cadence == "monthly":
        if not d.get("dom_consistent", False):
            return "irregular"
        needed = 2 if mandate else settings.min_months
        return "regular" if months >= needed else "emerging"
    # Timing must be consistent for the cadence. Monthly and weekly have a natural
    # anchor (day-of-month, weekday) that survives a skipped cycle; the rest are
    # judged on near-uniform gaps, so a merchant hit 47 then 74 days apart (which
    # averages to 'bi-monthly') is rejected as irregular rather than surfaced.
    if cadence == "weekly":
        if not d.get("dow_consistent", False):
            return "irregular"
        return "regular" if count >= _WEEKLY_MIN_HITS else "emerging"
    if not d.get("gap_consistent", True):
        return "irregular"
    if cadence == "daily":
        return "regular" if count >= _DAILY_MIN_HITS else "emerging"
    if cadence == "fortnightly":
        return "regular" if count >= _FORTNIGHTLY_MIN_HITS else "emerging"
    if cadence == "annual":
        return "regular" if count >= _ANNUAL_MIN_HITS else "emerging"
    # bi-monthly, quarterly
    needed = 2 if mandate else _LONG_MIN_HITS
    return "regular" if count >= needed else "emerging"


@dataclass
class Settings:
    """View-time thresholds the user can adjust. Defaults confirmed with Sachin:
    a 100-rupee floor and 4 distinct months of history for the primary list."""
    min_amount: float = 100.0
    min_months: int = 4


@dataclass
class Placement:
    where: str                 # SUBSCRIPTIONS | INVESTMENTS | SET_ASIDE
    reason: str = ""           # why it was set aside (empty for primary/investments)
    group: str = ""            # the set-aside sub-group label


def _contains_self_id(text: str, self_ids) -> bool:
    """Whether the descriptor names the account holder, so it is a transfer between
    their own accounts. Matches both the spaced name and its despaced form, because a
    VPA concatenates it (holder 'Sachin Juluri' appears as 'sachinjuluri@ybl')."""
    t = (text or "").lower()
    for sid in self_ids or []:
        sid = (sid or "").strip().lower()
        if len(sid) >= 3 and (sid in t or sid.replace(" ", "") in t):
            return True
    return False


def classify(d: dict, settings: Settings, self_ids=None) -> Placement:
    """Route one charge dict to a Placement."""
    desc = f"{d.get('raw_descriptor', '')} {d.get('merchant_key', '')}"
    eff = charges_mod.effective_category(d)
    rep = float(d.get("representative_amount") or 0)
    months = int(d.get("distinct_months") or 0)

    # --- authoritative human decisions win over the auto-funnel ---
    if d.get("review_status") == charges_mod.DISMISSED:
        return Placement(SET_ASIDE, "Dismissed by you", "Dismissed")
    if d.get("is_internal_transfer"):
        return Placement(SET_ASIDE, "Marked as a transfer between your own accounts",
                         "Internal transfers")
    if d.get("category_source") == "user":
        cat = d.get("category")
        if cat == "investment_commitment":
            return Placement(INVESTMENTS)
        if cat == "subscription_bill":
            return Placement(SUBSCRIPTIONS)
        if cat in ("personal_p2p", "vendor_noise"):
            return Placement(SET_ASIDE, "You set this aside", "Set aside by you")

    # --- deterministic funnel (order matters) ---
    # Transfers between the user's own accounts and cash stay visible but demoted:
    # they are excluded money, not subscriptions, and the user may want to confirm them.
    if d.get("internal_transfer_hint") or _contains_self_id(desc, self_ids):
        return Placement(SET_ASIDE, "Looks like a transfer between your own accounts",
                         "Internal transfers")
    if _ATM.search(desc):
        return Placement(IGNORED, "Cash or ATM withdrawal, not a subscription", "Cash & ATM")
    # Investments are routed by category before the regularity gate: a SIP or lump-sum
    # buy belongs in the investment view even if its timing or amount varies.
    if eff == "investment_commitment":
        return Placement(INVESTMENTS)

    # The regularity gate: no steady amount or schedule means it is not a clean
    # subscription pattern. Fail-safe, so a real subscription is never hidden: only
    # noise with no known subscription identity (uncategorised, vendor, or personal)
    # is collapsed into the ignored pile. Anything the model or a hint calls a
    # subscription stays visible in Set aside, flagged, for the user to judge.
    level = recurrence_level(d, settings)
    if level == "irregular":
        if eff == "subscription_bill":
            return Placement(SET_ASIDE, "Recurs, but the amount or timing is not steady",
                             "Irregular timing")
        return Placement(IGNORED, "No steady amount or schedule (looks like one-off or daily spend)",
                         "Irregular or one-off")

    # From here the charge has a genuine pattern; demote by value/category, else keep it.
    if rep < settings.min_amount:
        return Placement(SET_ASIDE, f"Below your ₹{int(settings.min_amount)} minimum",
                         "Small charges")
    if eff == "personal_p2p":
        return Placement(SET_ASIDE, "Looks like a personal or P2P payment", "Personal & P2P")
    if eff == "vendor_noise":
        return Placement(SET_ASIDE, "Looks like a shop or vendor payment", "One-off & vendor")
    if level == "emerging":
        hist = f"Only {months} month(s) of history so far" if months else "Not enough history yet"
        return Placement(SET_ASIDE, hist, "Emerging (needs more history)")
    return Placement(SUBSCRIPTIONS)


def classify_all(charges: list[dict], settings: Settings, self_ids=None) -> dict[str, list[dict]]:
    """Group all charges by placement. Returns
    {'subscriptions': [...], 'investments': [...],
     'set_aside': {group: [...]}, 'ignored': {group: [...]}}
    where set_aside and ignored are further grouped by reason, and each charge
    carries its Placement under '_placement' for the card to show the reason.

    'set_aside' is the short, meaningful demotion list (small, internal transfer,
    personal, emerging). 'ignored' is the collapsed pile of random/one-off/irregular
    spend kept out of the way: nothing is deleted, and the user can promote either."""
    subs, inv = [], []
    aside: dict[str, list[dict]] = {}
    ignored: dict[str, list[dict]] = {}
    for d in charges:
        p = classify(d, settings, self_ids)
        d = {**d, "_placement": {"where": p.where, "reason": p.reason, "group": p.group}}
        if p.where == SUBSCRIPTIONS:
            subs.append(d)
        elif p.where == INVESTMENTS:
            inv.append(d)
        elif p.where == IGNORED:
            ignored.setdefault(p.group or "Everything else", []).append(d)
        else:
            aside.setdefault(p.group or "Set aside", []).append(d)
    return {"subscriptions": subs, "investments": inv, "set_aside": aside, "ignored": ignored}
