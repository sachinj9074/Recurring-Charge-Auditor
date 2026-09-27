"""Read a tabular statement into a raw string grid (deterministic, no model calls).

This is the one bank-specific-file-shaped layer. It does the least possible: turn
a CSV / XLS / XLSX upload into a rectangular grid of trimmed strings, making no
assumption about which row is the header or which column is what. All of that
judgment lives in normalize.py, so this stays a thin, format-only adapter.

Real bank exports carry preamble rows (account holder, address, statement period)
above the actual table, and those rows often have a different column count than
the table. So CSVs are read with the stdlib csv module (which tolerates ragged
rows) rather than pandas, and the header is found later in normalize.py.

See recurring-charge-auditor-SPEC.md section 7.
"""

from __future__ import annotations

import csv
import io
import os

class IngestError(RuntimeError):
    """The file could not be read as a supported tabular format."""


_CSV_EXTS = {".csv", ".txt", ".tsv"}
_XLSX_EXTS = {".xlsx", ".xlsm"}
_XLS_EXTS = {".xls"}
SUPPORTED_EXTS = _CSV_EXTS | _XLSX_EXTS | _XLS_EXTS


def _cell(v) -> str:
    """A grid cell as a clean string. NaN / None become empty."""
    if v is None:
        return ""
    if isinstance(v, float):
        try:
            import math
            if math.isnan(v):
                return ""
        except Exception:
            pass
    return str(v).strip()


def _clean_grid(rows) -> list[list[str]]:
    """Trim every cell and drop rows that are entirely empty."""
    grid = [[_cell(v) for v in row] for row in rows]
    return [r for r in grid if any(c for c in r)]


def read_table(data: bytes, filename: str) -> list[list[str]]:
    """Return a rectangular grid of trimmed strings from a CSV/XLS/XLSX upload.

    Raises IngestError on an unsupported extension or an unreadable file.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise IngestError(
            f"unsupported file type {ext!r}; upload CSV, XLS, or XLSX "
            "(PDF is a later phase)"
        )

    try:
        if ext in _CSV_EXTS:
            rows = _read_csv_rows(data)
        else:
            rows = _read_excel_rows(data, xlsx=(ext in _XLSX_EXTS))
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"could not read {filename!r}: {e}") from e

    grid = _clean_grid(rows)
    if not grid:
        raise IngestError("the file has no data rows")
    return grid


def _read_csv_rows(data: bytes) -> list[list[str]]:
    """Decode and parse a CSV/TSV, sniffing the delimiter, tolerating ragged rows."""
    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise IngestError("could not decode the file as text")

    sample = text[:8192]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        if any(counts.values()):
            delimiter = max(counts, key=counts.get)
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _read_excel_rows(data: bytes, *, xlsx: bool):
    try:
        import pandas as pd
    except ImportError as e:  # pragma: no cover - pandas is a hard dependency
        raise IngestError("reading Excel requires the 'pandas' package") from e
    engine = "openpyxl" if xlsx else "xlrd"
    df = pd.read_excel(io.BytesIO(data), header=None, dtype=str, engine=engine)
    return list(df.itertuples(index=False, name=None))
