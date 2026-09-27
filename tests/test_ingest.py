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
        ingest.read_table(b"x", "statement.pdf")


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
