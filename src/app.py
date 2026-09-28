"""Recurring Charge Auditor: the Streamlit app (surface only; logic lives in src/).

Flow: access-code gate -> Explore the demo / Use it for real -> (real) sign in or
sign up -> add bank accounts (cap 2) -> upload a statement -> the mandatory
mapping-confirmation checkpoint -> deterministic detection -> LLM enrichment (if a
key is set) -> the two-lens review with a confirm/dismiss loop. The raw statement
lives only in the session and is discarded after detection; only the derived
charge list and the user's confirmations are persisted (encrypted, per user).
"""

from __future__ import annotations

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import (accounts, auth, charges as charges_mod, classify, detect, enrich,  # noqa: E402
                 ingest, mapping, normalize, render, store as store_mod, users)
from src.storage import InMemoryBackend  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

st.set_page_config(page_title="Recurring Charge Auditor", page_icon="\U0001f9fe", layout="wide")

_SECRET_KEYS = ("ANTHROPIC_API_KEY", "FAST_MODEL", "JUDGMENT_MODEL", "R2_BUCKET",
                "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "REAL_ACCESS_CODE", "DEMO_LIVE_UPLOADS", "REAL_STATEMENTS_PER_DAY")


def _bridge_secrets() -> None:
    """Make config visible in os.environ at startup, from either source, so every
    check (the API-key gate, the fail-closed lock, storage) sees it before it runs:
    a local .env for local runs, and Streamlit Cloud secrets on the hosted deploy.

    This must happen at startup, not lazily inside enrichment, or has_api_key() reads
    False on the first pass and both enrichment and the access-code lock silently
    misbehave."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    try:
        for k in _SECRET_KEYS:
            if k in st.secrets and not os.environ.get(k):
                os.environ[k] = str(st.secrets[k])
    except Exception:
        pass


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def paid_enabled() -> bool:
    """The operator's key is used only when an invite code is also configured, so a
    forgotten REAL_ACCESS_CODE can never leave it open to the public. Without it, the
    paid steps (LLM column-mapping fallback and enrichment) are skipped; deterministic
    detection still runs everywhere."""
    return has_api_key() and auth.access_code_required()


def _init_state() -> None:
    for k, v in (("auth", None), ("data_key", None), ("demo_backend", None),
                 ("pending", None), ("demo_uploads", 0), ("access_ok", False)):
        st.session_state.setdefault(k, v)


# --- store access -----------------------------------------------------------

def get_store():
    a = st.session_state.auth
    if not a:
        return None
    if a["mode"] == "demo":
        return store_mod.Store(backend=st.session_state.demo_backend)
    return store_mod.user_store(a["user_id"], st.session_state.data_key)


def _load_demo_backend(user_id: str) -> InMemoryBackend:
    be = InMemoryBackend()
    base = os.path.join(REPO, "demo_cache", "users", user_id)
    for coll, fname in (("banks", "accounts.json"), ("charges", "charges.json")):
        path = os.path.join(base, fname)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for rec in json.load(f):
                    be.put(f"{coll}/{rec['id']}.json", json.dumps(rec).encode("utf-8"))
    sid = os.path.join(base, "self_ids.json")
    if os.path.exists(sid):
        with open(sid, encoding="utf-8") as f:
            be.put("meta/self_ids.json", json.dumps(json.load(f)).encode("utf-8"))
    return be


# --- landing / auth ---------------------------------------------------------

def landing() -> None:
    st.title("\U0001f9fe Recurring Charge Auditor")
    st.caption("Find every subscription and auto-debit in your bank statements, and tell the "
               "money leaks apart from the wealth-building. Built for Indian bank rails.")
    tab_demo, tab_real = st.tabs(["Explore the demo", "Use it for real"])
    with tab_demo:
        _demo_login()
    with tab_real:
        _real_auth()


def _demo_login() -> None:
    st.write("Log in as a sample profile to explore the full review on synthetic data. "
             "No API key or upload needed.")
    profiles = auth.demo_users()
    if not profiles:
        st.info("No demo profiles found. Run `python scripts/build_demo_cache.py` first.")
        return
    labels = {u.user_id: f"{u.name}  (password: {u.password_hint})" for u in profiles}
    with st.form("demo_login"):        # a form so Enter submits
        uid = st.selectbox("Profile", [u.user_id for u in profiles],
                           format_func=lambda i: labels[i], key="demo_pick")
        pw = st.text_input("Password", type="password", key="demo_pw")
        submitted = st.form_submit_button("Enter demo", type="primary", use_container_width=True)
    if submitted:
        u = auth.authenticate_demo(uid, pw)
        if u:
            st.session_state.demo_backend = _load_demo_backend(uid)
            st.session_state.auth = {"mode": "demo", "user_id": uid, "name": u.name}
            st.rerun()
        else:
            st.error("Wrong password. Each profile's password is shown next to its name.")


def _real_auth() -> None:
    if auth.real_mode_locked():
        st.warning("Real mode is turned off to protect the API key. A key is configured "
                   "but no access code is set, so sign in and sign up are disabled. Set "
                   "**REAL_ACCESS_CODE** (in `.env` locally, or Streamlit secrets on deploy) "
                   "to enable it. See DEPLOY.md.")
        st.caption("The demo tab still works, and deterministic detection runs without the key.")
        return
    if auth.access_code_required() and not st.session_state.access_ok:
        st.write("Access is invite-only for now. Enter the code you were given.")
        with st.form("access_form"):
            code = st.text_input("Access code", type="password", key="access_code")
            entered = st.form_submit_button("Enter", type="primary", use_container_width=True)
        if entered:
            if auth.check_access_code(code):
                st.session_state.access_ok = True
                st.rerun()
            else:
                st.error("Invalid access code.")
        return

    us = users.UserStore(store_mod.build_base_backend())
    login_tab, signup_tab = st.tabs(["Log in", "Create account"])
    with login_tab:
        with st.form("login_form"):
            u = st.text_input("Username", key="li_user")
            p = st.text_input("Password", type="password", key="li_pw")
            logged_in = st.form_submit_button("Log in", type="primary", use_container_width=True)
        if logged_in:
            res = us.authenticate(u, p)
            if res:
                acct, dk = res
                st.session_state.auth = {"mode": "real", "user_id": acct["user_id"], "name": acct["name"]}
                st.session_state.data_key = dk
                st.rerun()
            else:
                st.error("Wrong username or password.")
    with signup_tab:
        with st.form("signup_form"):
            u = st.text_input("Choose a username", key="su_user")
            n = st.text_input("Your name", key="su_name")
            p = st.text_input("Password (at least 8 characters)", type="password", key="su_pw")
            created = st.form_submit_button("Create account", type="primary", use_container_width=True)
        if created:
            try:
                acct = us.create(u, n, p)
                _, dk = us.authenticate(u, p)
                st.session_state.auth = {"mode": "real", "user_id": acct["user_id"], "name": acct["name"]}
                st.session_state.data_key = dk
                st.rerun()
            except users.UserError as e:
                st.error(str(e))
        st.caption("Your charge list is encrypted with your password. There is no password reset "
                   "in this MVP, so keep it safe.")


# --- sidebar: accounts + logout ---------------------------------------------

def _sidebar(store) -> None:
    a = st.session_state.auth
    is_demo = a["mode"] == "demo"
    with st.sidebar:
        st.markdown(f"### {a['name']}")
        st.caption("Demo profile (session only)" if is_demo else "Signed in")
        if st.button("Log out", key="logout", use_container_width=True):
            for k in ("auth", "data_key", "demo_backend", "pending", "access_ok"):
                st.session_state.pop(k, None)
            st.rerun()

        st.divider()
        st.markdown("**How it works**")
        if is_demo:
            st.caption("You are viewing sample data. Explore the tabs on the right; any edits you "
                       "make here are for this session only.")
        else:
            st.markdown("1. Add a bank account below\n"
                        "2. Upload a statement in the main panel\n"
                        "3. Review your charges, then **Save**")

        st.divider()
        st.markdown("**Bank accounts**", help=(
            "Just a nickname to tell your accounts apart, so an uploaded statement attaches "
            "to the right one when you have more than one. We never ask for account numbers, "
            "balances, or login details."))
        accts = accounts.list_accounts(store)
        if accts:
            st.caption("Your accounts")
            for ac in accts:
                with st.container(border=True):
                    st.markdown(f"**{ac['label']}**")
                    if ac.get("bank_name"):
                        st.caption(ac["bank_name"])
                    if not is_demo:
                        st.button("Remove", key=f"del_{ac['id']}", use_container_width=True,
                                  on_click=accounts.delete_account, args=(store, ac["id"]))
        elif not is_demo:
            st.caption("No accounts yet. Add one below to start.")

        if is_demo:
            return

        st.divider()
        if len(accts) < accounts.MAX_BANK_ACCOUNTS:
            st.markdown("**Add an account**")
            with st.form("add_acct", clear_on_submit=True):
                lbl = st.text_input("Account label", placeholder="HDFC Salary",
                                    help="A nickname just for you, e.g. 'HDFC Salary' or 'Joint "
                                         "account'. Not the account number.")
                bank = st.text_input("Bank name", placeholder="HDFC Bank",
                                     help="Which bank this is, so you can tell accounts apart. "
                                          "No account number or login needed.")
                if st.form_submit_button("Add account", use_container_width=True):
                    try:
                        accounts.create_account(store, label=lbl, bank_name=bank)
                        st.rerun()
                    except accounts.BankAccountError as e:
                        st.error(str(e))
        else:
            st.caption(f"MVP limit is {accounts.MAX_BANK_ACCOUNTS} accounts. Remove one to add another.")

        st.divider()
        with st.expander("Start over"):
            st.caption("Clear every detected charge. Your login and bank accounts stay; only the "
                       "derived charge list is removed. Re-upload a statement to rebuild it.")
            confirm = st.checkbox("Yes, clear all my detected charges", key="confirm_clear")
            if st.button("Clear all charges", disabled=not confirm, key="clear_all",
                         use_container_width=True):
                removed = charges_mod.clear_charges(store)
                st.success(f"Cleared {removed} charges.")
                st.rerun()


# --- upload + mapping checkpoint --------------------------------------------

def _controls_section(store, *, has_charges: bool) -> None:
    """Uploading and settings, tucked into expanders so the review is front-and-centre.
    'Add a statement' opens by default only before there are charges (or while a
    mapping checkpoint is pending); 'Detection settings' stays collapsed but still
    runs, so the review always has current settings."""
    pending = bool(st.session_state.get("pending"))
    with st.expander("➕ Add a statement", expanded=(not has_charges) or pending):
        _upload_body(store)
    with st.expander("⚙️ Detection settings", expanded=False):
        st.session_state["_settings"] = render.settings_control()


def _upload_body(store) -> None:
    a = st.session_state.auth
    accts = accounts.list_accounts(store)
    if a["mode"] != "demo" and not accts:
        st.info("Add a bank account in the sidebar first, then upload a statement here.")
        return
    if a["mode"] == "demo":
        cap = int(os.getenv("DEMO_LIVE_UPLOADS", "2"))
        st.caption(f"Demo: you may try up to {cap} of your own files this session. They are "
                   "processed live and kept only in this browser session, never saved.")

    acct_options = {ac["id"]: ac["label"] for ac in accts}
    acct_id = None
    if acct_options:
        acct_id = st.selectbox("Attach to which account?", list(acct_options),
                               format_func=lambda i: acct_options[i], key="up_acct")
    file = st.file_uploader("Upload a CSV, XLS, or XLSX statement", type=["csv", "xls", "xlsx"],
                            key="up_file")
    if file is not None and st.button("Read file", key="up_read", type="primary"):
        _start_pending(file, acct_id)

    if st.session_state.pending:
        _checkpoint(store)


def _start_pending(file, account_id) -> None:
    try:
        grid = ingest.read_table(file.getvalue(), file.name)
        m = mapping.infer(grid, allow_llm=paid_enabled())
    except normalize.MappingError:
        st.error("Could not work out the columns automatically. Please check this is a bank "
                 "statement export, or try a different file.")
        return
    except ingest.IngestError as e:
        st.error(f"Could not read this file: {e}")
        return
    st.session_state.pending = {"grid": grid, "mapping": m.to_dict(),
                                "account_id": account_id, "filename": file.name}
    st.rerun()


def _checkpoint(store) -> None:
    import pandas as pd
    p = st.session_state.pending
    grid = p["grid"]
    base_m = normalize.Mapping.from_dict(p["mapping"])

    st.markdown("### Step 2 of 2 · Confirm how your statement was read")
    st.caption("This is the most important step. A statement can label money-out in three different "
               "ways, and reading it backwards would make every number wrong. Check the debit total "
               "below looks right before detecting.")

    header = grid[base_m.header_row] if 0 <= base_m.header_row < len(grid) else []
    ncols = max(len(r) for r in grid)

    def label(i):
        return f"{i}: {header[i]}" if i < len(header) and header[i] else f"column {i}"

    c1, c2 = st.columns(2)
    date_col = c1.selectbox("Date column", range(ncols), index=min(base_m.date_col, ncols - 1),
                            format_func=label, key="cp_date")
    desc_col = c2.selectbox("Description column", range(ncols), index=min(base_m.desc_col, ncols - 1),
                            format_func=label, key="cp_desc")
    st.caption(f"Amount convention detected: **{base_m.scheme.replace('_', ' ')}**"
               + ("" if base_m.source == "deterministic" else "  (inferred by the model, please double-check)"))
    flip = st.checkbox("My debits and credits look swapped, flip the direction",
                       value=base_m.flip, key="cp_flip")

    prev_m = mapping.finalize_mapping(p["mapping"],
                                      {"date_col": date_col, "desc_col": desc_col, "flip": flip})
    cp = mapping.build_checkpoint(grid, prev_m)
    counts = cp["counts"]

    m1, m2, m3 = st.columns(3)
    m1.metric("Money-out rows (debits)", counts["debit_count"], render.inr(counts["debit_total"]))
    m2.metric("Money-in rows (credits)", counts["credit_count"], render.inr(counts["credit_total"]))
    m3.metric("Transactions read", counts["total_rows"])

    st.markdown("**First rows as read:**")
    if cp["preview"]:
        st.dataframe(pd.DataFrame(cp["preview"]), hide_index=True, use_container_width=True)
    else:
        st.warning("No transactions parsed with these settings. Adjust the columns above.")

    go, cancel, _ = st.columns([1, 1, 3])
    if go.button("Confirm and detect", type="primary", key="cp_go", disabled=not cp["preview"]):
        _run_detection(store, grid, prev_m, p["account_id"])
    if cancel.button("Cancel", key="cp_cancel"):
        st.session_state.pending = None
        st.rerun()


def _run_detection(store, grid, m, account_id) -> None:
    a = st.session_state.auth
    txns = mapping.to_transactions(grid, m, account_id)
    if not txns:
        st.error("No transactions to detect. Adjust the mapping.")
        return

    # Capture the holder name (transient) to spot transfers between the user's own
    # accounts. Stored per user (encrypted), never sent to the LLM.
    charges_mod.add_self_ids(store, normalize.extract_account_holder(grid))

    if a["mode"] == "demo":
        cap = int(os.getenv("DEMO_LIVE_UPLOADS", "2"))
        if st.session_state.demo_uploads >= cap:
            st.error("Demo upload limit reached for this session.")
            return
        st.session_state.demo_uploads += 1

    detected = detect.detect_charges(txns)

    # Enrichment (the only paid step): runs only when the key is gated by an access
    # code (paid_enabled), and real accounts are further bounded by a daily cap.
    if paid_enabled() and detected:
        allowed = True
        us = None
        if a["mode"] == "real":
            us = users.UserStore(store_mod.build_base_backend())
            cap = int(os.getenv("REAL_STATEMENTS_PER_DAY", "10"))
            allowed = us.statements_today(a["user_id"]) < cap
        if allowed:
            try:
                with st.spinner("Naming and categorizing merchants..."):
                    enrich.enrich_charges(detected)
                if us is not None:
                    us.record_statement(a["user_id"])
            except Exception:
                st.warning("Merchant naming was skipped (model unavailable); showing detector labels.")
        else:
            st.info("Daily processing limit reached, so merchant naming was skipped. "
                    "Detection still ran.")

    # Replace this account's prior charges so re-uploading a statement is idempotent
    # (never doubles). account_id is set for real accounts and demo profiles alike.
    charges_mod.persist_detection(store, detected, replace_bank_account_id=account_id)
    st.session_state.pending = None          # discard the raw grid
    st.success(f"Found {len(detected)} recurring charges. The raw statement has been discarded; "
               "only the derived list is kept.")
    st.rerun()


# --- review -----------------------------------------------------------------

def _review_section(store, all_c) -> None:
    if not all_c:
        st.info("No charges yet. Open **Add a statement** above and upload one to get started.")
        return
    settings = st.session_state.get("_settings") or classify.Settings()
    self_ids = charges_mod.load_self_ids(store)
    render.funnel_view(store, all_c, settings, self_ids)


# --- main -------------------------------------------------------------------

def _authed_app() -> None:
    store = get_store()
    _sidebar(store)
    all_c = charges_mod.list_charges(store)
    st.markdown("## \U0001f9fe Recurring Charge Auditor")
    _controls_section(store, has_charges=bool(all_c))
    _review_section(store, all_c)
    st.caption("This tool organizes and explains what leaves your account. It is not financial or "
               "investment advice.")


def main() -> None:
    _bridge_secrets()
    _init_state()
    render.inject_css()
    if st.session_state.auth is None:
        landing()
    else:
        _authed_app()


main()
