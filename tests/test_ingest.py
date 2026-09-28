"""Reading CSV/XLS/XLSX into a raw grid, including ragged preamble rows."""

import io

import pytest

from src import ingest


def test_reads_csv_with_ragged_preamble():
    csv_text = (
        "HDFC BANK LIMITED\n"                       # 1 column
        "Account Holder: RAHUL MEHTA\n"             # 1 column
        "\n"                                         # blank
        "Date,Narration,Withdrawal Amt.,Deposit Amt.,Balance\n"
        "01/03/2026,UPI-X-x@y,100.00,,900.00\n"
        "02/03/2026,SALARY,,5000.00,5900.00\n"
    ).encode("utf-8")
    grid = ingest.read_table(csv_text, "stmt.csv")
    # Preamble kept as rows (normalize finds the header); blank row dropped.
    assert grid[0] == ["HDFC BANK LIMITED"]
    assert ["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Balance"] in grid
    assert grid[-1][0] == "02/03/2026"


def test_sniffs_semicolon_delimiter():
    csv_text = b"Date;Narration;Amount\n01/03/2026;A;100,00\n"
    grid = ingest.read_table(csv_text, "stmt.csv")
    assert grid[0] == ["Date", "Narration", "Amount"]


def test_unsupported_extension_raises():
    with pytest.raises(ingest.IngestError):
        ingest.read_table(b"x", "statement.docx")


def test_empty_file_raises():
    with pytest.raises(ingest.IngestError):
        ingest.read_table(b"\n\n", "stmt.csv")


def test_reads_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Narration", "Amount"])
    ws.append(["01/03/2026", "UPI-X", "100.00"])
    buf = io.BytesIO()
    wb.save(buf)
    grid = ingest.read_table(buf.getvalue(), "stmt.xlsx")
    assert grid[0] == ["Date", "Narration", "Amount"]
    assert grid[1][1] == "UPI-X"


# --- PDF (digital, text-based) ----------------------------------------------

def test_pdf_reads_same_transactions_as_csv():
    """The committed ICICI CSV and PDF hold identical data, so the PDF text-layer
    reconstruction must yield the same mapping and the same transactions as the CSV."""
    pytest.importorskip("pdfplumber")
    from src import mapping, normalize

    def read(fn):
        grid = ingest.read_table(open(f"samples/{fn}", "rb").read(), fn)
        m = mapping.infer(grid, allow_llm=False)
        txns = normalize.apply_mapping(grid, m)
        return m, [(t.date, t.description, t.amount, t.direction) for t in txns]

    m_csv, t_csv = read("sample_icici.csv")
    m_pdf, t_pdf = read("sample_icici.pdf")
    assert m_pdf.scheme == m_csv.scheme == "amount_flag"
    assert len(t_pdf) > 0 and t_pdf == t_csv


def test_pdf_preamble_yields_account_holder():
    """The holder name in the PDF preamble is recovered, so self-transfer detection
    works for PDF uploads exactly as it does for CSVs."""
    pytest.importorskip("pdfplumber")
    from src import normalize
    grid = ingest.read_table(open("samples/sample_icici.pdf", "rb").read(), "sample_icici.pdf")
    assert "rahul mehta" in normalize.extract_account_holder(grid)


def _encrypted_pdf(password: str) -> bytes:
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    if password:
        pdf.set_encryption(owner_password=password + "-owner", user_password=password)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    with pdf.table(first_row_as_headings=True, padding=2) as table:
        table.row(["Date", "Narration", "Amount", "Dr/Cr"])
        table.row(["01-03-2026", "UPI-X-x@y-SUB", "100.00", "DR"])
    return bytes(pdf.output())


def test_password_protected_pdf():
    pytest.importorskip("fpdf")
    data = _encrypted_pdf("s3cret")
    with pytest.raises(ingest.PdfPasswordError):
        ingest.read_table(data, "stmt.pdf")                      # no password
    with pytest.raises(ingest.PdfPasswordError):
        ingest.read_table(data, "stmt.pdf", password="wrong")    # wrong password
    grid = ingest.read_table(data, "stmt.pdf", password="s3cret")
    assert ["Date", "Narration", "Amount", "Dr/Cr"] in grid


def test_scanned_pdf_without_text_rejected():
    fpdf = pytest.importorskip("fpdf")
    pdf = fpdf.FPDF()
    pdf.add_page()                                                # a blank page: no text layer
    data = bytes(pdf.output())
    with pytest.raises(ingest.IngestError):
        ingest.read_table(data, "scan.pdf")
