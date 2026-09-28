"""Bank-account domain within a user (deterministic, no model calls).

A user has one or more bank accounts; every transaction and every detected charge
carries its `bank_account_id`, so the schema and engine are multi-account from
day one (see recurring-charge-auditor-SPEC.md section 9). The account limit is
a *product* cap for the MVP, enforced here, not an architectural one: raising
MAX_BANK_ACCOUNTS is the only change needed to allow more.

Records are stored in the user's encrypted Store under the 'banks' collection.
No account number or balance is ever kept, only a user-chosen label and an
optional free-text bank name.
"""

from __future__ import annotations

import datetime

from src import schema
from src.ids import new_id

MAX_BANK_ACCOUNTS = 5   # MVP UI/product cap; the engine has no such limit.

_BANKS = "banks"
_CHARGES = "charges"


class BankAccountError(RuntimeError):
    """A friendly, user-facing bank-account problem (cap reached, empty label)."""


def list_accounts(store) -> list[dict]:
    """A user's bank accounts, oldest first."""
    return sorted(store.list(_BANKS), key=lambda a: a.get("created", ""))


def get_account(store, account_id: str) -> dict | None:
    try:
        return store.load(_BANKS, account_id)
    except Exception:
        return None


def create_account(store, *, label: str, bank_name: str = "",
                   account_type: str | None = None) -> dict:
    """Create a bank account. Raises BankAccountError past the product cap or on
    an empty label."""
    if not (label or "").strip():
        raise BankAccountError("Give the account a name, for example 'HDFC Salary'.")
    if len(list_accounts(store)) >= MAX_BANK_ACCOUNTS:
        raise BankAccountError(
            f"This MVP supports up to {MAX_BANK_ACCOUNTS} accounts. Remove one to add another."
        )
    rec = {
        "id": new_id("bank"),
        "label": label.strip(),
        "bank_name": (bank_name or "").strip(),
        "account_type": account_type,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    schema.validate(rec, "bank_account")
    store.save(_BANKS, rec["id"], rec)
    return rec


def rename_account(store, account_id: str, label: str) -> dict:
    if not (label or "").strip():
        raise BankAccountError("The name cannot be empty.")
    rec = get_account(store, account_id)
    if rec is None:
        raise BankAccountError("No such account.")
    rec["label"] = label.strip()
    schema.validate(rec, "bank_account")
    store.save(_BANKS, account_id, rec)
    return rec


def delete_account(store, account_id: str, *, cascade: bool = True) -> bool:
    """Delete a bank account. With cascade, also delete every charge that belongs
    to it (a charge is tied to one account and never hops between accounts)."""
    if cascade:
        for chg in store.list(_CHARGES):
            if chg.get("bank_account_id") == account_id and chg.get("id"):
                store.delete(_CHARGES, chg["id"])
    return store.delete(_BANKS, account_id)
