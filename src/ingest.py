"""Read a statement into a raw string grid (deterministic, no model calls).

This is the one bank-specific-file-shaped layer. It does the least possible: turn
a CSV / XLS / XLSX / PDF upload into a rectangular grid of trimmed strings, making
no assumption about which row is the header or which column is what. All of that
judgment lives in normalize.py, so this stays a thin, format-only adapter.

Real bank exports carry preamble rows (account holder, address, statement period)
above the actual table, and those rows often have a different column count than
the table. So CSVs are read with the stdlib csv module (which tolerates ragged
rows) rather than pandas, and the header is found later in normalize.py.

PDF statements are read from their text layer (words plus positions) and folded
into the very same ragged grid: words are grouped into visual lines and split
into cells at the wide horizontal gaps between columns, so a preamble line becomes
a single-cell row just like the CSV path. Only digital (text-based) PDFs are
supported; a scanned/image PDF carries no text and is rejected with a clear
message (OCR is out of scope for this phase). Password-protected PDFs are common
for Indian e-statements, so read_table accepts a password and raises
PdfPasswordError when one is needed, letting the caller ask for it.

See recurring-charge-auditor-SPEC.md section 7.
"""

from __future__ import annotations

import csv
import io
import os
import statistics

class IngestError(RuntimeError):
    """The file could not be read as a supported format."""


class PdfPasswordError(IngestError):
    """The PDF is encrypted and the supplied password was missing or wrong.

    A distinct type so the UI can prompt for the password instead of treating it
    as an unreadable file."""


_CSV_EXTS = {".csv", ".txt", ".tsv"}
_XLSX_EXTS = {".xlsx", ".xlsm"}
_XLS_EXTS = {".xls"}
_PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = _CSV_EXTS | _XLSX_EXTS | _XLS_EXTS | _PDF_EXTS

# PDF grid reconstruction: words within this vertical distance are one visual line;
# columns are split where the horizontal gap exceeds a font-scaled threshold.
_LINE_TOL = 3.0


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


def read_table(data: bytes, filename: str, *, password: str | None = None) -> list[list[str]]:
    """Return a rectangular grid of trimmed strings from a CSV/XLS/XLSX/PDF upload.

    `password` is used only for an encrypted PDF. Raises IngestError on an
    unsupported extension or an unreadable file, and PdfPasswordError when a PDF
    needs a password that was not supplied (or was wrong).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in SUPPORTED_EXTS:
        raise IngestError(
            f"unsupported file type {ext!r}; upload CSV, XLS, XLSX, or PDF"
        )

    try:
        if ext in _CSV_EXTS:
            rows = _read_csv_rows(data)
        elif ext in _PDF_EXTS:
            rows = _read_pdf_rows(data, password)
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


# --- PDF (digital / text-based only) ----------------------------------------

def _import_pdf():
    """Lazily import the PDF stack, so nothing depends on it unless a PDF is read."""
    try:
        import pdfplumber
        from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
        from pdfminer.pdfparser import PDFParser
    except ImportError as e:  # pragma: no cover - needs the dep to hit
        raise IngestError("reading PDF requires the 'pdfplumber' package") from e
    return pdfplumber, PDFParser, PDFDocument, PDFPasswordIncorrect


def _read_pdf_rows(data: bytes, password: str | None) -> list[list[str]]:
    """Reconstruct a ragged row grid from a digital PDF's text layer.

    Raises PdfPasswordError if the file is encrypted and the password is missing
    or wrong, and IngestError if there is no text to read (a scanned image PDF)."""
    pdfplumber, PDFParser, PDFDocument, PDFPasswordIncorrect = _import_pdf()

    # Decide up front whether the file is encrypted (needs a password) rather than
    # simply unreadable: pdfplumber surfaces a wrong/missing password as a generic
    # parse error, so we ask pdfminer directly, where it is a distinct exception.
    try:
        PDFDocument(PDFParser(io.BytesIO(data)), password=password or "")
    except PDFPasswordIncorrect as e:
        raise PdfPasswordError(
            "this PDF is password-protected; enter its password and try again"
        ) from e
    except Exception:
        pass  # not a password problem; let pdfplumber raise the real error, if any

    lines: list[list[dict]] = []
    try:
        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            for page in pdf.pages:
                lines.extend(_page_lines(page))
    except PDFPasswordIncorrect as e:
        raise PdfPasswordError(
            "this PDF is password-protected; enter its password and try again"
        ) from e
    except Exception as e:
        raise IngestError(f"could not read the PDF: {e}") from e

    if not lines:
        raise IngestError(
            "no text found in this PDF; it looks like a scanned image. Upload a "
            "digital statement or its CSV/XLS/XLSX export instead."
        )
    threshold = _gap_threshold(lines)
    return [_split_line(ws, threshold) for ws in lines]


def _page_lines(page) -> list[list[dict]]:
    """Group a page's words into visual lines by their vertical (top) position."""
    words = sorted(page.extract_words(use_text_flow=False, keep_blank_chars=False),
                   key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    anchor: float | None = None
    for w in words:
        if anchor is None or (w["top"] - anchor) > _LINE_TOL:
            lines.append([w])
            anchor = w["top"]
        else:
            lines[-1].append(w)
    return lines


def _gap_threshold(lines: list[list[dict]]) -> float:
    """A column-gap threshold scaled to the font size (median word height), so it
    sits well above the small spaces between words but below the wide gaps between
    columns, at any font size."""
    heights = [w["bottom"] - w["top"] for ws in lines for w in ws]
    med = statistics.median(heights) if heights else 8.0
    return max(6.0, 0.9 * med)


def _split_line(ws: list[dict], threshold: float) -> list[str]:
    """Split one line's words into cells wherever the horizontal gap to the next
    word exceeds `threshold`; words closer than that join into one cell."""
    ws = sorted(ws, key=lambda w: w["x0"])
    cells: list[str] = []
    cur = ws[0]["text"]
    last_x1 = ws[0]["x1"]
    for w in ws[1:]:
        if w["x0"] - last_x1 > threshold:
            cells.append(cur)
            cur = w["text"]
        else:
            cur += " " + w["text"]
        last_x1 = w["x1"]
    cells.append(cur)
    return cells
