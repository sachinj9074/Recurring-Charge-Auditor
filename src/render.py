"""Streamlit rendering for the two-lens review (view layer; no money math here).

All numbers shown come straight from the persisted charge dicts (produced by the
deterministic engine). This module only formats and lays them out, and wires the
confirm/dismiss/category/internal-transfer controls to charges.py.
"""

from __future__ import annotations

import html
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


def inject_css() -> None:
    """A little CSS to tighten spacing and give the page a clearer hierarchy. Kept to
    layout-level, version-safe selectors so a Streamlit upgrade will not break it."""
    st.markdown(
        """
        <style>
          /* Start content higher: Streamlit's default top padding is large. Leave room
             at the bottom for the fixed 'unsaved changes' bar so it never covers a card. */
          .block-container { padding-top: 2.4rem; padding-bottom: 6rem; max-width: 1080px; }
          /* Expander headers read as section headers, not shouty controls. */
          details > summary { font-weight: 600; }
          /* Metric numbers a touch smaller so the three tiles do not dominate. */
          div[data-testid="stMetricValue"] { font-size: 1.5rem; }
          /* Tighten vertical rhythm between stacked blocks. */
          div[data-testid="stVerticalBlock"] { gap: 0.6rem; }

          /* Card header: name plus all tags on one wrapping row, tags aligned to the
             name. The name truncates with an ellipsis so a long name never pushes the
             tags around; on phones it truncates sooner to keep the row tidy. */
          .rc-head { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 6px; margin-bottom: 2px; }
          .rc-name { font-weight: 700; font-size: 1.03rem; line-height: 1.5; min-width: 0;
                     max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
          .rc-tag { border-radius: 6px; padding: 1px 8px; font-size: 0.74rem; white-space: nowrap;
                    line-height: 1.6; }
          @media (max-width: 640px) { .rc-name { max-width: 56vw; font-size: 1rem; } }

          /* Unsaved-changes bar: fixed at the bottom, out of document flow, so its
             appearance never reflows the page or snaps the scroll to the top. */
          .st-key-rc_savebar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
              background: var(--secondary-background-color, #1e1e26);
              border-top: 1px solid rgba(128,128,128,0.35);
              box-shadow: 0 -3px 12px rgba(0,0,0,0.18);
              padding: 0.5rem 1rem; }
          .st-key-rc_savebar > div { max-width: 1080px; margin: 0 auto; }

          /* On phones, remove side gutters eating width. */
          @media (max-width: 640px) { .block-container { padding-left: 0.8rem; padding-right: 0.8rem; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _title(d: dict) -> str:
    return d.get("brand_name") or d.get("merchant_key") or "(unknown merchant)"


_AMBER = ("#fff3cd", "#7a5b00")
_BLUE = ("#dbeafe", "#1e40af")
_RED = ("#fee2e2", "#991b1b")
_GRAY = ("#eef0f3", "#33363d")
_GREEN = ("#dcfce7", "#166534")

# Occurrences per year, to show a subscription's true annual cost (display only; the
# per-occurrence amount and cadence are the engine's, this just multiplies for the view).
_PERIODS_PER_YEAR = {"daily": 365, "weekly": 52, "fortnightly": 26, "monthly": 12,
                     "bi-monthly": 6, "quarterly": 4, "annual": 1}


def annual_cost(d: dict) -> float | None:
    """Roughly what this charge costs in a year, or None for an irregular cadence."""
    n = _PERIODS_PER_YEAR.get(d.get("cadence"))
    if not n:
        return None
    return float(d.get("representative_amount") or 0) * n


def next_expected(d: dict) -> str | None:
    """The next likely charge date (last seen + the typical gap), for active series."""
    import datetime as _dt
    gap, last = d.get("median_gap_days"), d.get("last_seen")
    if not gap or not last or d.get("status") != "active":
        return None
    try:
        y, m, day = (int(x) for x in str(last).split("-"))
        return (_dt.date(y, m, day) + _dt.timedelta(days=round(float(gap)))).isoformat()
    except Exception:
        return None


def _trail_list(d: dict, limit: int = 8) -> None:
    occ = d.get("occurrences") or []
    if not occ:
        return
    st.caption(f"Recent transactions ({len(occ)} total)")
    for o in occ[:limit]:
        st.caption(f"{o.get('date')} · {inr(o.get('amount'))}")


def _is_remembered(d: dict, remembered) -> bool:
    return bool(remembered) and charges_mod.merchant_ref(d) in remembered


def _badges(d: dict) -> list[tuple[str, str, str]]:
    """(text, background, foreground) chips. Explicit colors so they read on both
    light and dark themes."""
    out = []
    if d.get("price_creep"):
        out.append(("Price rose", *_AMBER))          # the amounts show in the trail
    if d.get("cross_account_duplicate"):
        out.append(("Duplicate · 2 accounts", *_BLUE))
    elif d.get("duplicate"):
        out.append(("Duplicate", *_BLUE))
    if d.get("status") == "stopped":
        out.append(("Stopped", *_RED))
    if d.get("missed_payment"):
        out.append(("Missed a cycle", *_RED))
    if d.get("internal_transfer_hint") and not d.get("is_internal_transfer"):
        out.append(("Maybe a transfer", *_GRAY))
    return out


def _sorted(charges: list[dict]) -> list[dict]:
    return sorted(charges, key=lambda c: (_TIER_RANK.get(c.get("confidence"), 3),
                                          -float(c.get("representative_amount") or 0)))


# --- staged edits: change many, save once -----------------------------------
# Every edit is held in the session and shown inline; nothing is written until the
# user presses Save. On the hosted app that turns one network write per click into a
# single batch write, so the screen stays responsive during a review.

def _pending() -> dict:
    return st.session_state.setdefault("pending_edits", {})


def _ver() -> int:
    return st.session_state.setdefault("edit_ver", 0)


def _stage(cid: str, **fields) -> None:
    p = _pending()
    entry = dict(p.get(cid, {}))
    entry.update(fields)
    p[cid] = entry


def _staged(cid: str, field: str, default):
    return _pending().get(cid, {}).get(field, default)


def _details_open(cid: str) -> bool:
    """Keep the Details expander open across the rerun an in-panel edit triggers, but
    not when only the outside Keep/Dismiss control changed (which would pop it open)."""
    return any(k in _pending().get(cid, {}) for k in ("category", "is_internal_transfer", "note"))


def _reset_edits() -> None:
    # Bumping the version changes every widget key, so controls re-init from stored state.
    st.session_state["pending_edits"] = {}
    st.session_state["edit_ver"] = _ver() + 1


def _cb_status(cid, key):
    val = {"Keep": charges_mod.CONFIRMED, "Dismiss": charges_mod.DISMISSED}.get(st.session_state.get(key))
    if val:
        _stage(cid, review_status=val)


def _cb_category(cid, key):
    _stage(cid, category=st.session_state[key])


def _cb_internal(cid, key):
    _stage(cid, is_internal_transfer=bool(st.session_state[key]))


def _cb_note(cid, key):
    _stage(cid, note=(st.session_state[key] or "").strip())


def _cb_promote(cid, category):
    _stage(cid, category=category, review_status=charges_mod.CONFIRMED, is_internal_transfer=False)


def save_bar(store) -> None:
    """A prominent bar (only when there are staged edits) to persist them all at once."""
    p = _pending()
    n = len(p)
    if not n:
        return
    # A keyed container so CSS can fix it to the bottom of the screen: being out of the
    # normal flow, it never reflows the page or snaps the scroll when it appears.
    with st.container(key="rc_savebar"):
        c0, c1, c2 = st.columns([3, 1, 1])
        c0.markdown(f"**{n} unsaved change{'s' if n != 1 else ''}**  \nNothing is stored until you save.")
        if c1.button("Save", type="primary", key="save_edits", use_container_width=True):
            with st.spinner("Saving your changes..."):
                charges_mod.apply_edits(store, p)
            _reset_edits()
            st.rerun()
        if c2.button("Discard", key="discard_edits", use_container_width=True):
            _reset_edits()
            st.rerun()


_CONF_COLORS = {"HIGH": _GREEN, "MEDIUM": _AMBER, "LOW": _GRAY}


def _tag(text: str, colors: tuple[str, str]) -> str:
    bg, fg = colors
    return f"<span class='rc-tag' style='background:{bg};color:{fg}'>{html.escape(text)}</span>"


def _all_chips(d: dict, remembered) -> list[str]:
    """Every status tag for a card, in one consistent style: confidence first, then
    detector flags, then whether it reflects a saved choice or an unsaved edit."""
    chips = []
    conf = d.get("confidence")
    if conf:
        chips.append(_tag(conf.title(), _CONF_COLORS.get(conf, _GRAY)))
    chips += [_tag(t, (bg, fg)) for t, bg, fg in _badges(d)]
    if _is_remembered(d, remembered):
        chips.append(_tag("✓ remembered", _GREEN))
    if d["id"] in _pending():
        chips.append(_tag("● unsaved", _AMBER))
    return chips


def _card_header(d: dict, remembered) -> None:
    """The name and all tags on one wrapping row. The name truncates (CSS ellipsis) so
    a long merchant name never distorts the tags, on desktop or mobile."""
    name = html.escape(_title(d))
    chips = "".join(_all_chips(d, remembered))
    st.markdown(f"<div class='rc-head'><span class='rc-name'>{name}</span>{chips}</div>",
                unsafe_allow_html=True)


# --- one charge card --------------------------------------------------------

def charge_card(store, d: dict, *, lens: str, key_prefix: str, remembered=None) -> None:
    """A minimal card: merchant, amount and yearly cost lead; one visible action
    (Keep / Dismiss); everything else sits behind a single 'Details & edit' expander.
    Edits are staged, not written, so the card stays put until the user saves."""
    cid = d["id"]
    ver = _ver()
    with st.container(border=True):
        _card_header(d, remembered)
        amount = f"{inr(d['representative_amount'])} {cadence_phrase(d.get('cadence',''))}"
        if lens == "A":
            ac = annual_cost(d)
            if ac:
                amount += f"  ·  ≈ {inr(ac)}/yr"
        st.markdown(amount)

        note = _staged(cid, "note", d.get("note") or "")
        if note:
            st.caption(f"📝 {note}")
        elif d.get("explanation"):
            st.caption(d["explanation"])

        meta = f"{d.get('occurrence_count', 0)} charges · {d.get('first_seen','')} to {d.get('last_seen','')}"
        if lens == "A":
            nx = next_expected(d)
            if nx:
                meta += f" · next ≈ {nx}"
        if lens == "B":
            ran = "ran on time" if d.get("status") == "active" and not d.get("missed_payment") else \
                  ("stopped" if d.get("status") == "stopped" else "check timing")
            meta += f" · {inr(d.get('total_amount', 0))} in window · {ran}"
        st.caption(meta)

        cur = _staged(cid, "review_status", d.get("review_status", charges_mod.PENDING))
        default = {charges_mod.CONFIRMED: "Keep", charges_mod.DISMISSED: "Dismiss"}.get(cur)
        skey = f"{key_prefix}st_{cid}_{ver}"
        st.segmented_control("Keep or dismiss", ["Keep", "Dismiss"], default=default, key=skey,
                             label_visibility="collapsed", on_change=_cb_status, args=(cid, skey))

        # Keep the editor open across the rerun an in-panel edit triggers, so it does
        # not collapse under the user mid-edit.
        with st.expander("Details & edit", expanded=_details_open(cid)):
            _edit_controls(d, key_prefix=key_prefix)


def _edit_controls(d: dict, *, key_prefix: str) -> None:
    cid = d["id"]
    ver = _ver()
    current = _staged(cid, "category", charges_mod.effective_category(d))
    options = _CATEGORY_ORDER[:]
    index = options.index(current) if current in options else 0
    ckey = f"{key_prefix}cat_{cid}_{ver}"
    st.selectbox("Category", options, index=index, format_func=lambda k: CATEGORY_LABELS[k],
                 key=ckey, on_change=_cb_category, args=(cid, ckey))

    itkey = f"{key_prefix}it_{cid}_{ver}"
    st.checkbox("This is a transfer between my own accounts",
                value=bool(_staged(cid, "is_internal_transfer", d.get("is_internal_transfer"))),
                key=itkey, on_change=_cb_internal, args=(cid, itkey))

    nkey = f"{key_prefix}note_{cid}_{ver}"
    st.text_input("Note", value=_staged(cid, "note", d.get("note", "")),
                  placeholder="What is this for? (e.g. 'Wife's Spotify')",
                  key=nkey, on_change=_cb_note, args=(cid, nkey))

    _trail_list(d)


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


def _render_group(store, charges: list[dict], *, lens: str, key_prefix: str,
                  remembered=None) -> None:
    ranked = _sorted(charges)
    high_med = [c for c in ranked if c.get("confidence") in ("HIGH", "MEDIUM")]
    low = [c for c in ranked if c.get("confidence") == "LOW"]
    for d in high_med:
        charge_card(store, d, lens=lens, key_prefix=key_prefix, remembered=remembered)
    if low:
        st.caption(f"Lower-confidence matches ({len(low)})")
        for d in low:
            charge_card(store, d, lens=lens, key_prefix=f"{key_prefix}low_", remembered=remembered)


def funnel_view(store, all_charges: list[dict], settings: classify.Settings,
                self_ids=None) -> None:
    placed = classify.classify_all(all_charges, settings, self_ids)
    subs, inv, aside = placed["subscriptions"], placed["investments"], placed["set_aside"]
    ignored = placed.get("ignored", {})
    aside_n = sum(len(v) for v in aside.values())
    ignored_n = sum(len(v) for v in ignored.values())
    remembered = set(charges_mod.load_merchant_prefs(store).keys())   # merchants you have decided on

    save_bar(store)      # persist staged edits in one go; only appears when there are some

    # Plain count metrics, no delta: the monthly figure is a total, not an increase.
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
        st.caption("Genuine, regular subscriptions or bills. Open a card to keep, dismiss, or edit it, "
                   "then Save when you are done.")
        if subs:
            _render_group(store, subs, lens="A", key_prefix="s_", remembered=remembered)
        else:
            st.info("No confident subscriptions found. Check Set aside, or lower the minimum value.")
    with tab_i:
        st.caption("Contributions only: this never shows value, returns, or NAV. Investments are "
                   "never treated as leaks.")
        if inv:
            _render_group(store, inv, lens="B", key_prefix="i_", remembered=remembered)
        else:
            st.info("No investments detected.")
    with tab_x:
        st.caption("Demoted so they do not crowd your subscriptions. Move any into Subscriptions or "
                   "Investments if we got it wrong.")
        if not aside_n and not ignored_n:
            st.info("Nothing set aside.")
        for group, items in sorted(aside.items(), key=lambda kv: -len(kv[1])):
            with st.expander(f"{group} ({len(items)})"):
                for d in _sorted(items):
                    _set_aside_card(store, d, key_prefix="x_", remembered=remembered)
        if ignored_n:
            with st.expander(f"Everything else we ignored ({ignored_n})"):
                st.caption("Random, one-off, or irregular spend with no steady amount or schedule "
                           "(daily food, cabs, cash, one-time payments). Kept out of the way, never "
                           "deleted. Move anything back if it is actually a subscription.")
                for group, items in sorted(ignored.items(), key=lambda kv: -len(kv[1])):
                    st.markdown(f"**{group}** · {len(items)}")
                    for d in _sorted(items):
                        _set_aside_card(store, d, key_prefix="ig_", remembered=remembered)


def _set_aside_card(store, d: dict, *, key_prefix: str = "x_", remembered=None) -> None:
    # Rendered inside a group expander, so it keeps its own controls inline (no nested
    # expander). Promote and note are staged like every other edit, saved together.
    cid = d["id"]
    ver = _ver()
    with st.container(border=True):
        _card_header(d, remembered)
        st.markdown(f"{inr(d['representative_amount'])} {cadence_phrase(d.get('cadence',''))}")
        reason = (d.get("_placement") or {}).get("reason", "")
        if reason:
            st.caption(reason)
        note = _staged(cid, "note", d.get("note") or "")
        if note:
            st.caption(f"📝 {note}")

        c1, c2 = st.columns(2)
        c1.button("→ Subscriptions", key=f"{key_prefix}ps_{cid}_{ver}", use_container_width=True,
                  on_click=_cb_promote, args=(cid, "subscription_bill"))
        c2.button("→ Investments", key=f"{key_prefix}pi_{cid}_{ver}", use_container_width=True,
                  on_click=_cb_promote, args=(cid, "investment_commitment"))
        nkey = f"{key_prefix}note_{cid}_{ver}"
        st.text_input("Note", value=_staged(cid, "note", d.get("note", "")),
                      placeholder="What is this for?", key=nkey, label_visibility="collapsed",
                      on_change=_cb_note, args=(cid, nkey))
        _trail_list(d, limit=6)
