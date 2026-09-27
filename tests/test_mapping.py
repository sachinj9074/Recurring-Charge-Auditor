"""Mapping orchestration: PII-safe LLM fallback, checkpoint, and corrections."""

import json

import pytest

from src import mapping, normalize, schema
from src.normalize import MappingError


def _preamble_grid():
    return [
        ["Account Holder: RAHUL MEHTA"],
        ["Account Number: 50100123451234"],
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Balance"],
        ["01/03/2026", "UPI-SPOTIFY-spotify@hdfcbank", "119.00", "", "900.00"],
        ["02/03/2026", "SALARY CREDIT", "", "5000.00", "5900.00"],
    ]


# --- PII-safe column shapes -------------------------------------------------

def test_column_shapes_are_pii_safe():
    grid = _preamble_grid()
    shapes = mapping.column_shapes(grid, header_row=2)
    blob = json.dumps(shapes)
    # Account holder name and number live in the preamble, never in shapes.
    assert "MEHTA" not in blob
    assert "50100123451234" not in blob
    # Masked examples reveal structure but no raw digits.
    assert all(not any(c.isdigit() for c in s["example_masked"]) for s in shapes)


def test_column_shape_kinds_and_flags():
    shapes = mapping.column_shapes(_preamble_grid(), header_row=2)
    kinds = [s["kind"] for s in shapes]
    assert kinds[0] == "date"
    assert kinds[1] == "text"
    assert kinds[2] == "number" and kinds[3] == "number"
    assert shapes[1]["has_vpa"] and shapes[1]["has_rail_keyword"]


# --- LLM fallback (dependency-injected, no live call) -----------------------

def test_llm_infer_mapping_builds_mapping():
    def fake_complete(**kw):
        return {"header_row": 2, "date_col": 0, "desc_col": 1,
                "scheme": "two_column", "debit_col": 2, "credit_col": 3,
                "money_out": "negative"}
    m = mapping.llm_infer_mapping(_preamble_grid(), complete=fake_complete, header_row=2)
    assert m.scheme == "two_column" and m.source == "llm" and not m.confident
    assert m.debit_col == 2 and m.credit_col == 3


def test_llm_invalid_response_rejected_by_schema():
    def bad_complete(**kw):
        return {"date_col": 0}          # missing desc_col and scheme
    with pytest.raises(schema.SchemaError):
        mapping.llm_infer_mapping(_preamble_grid(), complete=bad_complete)


def test_llm_non_object_response_raises():
    def list_complete(**kw):
        return ["not", "an", "object"]
    with pytest.raises(MappingError):
        mapping.llm_infer_mapping(_preamble_grid(), complete=list_complete)


def test_infer_falls_back_to_llm_when_no_header():
    grid = [["2026-03-01", "UPI-X-x@y", "100"]]     # no header keywords at all
    called = {"n": 0}

    def fake_complete(**kw):
        called["n"] += 1
        return {"header_row": -1, "date_col": 0, "desc_col": 1,
                "scheme": "signed", "amount_col": 2, "money_out": "negative"}

    m = mapping.infer(grid, complete=fake_complete)
    assert called["n"] == 1 and m.source == "llm" and m.scheme == "signed"


def test_infer_uses_deterministic_when_possible():
    grid = [
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt."],
        ["01/03/2026", "X", "100.00", ""],
    ]

    def should_not_call(**kw):
        raise AssertionError("LLM should not be called when deterministic succeeds")

    m = mapping.infer(grid, complete=should_not_call)
    assert m.source == "deterministic" and m.scheme == "two_column"


# --- checkpoint + corrections -----------------------------------------------

def test_build_checkpoint_payload():
    grid = [
        ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Balance"],
        ["01/03/2026", "UPI-X-x@y", "100.00", "", "900.00"],
        ["02/03/2026", "SALARY", "", "5000.00", "5900.00"],
    ]
    m = normalize.infer_mapping(grid)
    cp = mapping.build_checkpoint(grid, m)
    assert cp["scheme"] == "two_column"
    assert cp["columns"]["date"] == "Date"
    assert len(cp["preview"]) <= 3 and cp["preview"]
    assert cp["counts"]["debit_count"] == 1 and cp["counts"]["credit_count"] == 1
    assert cp["needs_direction_confirm"] is False


def test_finalize_mapping_applies_direction_flip():
    grid = [
        ["Date", "Description", "Amount"],
        ["2026-03-01", "A", "-100.00"],
    ]
    m = normalize.infer_mapping(grid)
    final = mapping.finalize_mapping(m.to_dict(), {"money_out": "positive"})
    assert final.money_out == "positive" and final.confident
    txns = mapping.to_transactions(grid, final)
    assert txns[0].direction == "credit"      # flip took effect
