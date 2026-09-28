"""Bank-account domain: creation, the MVP cap, rename, and cascade delete."""

import pytest

from src import accounts, store
from src.storage import InMemoryBackend


def _store():
    return store.Store(backend=InMemoryBackend())


def test_create_and_list():
    s = _store()
    a = accounts.create_account(s, label="HDFC Salary", bank_name="HDFC")
    b = accounts.create_account(s, label="ICICI Savings", bank_name="ICICI")
    ids = [x["id"] for x in accounts.list_accounts(s)]
    assert a["id"] in ids and b["id"] in ids
    assert a["label"] == "HDFC Salary"


def test_empty_label_rejected():
    s = _store()
    with pytest.raises(accounts.BankAccountError):
        accounts.create_account(s, label="   ")


def test_cap_enforced():
    s = _store()
    # Fill exactly up to the product cap, then the next one must be rejected.
    for i in range(accounts.MAX_BANK_ACCOUNTS):
        accounts.create_account(s, label=f"Account {i + 1}")
    assert len(accounts.list_accounts(s)) == accounts.MAX_BANK_ACCOUNTS
    with pytest.raises(accounts.BankAccountError):
        accounts.create_account(s, label="One too many")


def test_rename():
    s = _store()
    a = accounts.create_account(s, label="Old")
    accounts.rename_account(s, a["id"], "New")
    assert accounts.get_account(s, a["id"])["label"] == "New"


def test_delete_cascades_to_charges():
    s = _store()
    a = accounts.create_account(s, label="HDFC")
    # A charge tied to this account plus one tied to another.
    s.save("charges", "chg_1", {"id": "chg_1", "bank_account_id": a["id"]})
    s.save("charges", "chg_2", {"id": "chg_2", "bank_account_id": "bank_other"})

    assert accounts.delete_account(s, a["id"])
    assert accounts.get_account(s, a["id"]) is None
    remaining = [c["id"] for c in s.list("charges")]
    assert remaining == ["chg_2"]           # only the other account's charge remains
