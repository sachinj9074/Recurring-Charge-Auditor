"""The distillation funnel: routing to subscriptions / investments / set aside / ignored."""

from src import classify
from src.classify import IGNORED, INVESTMENTS, SET_ASIDE, SUBSCRIPTIONS, Settings


def mk(**over):
    # A clean monthly subscription: steady amount, same day-of-month, enough history.
    d = dict(
        id="c1", raw_descriptor="UPI-AUTOPAY-SPOTIFY-spotify@hdfcbank",
        merchant_key="spotify@hdfcbank", representative_amount=199.0,
        cadence="monthly", amount_stable=True, price_creep=False,
        dom_consistent=True, dow_consistent=True, gap_consistent=True, occurrence_count=6,
        distinct_months=6, category=None, category_hint="subscription_bill",
        is_internal_transfer=False, internal_transfer_hint=False,
        review_status="pending",
    )
    d.update(over)
    return d


S = Settings()   # min 100, min 4 months


def test_clean_subscription_is_primary():
    assert classify.classify(mk(), S).where == SUBSCRIPTIONS


def test_price_creep_still_primary():
    p = classify.classify(mk(representative_amount=649.0, price_creep=True), S)
    assert p.where == SUBSCRIPTIONS


def test_below_minimum_set_aside():
    p = classify.classify(mk(representative_amount=30.0), S)
    assert p.where == SET_ASIDE and p.group == "Small charges"


def test_irregular_noise_is_ignored():
    # No steady schedule or amount AND no known subscription identity: random one-off
    # spend, kept out of the way (not deleted).
    assert classify.classify(mk(cadence="irregular", category_hint=None), S).where == IGNORED
    p = classify.classify(mk(amount_stable=False, price_creep=False, category_hint=None), S)
    assert p.where == IGNORED and p.group == "Irregular or one-off"


def test_irregular_subscription_is_never_hidden():
    # Fail-safe: a charge the model or a hint calls a subscription is never ignored,
    # even with unsteady timing. It stays visible in Set aside, flagged, not collapsed.
    p = classify.classify(mk(cadence="irregular", category_hint="subscription_bill"), S)
    assert p.where == SET_ASIDE and p.group == "Irregular timing"
    p = classify.classify(mk(dom_consistent=False, category="subscription_bill",
                             category_source="llm"), S)
    assert p.where == SET_ASIDE and p.group == "Irregular timing"


def test_monthly_needs_the_same_day_of_month():
    # Monthly cadence but scattered across the month is not a clean subscription pattern;
    # with no subscription identity it is ignored.
    p = classify.classify(mk(dom_consistent=False, category_hint=None), S)
    assert p.where == IGNORED


def test_weekly_needs_same_weekday_and_enough_weeks():
    steady = mk(cadence="weekly", occurrence_count=10, dow_consistent=True,
                category_hint="subscription_bill")
    assert classify.classify(steady, S).where == SUBSCRIPTIONS
    scattered = mk(cadence="weekly", occurrence_count=10, dow_consistent=False, category_hint=None)
    assert classify.classify(scattered, S).where == IGNORED
    too_few = mk(cadence="weekly", occurrence_count=3, dow_consistent=True)
    assert classify.classify(too_few, S).where == SET_ASIDE and "Emerging" in \
        classify.classify(too_few, S).group


def test_daily_needs_a_sustained_run():
    steady = mk(cadence="daily", occurrence_count=40, category_hint="subscription_bill")
    assert classify.classify(steady, S).where == SUBSCRIPTIONS
    too_few = mk(cadence="daily", occurrence_count=5)
    assert classify.classify(too_few, S).where == SET_ASIDE


def test_mandate_keyword_surfaces_at_two_hits():
    base = dict(cadence="quarterly", occurrence_count=2, distinct_months=2,
                gap_consistent=True, category_hint=None)
    # Without the mandate signal, two quarterly hits are only emerging.
    assert classify.classify(mk(**base), S).where == SET_ASIDE
    # With it, they surface as a subscription (the amount is still steady).
    assert classify.classify(mk(**base, mandate=True), S).where == SUBSCRIPTIONS
    # But the keyword never rescues a variable amount.
    p = classify.classify(mk(**base, mandate=True, amount_stable=False, price_creep=False), S)
    assert p.where == IGNORED


def test_small_but_regular_stays_in_set_aside():
    # A tiny but genuinely regular charge (e.g. a 49-rupee news app) is set aside as
    # small, not ignored: the pattern is real, it is just below the tracking floor.
    p = classify.classify(mk(representative_amount=49.0), S)
    assert p.where == SET_ASIDE and p.group == "Small charges"


def test_small_and_random_is_ignored():
    # A tiny charge with no pattern and no subscription identity is ignored.
    p = classify.classify(mk(representative_amount=49.0, cadence="irregular", category_hint=None), S)
    assert p.where == IGNORED


def test_emerging_needs_more_history():
    p = classify.classify(mk(distinct_months=2), S)
    assert p.where == SET_ASIDE and "Emerging" in p.group


def test_investment_goes_to_investments():
    p = classify.classify(mk(category_hint="investment_commitment"), S)
    assert p.where == INVESTMENTS


def test_internal_transfer_by_self_name():
    d = mk(raw_descriptor="UPI-RAHUL MEHTA-rahul@oksbi-P2P", merchant_key="rahul mehta",
           category_hint=None)
    p = classify.classify(d, S, self_ids=["Rahul Mehta"])
    assert p.where == SET_ASIDE and p.group == "Internal transfers"


def test_internal_transfer_matches_concatenated_name_vpa():
    # A VPA concatenates the holder's name (no space), so the despaced form must match.
    d = mk(raw_descriptor="UPI-SACHINJULURI-sachinjuluri@ybl-P2P", merchant_key="sachinjuluri@ybl",
           category_hint=None)
    p = classify.classify(d, S, self_ids=["Sachin Juluri"])
    assert p.where == SET_ASIDE and p.group == "Internal transfers"


def test_atm_is_ignored():
    d = mk(raw_descriptor="ATM WDL-HDFC ATM ANDHERI", merchant_key="atm wdl hdfc",
           category_hint=None)
    p = classify.classify(d, S)
    assert p.where == IGNORED and p.group == "Cash & ATM"


def test_personal_and_vendor_categories_set_aside():
    assert classify.classify(mk(category="personal_p2p", category_hint=None), S).group == "Personal & P2P"
    assert classify.classify(mk(category="vendor_noise", category_hint=None), S).group == "One-off & vendor"


def test_user_override_beats_filters():
    # User insists it is a subscription even though it is below the minimum.
    d = mk(representative_amount=30.0, category="subscription_bill", category_source="user")
    assert classify.classify(d, S).where == SUBSCRIPTIONS


def test_dismissed_set_aside():
    p = classify.classify(mk(review_status="dismissed"), S)
    assert p.where == SET_ASIDE and p.group == "Dismissed"


def test_classify_all_groups_and_annotates():
    charges = [
        mk(id="a"),                                            # subscription
        mk(id="b", category_hint="investment_commitment"),     # investment
        mk(id="c", representative_amount=20.0),                # small but regular -> set aside
        mk(id="d", cadence="irregular", category_hint=None),   # random noise -> ignored
    ]
    out = classify.classify_all(charges, S)
    assert len(out["subscriptions"]) == 1
    assert len(out["investments"]) == 1
    assert sum(len(v) for v in out["set_aside"].values()) == 1
    assert sum(len(v) for v in out["ignored"].values()) == 1
    assert out["subscriptions"][0]["_placement"]["where"] == SUBSCRIPTIONS
