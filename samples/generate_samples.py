"""Generate synthetic Indian bank statements (deterministic; run to (re)build them).

These files are the public demo data and the eval fixtures. They are entirely
fictional, but they deliberately reproduce the hard cases found on real data:

  - UPI AutoPay descriptor drift: 'UPI-SPOTIFY...' becomes 'UPI-AUTOPAY-SPOTIFY...'
    mid-window, while the VPA (spotify.bdsi@hdfcbank) stays identical. Keying on
    the VPA must keep it one subscription.
  - Aggregator collision: several 'RAZORPAYSOFTWAREPRIV' e-NACH mandates at
    different amounts and days that must be separated, not merged.
  - Investments (SIPs, an RD) that must never be called leaks.
  - A price-creep series (Netflix 499 -> 649).
  - A cross-account duplicate: Spotify billed on both the HDFC and ICICI accounts.

Two file shapes exercise two of the three debit/credit conventions: HDFC as
separate Withdrawal/Deposit columns (two_column), ICICI as Amount + a Dr/Cr flag
(amount_flag). A third small signed-column file exercises the signed convention.

Usage:  python samples/generate_samples.py
"""

from __future__ import annotations

import calendar
import csv
import datetime
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = [(2026, m) for m in range(3, 9)]   # Mar..Aug 2026, before "today"
RNG = random.Random(20260924)


def on(year: int, month: int, day: int) -> datetime.date:
    day = min(day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def inr(x: float) -> str:
    return f"{x:,.2f}"


# --- recurring definitions --------------------------------------------------
# Each is (narration_fn(year,month) -> str, amount_fn(idx) -> float, day, direction)

def _hdfc_events() -> list[tuple[datetime.date, str, float, str]]:
    """Return (date, narration, amount, direction) events for the HDFC account."""
    ev: list[tuple[datetime.date, str, float, str]] = []

    for i, (y, m) in enumerate(MONTHS):
        # Salary credit (money-in; not a charge)
        ev.append((on(y, m, 1), "NEFT CR-ACME PAYROLL SERVICES-SALARY-N0293", 120000.00, "credit"))

        # Spotify UPI AutoPay: descriptor drifts after 3 months, VPA is stable.
        if i < 3:
            narr = f"UPI-SPOTIFY INDIA-spotify.bdsi@hdfcbank-HDFC0000001-52831{i}-SUBSCRIPTION"
        else:
            narr = f"UPI-AUTOPAY-SPOTIFY INDIA-spotify.bdsi@hdfcbank-HDFC0000001-71920{i}-MANDATE"
        ev.append((on(y, m, 12), narr, 119.00, "debit"))

        # Netflix e-NACH with a price hike (499 -> 649 from month 3).
        netflix = 499.00 if i < 3 else 649.00
        ev.append((on(y, m, 15), "ACH D- NETFLIX ENTERTAINMENT SERVICES-MANDATE 4471", netflix, "debit"))

        # Razorpay aggregator hiding TWO mandates: different amount + day.
        ev.append((on(y, m, 5), "ACH D- RAZORPAYSOFTWAREPRIV-COLLECT-99120", 149.00, "debit"))
        ev.append((on(y, m, 18), "ACH D- RAZORPAYSOFTWAREPRIV-COLLECT-99120", 599.00, "debit"))

        # Investments (must land in Lens B, never called leaks).
        ev.append((on(y, m, 3), "ACH D- AXIS MUTUAL FUND-SIP-AXISMF7781", 8000.00, "debit"))
        ev.append((on(y, m, 7), "ACH D- INDIAN CLEARING CORP-EQUITY-ICCL221", 5000.00, "debit"))
        ev.append((on(y, m, 10), "UPI-AUTOPAY-GROWW-groww.axis@okicici-GROW0001-SIP", 10000.00, "debit"))
        ev.append((on(y, m, 28), "ACH D- HDFC RECURRING DEPOSIT-RD00219", 3000.00, "debit"))

        # Smallcase grey area: flat monthly, reads like a platform fee (low-conf).
        ev.append((on(y, m, 22), "UPI-AUTOPAY-SMALLCASE-smallcase.pay@yesbank-SMC01-FEE", 123.00, "debit"))

        # --- recurring NOISE: regular, but NOT a subscription. Must be set aside. ---
        # Transfer to the user's own account: carries the holder name, no "self" keyword,
        # so only holder-name matching (not a keyword) can catch it.
        ev.append((on(y, m, 2), "UPI-RAHUL MEHTA-rahul.mehta@okaxis-FUND TRANSFER", 15000.00, "debit"))
        # A tiny recurring charge below the minimum value.
        ev.append((on(y, m, 6), "UPI-AUTOPAY-DAILYHUNT-dailyhunt@ybl-NEWS", 49.00, "debit"))

    # Non-recurring one-offs (dropped as singletons by detection).
    ev.append((on(2026, 4, 9), "UPI-RAHUL SHARMA-rahul.sharma@oksbi-P2P-441", 2500.00, "debit"))
    ev.append((on(2026, 6, 21), "POS 4629-BIGBASKET-GROCERY", 1840.50, "debit"))
    ev.append((on(2026, 7, 2), "ATM WDL-HDFC ATM ANDHERI", 5000.00, "debit"))
    # Irregular railway bookings: same amount, irregular timing -> not recurring.
    for d in (datetime.date(2026, 3, 9), datetime.date(2026, 4, 25), datetime.date(2026, 7, 8)):
        ev.append((d, "UPI-IRCTC-irctc@sbi-TICKET", 500.00, "debit"))
    return ev


def _icici_events() -> list[tuple[datetime.date, str, float, str]]:
    ev = []
    for i, (y, m) in enumerate(MONTHS):
        # Spotify again: SAME VPA as HDFC -> cross-account duplicate.
        ev.append((on(y, m, 14), "UPI-AUTOPAY-SPOTIFY INDIA-spotify.bdsi@hdfcbank-ICIC-88213-MANDATE", 119.00, "debit"))
        # YouTube Premium
        ev.append((on(y, m, 8), "UPI-AUTOPAY-GOOGLE YOUTUBE-google.yt@okaxis-YT4471-SUB", 129.00, "debit"))
        # Birla SIP (investment)
        ev.append((on(y, m, 6), "ACH D- ADITYA BIRLA SUN LIFE MF-SIP-BSL9920", 6000.00, "debit"))
        # Interest credit (money-in)
        ev.append((on(y, m, 30), "CR INTEREST PAID-SAVINGS", RNG.choice([182.0, 201.0, 176.0]), "credit"))
    return ev


# --- writers ----------------------------------------------------------------

def write_hdfc(path: str) -> None:
    """Two-column convention (Withdrawal Amt / Deposit Amt), with preamble + balance."""
    ev = sorted(_hdfc_events(), key=lambda e: e[0])
    balance = 250000.00
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["HDFC BANK LIMITED"])
        w.writerow(["Statement of Account"])
        w.writerow(["Account Holder: RAHUL MEHTA"])           # PII: must be stripped
        w.writerow(["Account Number: 50100XXXXXX1234"])       # PII: must be stripped
        w.writerow(["Period: 01-03-2026 to 31-08-2026"])
        w.writerow([])
        w.writerow(["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"])
        for date, narr, amt, direction in ev:
            if direction == "debit":
                balance -= amt
                w.writerow([date.strftime("%d/%m/%Y"), narr, inr(amt), "", inr(balance)])
            else:
                balance += amt
                w.writerow([date.strftime("%d/%m/%Y"), narr, "", inr(amt), inr(balance)])
        w.writerow(["", "Closing Balance", "", "", inr(balance)])   # footer (no date -> skipped)


def write_icici(path: str) -> None:
    """Amount + Dr/Cr flag convention, with preamble + balance."""
    ev = sorted(_icici_events(), key=lambda e: e[0])
    balance = 90000.00
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ICICI BANK"])
        w.writerow(["Account Holder: RAHUL MEHTA"])           # PII: must be stripped
        w.writerow(["Account Number: 001501XXXXXX9987"])      # PII: must be stripped
        w.writerow([])
        w.writerow(["Txn Date", "Transaction Remarks", "Amount (INR)", "Dr/Cr", "Balance"])
        for date, narr, amt, direction in ev:
            flag = "DR" if direction == "debit" else "CR"
            balance += (-amt if direction == "debit" else amt)
            w.writerow([date.strftime("%d-%m-%Y"), narr, inr(amt), flag, inr(balance)])


def write_signed(path: str) -> None:
    """Single signed-amount convention (negative = money-out). Small file for tests."""
    rows = [
        (on(2026, 3, 11), "UPI-AUTOPAY-APPLE SERVICES-apple.in@hdfcbank-APL-01", -179.00),
        (on(2026, 4, 11), "UPI-AUTOPAY-APPLE SERVICES-apple.in@hdfcbank-APL-02", -179.00),
        (on(2026, 5, 11), "UPI-AUTOPAY-APPLE SERVICES-apple.in@hdfcbank-APL-03", -179.00),
        (on(2026, 3, 2), "SALARY CREDIT-CONTRACT", 45000.00),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Amount"])
        for date, narr, amt in rows:
            w.writerow([date.strftime("%Y-%m-%d"), narr, inr(amt)])


def main() -> None:
    write_hdfc(os.path.join(HERE, "sample_hdfc.csv"))
    write_icici(os.path.join(HERE, "sample_icici.csv"))
    write_signed(os.path.join(HERE, "sample_signed.csv"))
    print("Wrote sample_hdfc.csv, sample_icici.csv, sample_signed.csv to", HERE)


if __name__ == "__main__":
    main()
