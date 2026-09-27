"""Header detection, the three debit/credit conventions, parsing, and PII stripping."""

import datetime
from decimal import Decimal

from src import normalize
from src.normalize import (Mapping, apply_mapping, classify_header, counts,
                           detect_header_row, infer_mapping, parse_amount,
                           parse_date, parse_signed_amount)


# --- header classification --------------------------------------------------

def test_classify_header_roles():
    assert classify_header("Date") == normalize.DATE
    assert classify_header("Txn Date") == normalize.DATE
    assert classify_header("Narration") == normalize.DESC
    assert classify_header("Transaction Remarks") == normalize.DESC
    assert classify_header("Withdrawal Amt.") == normalize.DEBIT
    assert classify_header("Debit Amount") == normalize.DEBIT
    assert classify_header("Deposit Amt.") == normalize.CREDIT
    assert classify_header("Credit") == normalize.CREDIT
    assert classify_header("Dr/Cr") == normalize.FLAG
    assert classify_header("Closing Balance") == normalize.BALANCE
    assert classify_header("Amount (INR)") == normalize.AMOUNT
    assert classify_header("Cheque No") == normalize.OTHER


def test_detect_header_row_skips_preamble():
    grid = [
        ["HDFC BANK"],
        ["Account Holder: RAHUL MEHTA"],
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Balance"],
        ["01/03/2026", "X", "100.00", "", "900.00"],
    ]
    assert detect_header_row(grid) == 2


# --- amount parsing ---------------------------------------------------------

def test_parse_amount_indian_and_symbols():
    assert parse_amount("1,20,000.00") == Decimal("120000.00")
    assert parse_amount("Rs. 1,234.50") == Decimal("1234.50")
    assert parse_amount("₹499") == Decimal("499")
    assert parse_amount("500.00 Dr") == Decimal("500.00")   # Dr ignored for magnitude
    assert parse_amount("(250.00)") == Decimal("250.00")    # parentheses magnitude
    assert parse_amount("-99.00") == Decimal("99.00")       # sign ignored (magnitude)


def test_parse_amount_non_numeric():
    assert parse_amount("") is None
    assert parse_amount("   ") is None
    assert parse_amount("abc") is None


def test_parse_signed_amount():
    assert parse_signed_amount("-100.00") == (Decimal("-100.00"), None)
    assert parse_signed_amount("(100.00)") == (Decimal("-100.00"), None)
    assert parse_signed_amount("50.00") == (Decimal("50.00"), None)
    val, hint = parse_signed_amount("100.00 Cr")
    assert val == Decimal("100.00") and hint == "credit"
    val, hint = parse_signed_amount("100.00 Dr")
    assert val == Decimal("100.00") and hint == "debit"


# --- date parsing -----------------------------------------------------------

def test_parse_date_formats_day_first():
    assert parse_date("01/03/2026") == datetime.date(2026, 3, 1)
    assert parse_date("15-08-26") == datetime.date(2026, 8, 15)
    assert parse_date("2026-03-01") == datetime.date(2026, 3, 1)
    assert parse_date("9 Mar 2026") == datetime.date(2026, 3, 9)
    assert parse_date("31-13-2026") is None
    assert parse_date("") is None


# --- mapping + direction across the three schemes ---------------------------

def _two_col_grid():
    return [
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
        ["01/03/2026", "UPI-X-x@y", "100.00", "", "900.00"],
        ["02/03/2026", "SALARY", "", "5,000.00", "5,900.00"],
    ]


def test_two_column_scheme():
    m = infer_mapping(_two_col_grid())
    assert m.scheme == "two_column" and m.confident
    txns = apply_mapping(_two_col_grid(), m)
    assert (txns[0].direction, txns[0].amount) == ("debit", Decimal("100.00"))
    assert (txns[1].direction, txns[1].amount) == ("credit", Decimal("5000.00"))


def test_amount_flag_scheme():
    grid = [
        ["Txn Date", "Remarks", "Amount", "Dr/Cr", "Balance"],
        ["01-03-2026", "A", "100.00", "DR", "900"],
        ["02-03-2026", "B", "50.00", "CR", "950"],
    ]
    m = infer_mapping(grid)
    assert m.scheme == "amount_flag"
    txns = apply_mapping(grid, m)
    assert txns[0].direction == "debit"
    assert txns[1].direction == "credit"


def test_amount_flag_unknown_token_uses_default():
    grid = [
        ["Date", "Remarks", "Amount", "Type"],
        ["01-03-2026", "A", "100.00", "XYZ"],
    ]
    m = infer_mapping(grid)
    assert m.scheme == "amount_flag"
    txns = apply_mapping(grid, m)
    assert txns[0].direction == "debit"          # money_out default when flag unknown


def test_signed_scheme_and_direction_flip():
    grid = [
        ["Date", "Description", "Amount"],
        ["2026-03-01", "A", "-100.00"],
        ["2026-03-02", "B", "5000.00"],
    ]
    m = infer_mapping(grid)
    assert m.scheme == "signed" and not m.confident   # least certain: needs confirm
    txns = apply_mapping(grid, m)
    assert txns[0].direction == "debit"              # negative = money-out (default)
    assert txns[1].direction == "credit"

    m.money_out = "positive"
    flipped = apply_mapping(grid, m)
    assert flipped[0].direction == "credit"          # flip: now positive = money-out
    assert flipped[1].direction == "debit"


def test_flip_swaps_direction_wholesale():
    m = infer_mapping(_two_col_grid())
    normal = apply_mapping(_two_col_grid(), m)
    m.flip = True
    flipped = apply_mapping(_two_col_grid(), m)
    # Every direction is inverted relative to the un-flipped read.
    assert [t.direction for t in flipped] == [
        "credit" if t.direction == "debit" else "debit" for t in normal]


def test_signed_scheme_respects_explicit_drcr_over_sign():
    grid = [
        ["Date", "Description", "Amount"],
        ["2026-03-01", "A", "100.00 Dr"],            # positive number, explicit Dr
    ]
    m = infer_mapping(grid)
    txns = apply_mapping(grid, m)
    assert txns[0].direction == "debit"              # explicit marker wins over sign


# --- PII stripping ----------------------------------------------------------

def test_transactions_carry_no_pii_columns():
    grid = [
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
        ["01/03/2026", "UPI-X-x@y", "100.00", "", "888888.00"],
    ]
    m = infer_mapping(grid)
    t = apply_mapping(grid, m)[0]
    d = t.to_dict()
    assert set(d) == {"date", "description", "amount", "direction", "bank_account_id"}
    # The balance value is never carried into a transaction.
    assert "888888" not in str(d)


def test_counts():
    m = infer_mapping(_two_col_grid())
    c = counts(apply_mapping(_two_col_grid(), m))
    assert c["debit_count"] == 1 and c["credit_count"] == 1
    assert c["debit_total"] == 100.0 and c["credit_total"] == 5000.0
