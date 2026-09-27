"""Streamlit rendering for the two-lens review (view layer; no money math here).

All numbers shown come straight from the persisted charge dicts (produced by the
deterministic engine). This module only formats and lays them out, and wires the
confirm/dismiss/category/internal-transfer controls to charges.py.
"""

from __future__ import annotations

import re

import streamlit as st

from src import charges as charges_mod
from src import classify

CATEGORY_LABELS = {
    "subscription_bill": "Subscription / bill",
    "investment_commitment": "Investment / commitment",
    "personal_p2p": "Personal (P2P)",
    "vendor_noise": "One-off / vendor",
}
_CATEGORY_ORDER = list(CATEGORY_LABELS.keys())

_PER = {"daily": "/ day", "weekly": "/ week", "fortnightly": "/ 2 weeks", "monthly": "/ month",
        "bi-monthly": "/ 2 months", "quarterly": "/ quarter", "annual": "/ year"}

_TIER_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def inr(x) -> str:
    """Indian-grouped rupee amount, rounded to whole rupees (e.g. 12,34,567)."""
    try:
        n = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "-" if n < 0 else ""
    s = str(abs(int(round(n))))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{sign}{s}"


def cadence_phrase(cadence: str) -> str:
    return _PER.get(cadence, f"({cadence})")


def _title(d: dict) -> str:
    return d.get("brand_name") or d.get("merchant_key") or "(unknown merchant)"


_AMBER = ("#fff3cd", "#7a5b00")
_BLUE = ("#dbeafe", "#1e40af")
_RED = ("#fee2e2", "#991b1b")
_GRAY = ("#eef0f3", "#33363d")


def _badges(d: dict) -> list[tuple[str, str, str]]:
    """(text, background, foreground) chips. Explicit colors so they read on both
    light and dark themes."""
    out = []
    if d.get("price_creep"):
        segs = d.get("price_segments") or []
        text = (f"Price rose {inr(segs[0]['amount'])} to {inr(segs[-1]['amount'])}"
                if len(segs) >= 2 else "Price changed")
        out.append((text, *_AMBER))
    if d.get("cross_account_duplicate"):
        out.append(("Duplicate across accounts", *_BLUE))
    elif d.get("duplicate"):
        out.append(("Possible duplicate", *_BLUE))
    if d.get("status") == "stopped":
        out.append(("Looks stopped", *_RED))
    if d.get("missed_payment"):
        out.append(("Missed a cycle", *_RED))
    if d.get("internal_transfer_hint") and not d.get("is_internal_transfer"):
        out.append(("Might be an internal transfer", *_GRAY))
    return out


def _sorted(charges: list[dict]) -> list[dict]:
    return sorted(charges, key=lambda c: (_TIER_RANK.get(c.get("confidence"), 3),
                                          -float(c.get("representative_amount") or 0)))


# --- one charge card --------------------------------------------------------

def charge_card(store, d: dict, *, lens: str, key_prefix: str, editable: bool = True) -> None:
    cid = d["id"]
    with st.container(border=True):
        top = st.columns([4, 1])
        with top[0]:
            st.markdown(f"**{_title(d)}**  \n"
                        f"{inr(d['representative_amount'])} {cadence_phrase(d.get('cadence',''))}")
        with top[1]:
            st.caption(f"{d.get('confidence','')}")

        if d.get("explanation"):
            st.caption(d["explanation"])

        # meta line
        meta = f"{d.get('occurrence_count', 0)} charges · {d.get('first_seen','')} to {d.get('last_seen','')}"
        if lens == "B":
            deployed = inr(d.get("total_amount", 0))
            ran = "ran on time" if d.get("status") == "active" and not d.get("missed_payment") else \
                  ("stopped" if d.get("status") == "stopped" else "check timing")
            meta += f" · {deployed} deployed in window · {ran}"
        st.caption(meta)

        badges = _badges(d)
        if badges:
            chips = "".join(
                f"<span style='background:{bg};color:{fg};border-radius:6px;padding:2px 8px;"
                f"margin-right:6px;font-size:0.8em;white-space:nowrap'>{text}</span>"
                for text, bg, fg in badges)
            st.markdown(chips, unsafe_allow_html=True)

        if not editable:
            return

        # controls
        c1, c2, c3 = st.columns([1, 1, 2])
        status = d.get("review_status", charges_mod.PENDING)
        with c1:
            st.button("Keep" if status != charges_mod.CONFIRMED else "Kept ✓",
                      key=f"{key_prefix}keep_{cid}", use_container_width=True,
                      on_click=charges_mod.set_status, args=(store, cid, charges_mod.CONFIRMED))
        with c2:
            st.button("Dismiss", key=f"{key_prefix}dismiss_{cid}", use_container_width=True,
                      on_click=charges_mod.set_status, args=(store, cid, charges_mod.DISMISSED))
        with c3:
            current = charges_mod.effective_category(d)
            options = _CATEGORY_ORDER[:]
            index = options.index(current) if current in options else 0
            st.selectbox("Category", options, index=index,
                         format_func=lambda k: CATEGORY_LABELS[k],
                         key=f"{key_prefix}cat_{cid}", label_visibility="collapsed",
                         on_change=_on_category, args=(store, cid, f"{key_prefix}cat_{cid}"))

        st.checkbox("This is a transfer between my own accounts (exclude it)",
                    value=bool(d.get("is_internal_transfer")),
                    key=f"{key_prefix}it_{cid}",
                    on_change=_on_internal, args=(store, cid, f"{key_prefix}it_{cid}"))

        _note_input(store, d, key_prefix=key_prefix)


def _note_input(store, d: dict, *, key_prefix: str) -> None:
    """A compact free-text note so a cryptic VPA becomes recognisable (e.g. tag
    'spotify.bdsi@hdfcbank' as 'Wife's Spotify'). Persisted per user, not sent to the LLM."""
    cid = d["id"]
    st.text_input("Note", value=d.get("note", ""), key=f"{key_prefix}note_{cid}",
                  placeholder="Add a note: what is this for? (e.g. 'Wife's Spotify')",
                  label_visibility="collapsed",
                  on_change=_on_note, args=(store, cid, f"{key_prefix}note_{cid}"))


def _on_note(store, cid, widget_key):
    charges_mod.set_note(store, cid, st.session_state[widget_key])


def _on_category(store, cid, widget_key):
    charges_mod.set_category(store, cid, st.session_state[widget_key])


def _on_internal(store, cid, widget_key):
    charges_mod.set_internal_transfer(store, cid, st.session_state[widget_key])


# --- funnel view ------------------------------------------------------------

def _approx_monthly(charges: list[dict]) -> float:
    return sum(float(c.get("representative_amount") or 0)
               for c in charges if c.get("cadence") == "monthly")


def settings_control() -> classify.Settings:
    """The filter panel, shown upfront with the input fields. Both thresholds are
    user-adjustable (defaults confirmed with Sachin: a 100-rupee floor and 4 months
    of history), and the full recurrence rule is stated so the user knows what is
    set aside and why."""
    st.markdown("**Detection settings**")
    c1, c2 = st.columns(2)
    with c1:
        min_amount = st.number_input(
            "Minimum charge value to track (₹)", min_value=0, max_value=100000,
            value=int(st.session_state.get("min_amount", 100)), step=50, key="min_amount",
            help="Charges below this are set aside. A smaller value surfaces more, "
                 "including small stuff such as tea, autos, and news apps.")
    with c2:
        min_months = st.number_input(
            "Months of history for a monthly subscription", min_value=1, max_value=24,
            value=int(st.session_state.get("min_months", 4)), step=1, key="min_months",
            help="A monthly charge seen in fewer distinct months than this is held back "
                 "as 'emerging' until it has enough history.")
    st.caption("A charge counts as a subscription only with a steady amount and a steady schedule: "
               "monthly on about the same date for the months above, weekly on the same weekday for "
               "8+ weeks, or daily for 28+ days. Charges with a changing amount or random timing "
               "(daily food, cabs, cash, one-off payments) are set aside as noise, not tracked. "
               "Upload 12 to 24 months to catch yearly subscriptions.")
    return classify.Settings(min_amount=float(min_amount), min_months=int(min_months))


def _render_group(store, charges: list[dict], *, lens: str, key_prefix: str) -> None:
    ranked = _sorted(charges)
    high_med = [c for c in ranked if c.get("confidence") in ("HIGH", "MEDIUM")]
    low = [c for c in ranked if c.get("confidence") == "LOW"]
    for d in high_med:
        charge_card(store, d, lens=lens, key_prefix=key_prefix)
    if low:
        with st.expander(f"Lower-confidence matches ({len(low)})"):
            for d in low:
                charge_card(store, d, lens=lens, key_prefix=f"{key_prefix}low_")


def funnel_view(store, all_charges: list[dict], settings: classify.Settings,
                self_ids=None) -> None:
    placed = classify.classify_all(all_charges, settings, self_ids)
    subs, inv, aside = placed["subscriptions"], placed["investments"], placed["set_aside"]
    ignored = placed.get("ignored", {})
    aside_n = sum(len(v) for v in aside.values())
    ignored_n = sum(len(v) for v in ignored.values())

    # Plain count metrics, no delta: the monthly figure is a total, not an increase,
    # so it goes in a caption rather than st.metric's green up-arrow.
    m = st.columns(3)
    with m[0]:
        st.metric("Subscriptions & bills", len(subs))
        st.caption(f"about {inr(_approx_monthly(subs))} / month")
    with m[1]:
        st.metric("Investments", len(inv))
        st.caption(f"about {inr(_approx_monthly(inv))} / month")
    with m[2]:
        st.metric("Set aside", aside_n)
        st.caption(f"+ {ignored_n} ignored as one-off" if ignored_n else " ")

    tab_s, tab_i, tab_x = st.tabs([
        f"Subscriptions & bills ({len(subs)})",
        f"Investments ({len(inv)})",
        f"Set aside ({aside_n})",
    ])
    with tab_s:
        st.caption("The charges we are confident are genuine, regular subscriptions or bills. "
                   "Keep what you use; dismiss what you do not.")
        if subs:
            _render_group(store, subs, lens="A", key_prefix="s_")
        else:
            st.info("No confident subscriptions found. Check Set aside, or lower the minimum value.")
    with tab_i:
        st.caption("Contributions only: this never shows value, returns, or NAV. Investments are "
                   "never treated as leaks.")
        if inv:
            _render_group(store, inv, lens="B", key_prefix="i_")
        else:
            st.info("No investments detected.")
    with tab_x:
        st.caption("Recurring charges we demoted so they do not crowd your subscriptions. Each shows "
                   "why. Move any into Subscriptions or Investments if we got it wrong.")
        if not aside_n:
            st.info("Nothing set aside.")
        for group, items in sorted(aside.items(), key=lambda kv: -len(kv[1])):
            with st.expander(f"{group} ({len(items)})"):
                for d in _sorted(items):
                    _set_aside_card(store, d, key_prefix="x_")
        if ignored_n:
            with st.expander(f"Everything else we ignored ({ignored_n})"):
                st.caption("Random, one-off, or irregular spend with no steady amount or schedule "
                           "(daily food, cabs, cash, one-time payments). Kept out of the way, never "
                           "deleted. Move anything back if it is actually a subscription.")
                for group, items in sorted(ignored.items(), key=lambda kv: -len(kv[1])):
                    st.markdown(f"**{group}** · {len(items)}")
                    for d in _sorted(items):
                        _set_aside_card(store, d, key_prefix="ig_")


def _set_aside_card(store, d: dict, *, key_prefix: str = "x_") -> None:
    cid = d["id"]
    with st.container(border=True):
        reason = (d.get("_placement") or {}).get("reason", "")
        st.markdown(f"**{_title(d)}** · {inr(d['representative_amount'])} "
                    f"{cadence_phrase(d.get('cadence',''))}")
        if reason:
            st.caption(reason)
        c1, c2, c3 = st.columns(3)
        c1.button("Move to Subscriptions", key=f"{key_prefix}promo_s_{cid}", use_container_width=True,
                  on_click=charges_mod.recategorize, args=(store, cid, "subscription_bill"))
        c2.button("Move to Investments", key=f"{key_prefix}promo_i_{cid}", use_container_width=True,
                  on_click=charges_mod.recategorize, args=(store, cid, "investment_commitment"))
        with c3:
            occ = d.get("occurrences") or []
            with st.expander(f"Trail ({len(occ)})"):
                for o in occ[:12]:
                    st.caption(f"{o.get('date')} · {inr(o.get('amount'))}")
        _note_input(store, d, key_prefix=key_prefix)
