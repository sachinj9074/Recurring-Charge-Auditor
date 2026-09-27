"""Turn a raw statement grid into normalized transactions (deterministic, no model).

This is the correctness-critical layer. Two failures here are the most dangerous
in the whole tool, because a wrong read still looks perfectly clean downstream:

  1. Reading the debit/credit direction backwards (booking deposits as expenses,
     or dropping debits). Indian statements use three conventions and we handle
     all three explicitly; which direction is money-out is always surfaced for
     confirmation (see mapping.py and the app checkpoint).
  2. Leaking PII. Only date, description, amount, and direction are carried
     forward. Name, address, account number, and running balance are never
     mapped into a transaction, so they never reach detection, the LLM, or storage.

The three amount/direction conventions (schemes):
  - two_column : separate Withdrawal/Debit and Deposit/Credit columns.
  - amount_flag: one Amount column plus a Dr/Cr type column.
  - signed     : one signed Amount column (sign, or a Dr/Cr suffix, gives direction).

See recurring-charge-auditor-SPEC.md sections 5, 7.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# --- role vocabulary --------------------------------------------------------

DATE = "date"
DESC = "description"
DEBIT = "debit"
CREDIT = "credit"
AMOUNT = "amount"
FLAG = "flag"
BALANCE = "balance"
OTHER = "other"

# Debit/credit flag tokens (for the amount_flag scheme and Dr/Cr suffixes).
_DEBIT_FLAGS = {"dr", "d", "debit", "w", "withdrawal", "wd", "debit(dr)"}
_CREDIT_FLAGS = {"cr", "c", "credit", "deposit", "dep", "credit(cr)"}


def _norm(s: str) -> str:
    """Lowercase, turn non-alphanumeric into spaces, collapse runs, strip."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def classify_header(cell: str) -> str:
    """Map one header cell to a column role. Order matters: the more specific
    debit/credit/balance checks come before the generic 'amount' check so that
    'Withdrawal Amount' resolves to debit, not amount."""
    h = _norm(cell)
    if not h:
        return OTHER
    tokens = h.split()
    if "balance" in h:
        return BALANCE
    if "date" in tokens or h.endswith(" date") or h == "date" or "date" in h:
        return DATE
    if any(k in h for k in ("narration", "description", "particular", "remark", "detail")):
        return DESC
    # explicit debit/credit words
    if any(k in h for k in ("withdrawal", "withdrawl")) or "debit" in h:
        return DEBIT
    if "deposit" in h or "credit" in h:
        return CREDIT
    # a combined Dr/Cr type/indicator column
    if ("dr" in tokens and "cr" in tokens) or h in ("drcr", "type", "transaction type", "indicator"):
        return FLAG
    # short standalone Dr / Cr headers (a two-column pair)
    if h in ("dr", "dr amt", "dr amount", "paid out"):
        return DEBIT
    if h in ("cr", "cr amt", "cr amount", "paid in"):
        return CREDIT
    if "amount" in h or h == "amt":
        return AMOUNT
    return OTHER


# --- header-row detection ---------------------------------------------------

def _row_role_score(row: list[str]) -> tuple[int, dict[str, int]]:
    """How header-like a row is: number of distinct meaningful roles it carries."""
    roles: dict[str, int] = {}
    for i, cell in enumerate(row):
        r = classify_header(cell)
        if r != OTHER:
            roles.setdefault(r, i)
    # A header must at least look like it has a date and some money column.
    money = any(r in roles for r in (DEBIT, CREDIT, AMOUNT))
    score = len(roles) + (2 if (DATE in roles and money) else 0)
    return score, roles


def detect_header_row(grid: list[list[str]]) -> int:
    """Index of the most header-like row, or -1 if none looks like a header."""
    best_idx, best_score = -1, 0
    for i, row in enumerate(grid[:25]):   # headers live near the top
        score, roles = _row_role_score(row)
        has_date = DATE in roles
        has_money = any(r in roles for r in (DEBIT, CREDIT, AMOUNT))
        if has_date and has_money and score > best_score:
            best_idx, best_score = i, score
    return best_idx


# --- value parsing ----------------------------------------------------------

_CURRENCY = re.compile(r"(?i)(inr|rs\.?|₹)")
_DR = re.compile(r"(?i)(?<![a-z])dr(?![a-z])")
_CR = re.compile(r"(?i)(?<![a-z])cr(?![a-z])")


def parse_amount(s: str) -> Decimal | None:
    """Magnitude of a money cell, ignoring sign and Dr/Cr. None if not numeric.

    Handles Indian grouping (1,23,456.78), currency symbols, and stray spaces."""
    if s is None:
        return None
    t = _CURRENCY.sub("", str(s)).strip()
    t = t.replace("(", "").replace(")", "")
    t = _DR.sub("", t)
    t = _CR.sub("", t)
    t = t.replace(",", "").replace(" ", "").replace(" ", "")
    t = t.lstrip("+-")
    if not t or not re.search(r"\d", t):
        return None
    try:
        val = Decimal(t)
    except InvalidOperation:
        return None
    return abs(val)


def parse_signed_amount(s: str) -> tuple[Decimal | None, str | None]:
    """Signed value and an optional direction hint from a single amount cell.

    Returns (value, hint) where value keeps its sign (parentheses or a leading/
    trailing minus mean negative) and hint is 'debit'/'credit' if a Dr/Cr marker
    is present, else None. The caller decides how an unmarked sign maps to a
    direction (that is the ambiguity the confirmation checkpoint exists for).
    """
    if s is None:
        return None, None
    raw = str(s).strip()
    if not raw:
        return None, None
    hint = None
    if _DR.search(raw):
        hint = "debit"
    elif _CR.search(raw):
        hint = "credit"
    negative = ("(" in raw and ")" in raw) or raw.strip().startswith("-") or raw.strip().endswith("-")
    mag = parse_amount(raw)
    if mag is None:
        return None, hint
    return (-mag if negative else mag), hint


_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y",
    "%d-%B-%Y", "%d %B %Y",
    "%Y-%m-%d", "%Y/%m/%d",
)


def parse_date(s: str) -> datetime.date | None:
    """Parse a date cell, day-first (the Indian convention). None if unparseable."""
    if not s:
        return None
    t = str(s).strip()
    # Drop a trailing time component if present.
    t = re.split(r"[ T]", t)[0] if re.search(r"\d{2}:\d{2}", t) else t
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


# --- mapping ----------------------------------------------------------------

# amount_flag: which flag value means money-out. signed: which sign means money-out.
FLAG_DEBIT = "debit_flag"     # placeholder default for amount_flag
SIGN_NEGATIVE = "negative"    # default: a negative amount is money-out


@dataclass
class Mapping:
    """How a grid's columns map to transaction fields, plus how direction is read.

    `scheme` is one of 'two_column' | 'amount_flag' | 'signed'. `money_out` records
    the direction convention the user can flip at the checkpoint:
      - two_column : unused (the debit column is money-out by construction).
      - amount_flag: the set of flag tokens that mean money-out (default Dr-like).
      - signed     : 'negative' or 'positive' (which sign is money-out).
    """
    header_row: int
    date_col: int
    desc_col: int
    scheme: str
    debit_col: int | None = None
    credit_col: int | None = None
    amount_col: int | None = None
    flag_col: int | None = None
    money_out: str = SIGN_NEGATIVE
    source: str = "deterministic"     # or 'llm'
    confident: bool = True

    def to_dict(self) -> dict:
        return {
            "header_row": self.header_row, "date_col": self.date_col,
            "desc_col": self.desc_col, "scheme": self.scheme,
            "debit_col": self.debit_col, "credit_col": self.credit_col,
            "amount_col": self.amount_col, "flag_col": self.flag_col,
            "money_out": self.money_out, "source": self.source,
            "confident": self.confident,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mapping":
        return cls(**{k: d.get(k) for k in (
            "header_row", "date_col", "desc_col", "scheme", "debit_col",
            "credit_col", "amount_col", "flag_col", "money_out", "source",
            "confident")})


class MappingError(RuntimeError):
    """The grid could not be mapped deterministically (headers missing/ambiguous)."""


def infer_mapping(grid: list[list[str]]) -> Mapping:
    """Deterministically infer a Mapping from header keywords, or raise MappingError.

    Raising is the signal for the caller (mapping.py) to try the LLM fallback."""
    hdr = detect_header_row(grid)
    if hdr < 0:
        raise MappingError("no header row detected")
    roles: dict[str, int] = {}
    for i, cell in enumerate(grid[hdr]):
        r = classify_header(cell)
        if r != OTHER and r not in roles:   # first column wins a role
            roles[r] = i
    if DATE not in roles:
        raise MappingError("no date column detected")
    if DESC not in roles:
        # Fall back to the widest text column as the description.
        desc = _widest_text_col(grid, hdr, exclude=set(roles.values()))
        if desc is None:
            raise MappingError("no description column detected")
        roles[DESC] = desc

    base = dict(header_row=hdr, date_col=roles[DATE], desc_col=roles[DESC])

    if DEBIT in roles and CREDIT in roles:
        return Mapping(scheme="two_column", debit_col=roles[DEBIT],
                       credit_col=roles[CREDIT], **base)
    if AMOUNT in roles and FLAG in roles:
        return Mapping(scheme="amount_flag", amount_col=roles[AMOUNT],
                       flag_col=roles[FLAG], money_out=FLAG_DEBIT, **base)
    if AMOUNT in roles:
        # Single amount column, no flag: direction comes from the sign. This is
        # the least certain case, so mark it for explicit confirmation.
        return Mapping(scheme="signed", amount_col=roles[AMOUNT],
                       money_out=SIGN_NEGATIVE, confident=False, **base)
    raise MappingError("no usable amount columns detected")


def _widest_text_col(grid, hdr, exclude) -> int | None:
    """Column whose data cells hold the longest average text: the narration."""
    ncols = max((len(r) for r in grid), default=0)
    best, best_len = None, 0.0
    for c in range(ncols):
        if c in exclude:
            continue
        vals = [row[c] for row in grid[hdr + 1:] if c < len(row) and row[c]]
        if not vals:
            continue
        # skip columns that are mostly numeric or date-like
        numericish = sum(1 for v in vals if parse_amount(v) is not None or parse_date(v))
        if numericish > len(vals) / 2:
            continue
        avg = sum(len(v) for v in vals) / len(vals)
        if avg > best_len:
            best, best_len = c, avg
    return best if best_len >= 6 else None


# --- applying a mapping to produce transactions -----------------------------

@dataclass
class Transaction:
    date: datetime.date
    description: str
    amount: Decimal            # always positive
    direction: str             # 'debit' | 'credit'
    bank_account_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": float(self.amount),
            "direction": self.direction,
            "bank_account_id": self.bank_account_id,
        }


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _flag_direction(flag_cell: str) -> str | None:
    f = _norm(flag_cell).replace(" ", "")
    if f in _DEBIT_FLAGS:
        return "debit"
    if f in _CREDIT_FLAGS:
        return "credit"
    return None


def apply_mapping(grid: list[list[str]], m: Mapping,
                  bank_account_id: str | None = None) -> list[Transaction]:
    """Build normalized transactions from a grid and a confirmed mapping.

    Rows without a parseable date or a non-zero amount are skipped (they are
    subtotals, footers, or blank separators). PII is dropped by construction:
    only the four transaction fields are ever read out of the grid."""
    out: list[Transaction] = []
    for row in grid[m.header_row + 1:]:
        d = parse_date(_cell(row, m.date_col))
        if d is None:
            continue
        desc = _cell(row, m.desc_col).strip()
        amount, direction = _extract_money(row, m)
        if amount is None or amount == 0:
            continue
        out.append(Transaction(d, desc, amount, direction, bank_account_id))
    return out


def _extract_money(row: list[str], m: Mapping) -> tuple[Decimal | None, str | None]:
    if m.scheme == "two_column":
        debit = parse_amount(_cell(row, m.debit_col))
        credit = parse_amount(_cell(row, m.credit_col))
        if debit and debit != 0:
            return debit, "debit"
        if credit and credit != 0:
            return credit, "credit"
        return None, None
    if m.scheme == "amount_flag":
        amt = parse_amount(_cell(row, m.amount_col))
        if amt is None or amt == 0:
            return None, None
        direction = _flag_direction(_cell(row, m.flag_col))
        if direction is None:
            # Unknown flag token: fall back to the confirmed default.
            direction = "debit" if m.money_out == FLAG_DEBIT else "credit"
        return amt, direction
    if m.scheme == "signed":
        val, hint = parse_signed_amount(_cell(row, m.amount_col))
        if val is None or val == 0:
            return None, None
        if hint:                                   # an explicit Dr/Cr wins
            return abs(val), hint
        if m.money_out == SIGN_NEGATIVE:
            return abs(val), ("debit" if val < 0 else "credit")
        return abs(val), ("debit" if val > 0 else "credit")
    return None, None


def counts(transactions: list[Transaction]) -> dict:
    """Debit/credit counts and totals, for the mapping-confirmation checkpoint."""
    deb = [t for t in transactions if t.direction == "debit"]
    cred = [t for t in transactions if t.direction == "credit"]
    return {
        "debit_count": len(deb),
        "credit_count": len(cred),
        "debit_total": float(sum((t.amount for t in deb), Decimal(0))),
        "credit_total": float(sum((t.amount for t in cred), Decimal(0))),
        "total_rows": len(transactions),
    }
