"""Streamlit rendering for the two-lens review (view layer; no money math here).

All numbers shown come straight from the persisted charge dicts (produced by the
deterministic engine). This module only formats and lays them out, and wires the
confirm/dismiss/category/internal-transfer controls to charges.py.
"""

from __future__ import annotations

import re

import streamlit as st

from src import charges as charges_mod

CATEGORY_LABELS = {
    "subscription_bill": "Subscription / bill",
    "investment_commitment": "Investment / commitment",
    "personal_p2p": "Personal (P2P)",
    "vendor_noise": "One-off / vendor",
}
_CATEGORY_ORDER = list(CATEGORY_LABELS.keys())

_PER = {"weekly": "/ week", "fortnightly": "/ 2 weeks", "monthly": "/ month",
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


def _on_category(store, cid, widget_key):
    charges_mod.set_category(store, cid, st.session_state[widget_key])


def _on_internal(store, cid, widget_key):
    charges_mod.set_internal_transfer(store, cid, st.session_state[widget_key])


# --- lenses -----------------------------------------------------------------

def _approx_monthly(charges: list[dict]) -> float:
    return sum(float(c.get("representative_amount") or 0)
               for c in charges if c.get("cadence") == "monthly")


def summary(lens_a: list[dict], lens_b: list[dict]) -> None:
    m = st.columns(3)
    m[0].metric("Subscriptions & bills", len(lens_a), f"~{inr(_approx_monthly(lens_a))}/mo")
    m[1].metric("Investments & commitments", len(lens_b), f"~{inr(_approx_monthly(lens_b))}/mo")
    m[2].metric("Total recurring found", len(lens_a) + len(lens_b))
    st.caption("Monthly figures are an approximate sum of monthly-cadence charges, for orientation only.")


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


def two_lens(store, all_charges: list[dict]) -> None:
    lens_a, lens_b = charges_mod.split_lenses(all_charges)
    summary(lens_a, lens_b)

    tab_a, tab_b, tab_x = st.tabs([
        f"Subscriptions & bills ({len(lens_a)})",
        f"Investments & commitments ({len(lens_b)})",
        "Set aside",
    ])
    with tab_a:
        st.caption("Reviewed for leaks. Keep what you use, dismiss what you do not.")
        if lens_a:
            _render_group(store, lens_a, lens="A", key_prefix="a_")
        else:
            st.info("No subscriptions or bills detected yet.")
    with tab_b:
        st.caption("Tracked for completeness and consistency. Contributions only: this never "
                   "shows value, returns, or NAV. Investments are never treated as leaks.")
        if lens_b:
            _render_group(store, lens_b, lens="B", key_prefix="b_")
        else:
            st.info("No investments or commitments detected yet.")
    with tab_x:
        _set_aside(store, all_charges)


def _set_aside(store, all_charges: list[dict]) -> None:
    dismissed = charges_mod.dismissed(all_charges)
    transfers = charges_mod.internal_transfers(all_charges)
    if not dismissed and not transfers:
        st.caption("Nothing set aside. Dismissed charges and internal transfers show up here.")
        return
    if dismissed:
        st.markdown("**Dismissed**")
        for d in dismissed:
            cols = st.columns([4, 1])
            cols[0].caption(f"{_title(d)} · {inr(d['representative_amount'])} {cadence_phrase(d.get('cadence',''))}")
            cols[1].button("Restore", key=f"restore_{d['id']}",
                           on_click=charges_mod.set_status, args=(store, d["id"], charges_mod.PENDING))
    if transfers:
        st.markdown("**Tagged as internal transfers**")
        for d in transfers:
            cols = st.columns([4, 1])
            cols[0].caption(f"{_title(d)} · {inr(d['representative_amount'])}")
            cols[1].button("Unset", key=f"unset_{d['id']}",
                           on_click=charges_mod.set_internal_transfer, args=(store, d["id"], False))
