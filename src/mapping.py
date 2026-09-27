"""Column-mapping orchestration and the confirmation checkpoint (LLM fallback only).

Deterministic keyword detection (normalize.infer_mapping) handles most clean bank
exports. When headers are missing or ambiguous, an LLM fallback proposes the
mapping instead, which is what lets the tool open to banks we have never seen with
zero per-bank code.

PII discipline for the fallback: the model is shown column *shapes* (kind, a
digit-masked example, and a few structural flags) plus header labels, never raw
cell values. Header labels like "Narration" are not PII; account numbers, names,
and balances live in the values, which are never sent. The model only chooses
which column is which; it never reads or returns a number the pipeline trusts.

Every path ends at a mapping-confirmation checkpoint: the app shows the inferred
mapping, a three-row preview, and the debit/credit counts, and the user confirms
or corrects it (especially which direction is money-out) before detection runs.
This checkpoint is non-negotiable (recurring-charge-auditor-SPEC.md sections 4, 7).
"""

from __future__ import annotations

import re

from src import normalize, schema
from src.normalize import Mapping, MappingError

_MAPPING_KEYS = ("header_row", "date_col", "desc_col", "scheme", "debit_col",
                 "credit_col", "amount_col", "flag_col", "money_out")

_RAIL = re.compile(r"(?i)\b(upi|neft|imps|ach|nach|ecs|mandate|si|autopay|pos|atm)\b")


# --- PII-safe column shapes (what the LLM fallback is allowed to see) --------

def column_shapes(grid: list[list[str]], header_row: int | None) -> list[dict]:
    """A privacy-preserving summary of each column: kind, a digit-masked example,
    and structural flags. Never includes raw values, so no account number, name,
    or balance can reach the model."""
    ncols = max((len(r) for r in grid), default=0)
    data_start = (header_row + 1) if (header_row is not None and header_row >= 0) else 0
    shapes = []
    for c in range(ncols):
        header = grid[header_row][c] if (header_row is not None and 0 <= header_row < len(grid)
                                         and c < len(grid[header_row])) else ""
        vals = [row[c] for row in grid[data_start:] if c < len(row) and row[c].strip()]
        shapes.append({
            "index": c,
            "header": header,
            "kind": _kind_of(vals),
            "example_masked": _mask(vals[0]) if vals else "",
            "has_vpa": any("@" in v for v in vals[:40]),
            "has_rail_keyword": any(_RAIL.search(v) for v in vals[:40]),
            "avg_len": round(sum(len(v) for v in vals) / len(vals), 1) if vals else 0,
        })
    return shapes


def _kind_of(vals: list[str]) -> str:
    if not vals:
        return "empty"
    n = len(vals)
    dates = sum(1 for v in vals if normalize.parse_date(v))
    nums = sum(1 for v in vals if normalize.parse_amount(v) is not None)
    flags = sum(1 for v in vals if normalize._norm(v).replace(" ", "")
                in (normalize._DEBIT_FLAGS | normalize._CREDIT_FLAGS))
    if flags > n / 2:
        return "flag"
    if dates > n / 2:
        return "date"
    if nums > n / 2:
        return "number"
    return "text"


def _mask(v: str) -> str:
    """Digit-masked shape of a value: digits to '#', letters kept only in short
    tokens so structure shows without leaking content."""
    masked = re.sub(r"\d", "#", v)
    return masked[:24]


# --- LLM fallback -----------------------------------------------------------

_SYSTEM = (
    "You map bank-statement columns to fields. You are given a list of columns, "
    "each with an index, its header label, a value kind, a digit-masked example, "
    "and structural flags. Decide which column is the date, which is the "
    "description/narration, and how amounts and direction are represented. "
    "Return ONLY a JSON object with keys: header_row (int, -1 if none), date_col, "
    "desc_col, scheme ('two_column' | 'amount_flag' | 'signed'), and whichever of "
    "debit_col, credit_col, amount_col, flag_col apply (others null), plus "
    "money_out ('negative' or 'positive' for signed; for amount_flag the token "
    "meaning money-out). Do not include any other keys or prose."
)


def llm_infer_mapping(grid: list[list[str]], *, complete=None,
                      header_row: int | None = None) -> Mapping:
    """Ask the model to map columns from PII-safe shapes, validate, and build a
    Mapping. `complete` defaults to model.complete_json but can be injected for
    tests."""
    if complete is None:
        from src.model import complete_json as complete
    shapes = column_shapes(grid, header_row)
    user = (
        "Columns:\n"
        + "\n".join(
            f"[{s['index']}] header={s['header']!r} kind={s['kind']} "
            f"example={s['example_masked']!r} has_vpa={s['has_vpa']} "
            f"has_rail_keyword={s['has_rail_keyword']} avg_len={s['avg_len']}"
            for s in shapes
        )
        + "\n\nReturn the JSON mapping object."
    )
    raw = complete(system=_SYSTEM, user=user, tier="fast", max_tokens=600)
    if not isinstance(raw, dict):
        raise MappingError("LLM mapping response was not a JSON object")
    picked = {k: raw.get(k) for k in _MAPPING_KEYS if k in raw}
    picked.setdefault("header_row", header_row if header_row is not None else -1)
    picked.setdefault("money_out", normalize.SIGN_NEGATIVE)
    schema.validate(picked, "mapping")
    m = Mapping.from_dict({**_defaults(), **picked})
    m.source = "llm"
    m.confident = False
    return m


def _defaults() -> dict:
    return {"header_row": -1, "date_col": 0, "desc_col": 1, "scheme": "signed",
            "debit_col": None, "credit_col": None, "amount_col": None,
            "flag_col": None, "money_out": normalize.SIGN_NEGATIVE,
            "source": "llm", "confident": False}


# --- orchestration + checkpoint ---------------------------------------------

def infer(grid: list[list[str]], *, allow_llm: bool = True, complete=None) -> Mapping:
    """Deterministic first; LLM fallback only when the deterministic pass fails."""
    try:
        return normalize.infer_mapping(grid)
    except MappingError:
        if not allow_llm:
            raise
        header_row = normalize.detect_header_row(grid)
        return llm_infer_mapping(grid, complete=complete,
                                 header_row=header_row if header_row >= 0 else None)


def build_checkpoint(grid: list[list[str]], mapping: Mapping) -> dict:
    """The payload the app shows at the mandatory confirmation checkpoint."""
    txns = normalize.apply_mapping(grid, mapping)
    header = grid[mapping.header_row] if 0 <= mapping.header_row < len(grid) else []

    def name(idx):
        return header[idx] if (idx is not None and 0 <= idx < len(header)) else (
            f"column {idx}" if idx is not None else None)

    columns = {"date": name(mapping.date_col), "description": name(mapping.desc_col)}
    if mapping.scheme == "two_column":
        columns["money_out (debit)"] = name(mapping.debit_col)
        columns["money_in (credit)"] = name(mapping.credit_col)
    elif mapping.scheme == "amount_flag":
        columns["amount"] = name(mapping.amount_col)
        columns["direction flag"] = name(mapping.flag_col)
    else:
        columns["amount (signed)"] = name(mapping.amount_col)

    return {
        "mapping": mapping.to_dict(),
        "scheme": mapping.scheme,
        "source": mapping.source,
        "columns": columns,
        "preview": [t.to_dict() for t in txns[:3]],
        "counts": normalize.counts(txns),
        "needs_direction_confirm": mapping.scheme in ("signed", "amount_flag"),
    }


def finalize_mapping(mapping_dict: dict, overrides: dict | None = None) -> Mapping:
    """Apply the user's corrections from the checkpoint and return the Mapping to
    run detection on. Overrides may set any column index, the scheme, or
    money_out (the direction flip)."""
    merged = dict(mapping_dict)
    if overrides:
        for k in _MAPPING_KEYS:
            if k in overrides and overrides[k] is not None:
                merged[k] = overrides[k]
    merged["confident"] = True         # the user has now confirmed it
    return Mapping.from_dict(merged)


def to_transactions(grid: list[list[str]], mapping: Mapping,
                    bank_account_id: str | None = None):
    """Apply a confirmed mapping to produce normalized transactions."""
    return normalize.apply_mapping(grid, mapping, bank_account_id)
