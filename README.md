# Recurring Charge Auditor

**Find every subscription and auto-debit hiding in your bank statements, and tell the money leaks apart from the wealth-building.** Upload a statement from any Indian bank, and the tool detects your recurring charges (UPI AutoPay, e-NACH, card auto-pays, standing instructions), separates genuine subscriptions from SIPs and other investments, flags price hikes and duplicates, and lets you confirm the list in a few minutes.

Reference point: Rocket Money (US) proves the model. This is built for the Indian rails, where the same job is unsolved.

> **Status: Phase 1 MVP, in active development.** The security spine, ingestion and normalization, the deterministic detection engine, and the LLM enrichment layer are built and tested (90 tests passing). The Streamlit review UI, the eval scorer, and the hosted deploy are in progress. A live demo link will be added when it ships.

---

## The problem

People do not know their full list of recurring charges. They are scattered across card auto-pays, UPI AutoPay, e-NACH, net-banking standing instructions, and app-store billing. Five distinct failures follow: **discovery** (you cannot see the whole list), **zombies** (paying for things you no longer use), **silent price creep** (amounts rising unnoticed), **duplication** (the same service paid twice, often on different cards), and **free-trial conversion**. It stays unsolved because the incentives run against you: banks profit from spend, app stores obscure the underlying merchant, and merchants bury the cancel button.

## What it does

The loop is: **upload a statement, confirm how it was read, detect, split into two lenses, confirm each charge.**

- **Reads any bank's tabular statement** (CSV, XLS, XLSX). Column detection is deterministic first, with an LLM fallback for layouts it has never seen, so there is no per-bank code.
- **Confirms the mapping before trusting a number.** A mandatory checkpoint shows the inferred columns, a three-row preview, and the debit/credit counts, so a reversed debit/credit convention (the single most dangerous ingestion error) is caught by you, not discovered later.
- **Detects recurring charges deterministically.** UPI is keyed on the VPA, ACH/e-NACH on descriptor plus amount plus day-of-month, cards on the normalized descriptor. It computes cadence, a confidence tier, price creep, and duplicates.
- **Splits the output into two lenses.** Lens A is subscriptions and bills, reviewed for leaks. Lens B is investments and commitments (SIPs, RDs), tracked for contributions and consistency, never labelled as leaks.
- **Lets you confirm.** Keep or dismiss each charge, correct its category, and tag internal transfers.

## Why it is built this way (the engineering worth reading)

1. **All money math is deterministic; the LLM never touches a number.** Recurrence, cadence, amount, price-change, and duplicate logic live in plain code. The model only decodes a cryptic descriptor into a brand name, assigns a category, and writes a one-line explanation, behind a schema-validated contract that structurally cannot alter a figure the engine computed. A model that invents patterns or answers differently run to run is fatal in a money tool.

2. **UPI is keyed on the VPA, not the display name.** On real data, NPCI changed the AutoPay narration mid-window (`UPI-SPOTIFY...` became `UPI-AUTOPAY-SPOTIFY...`), and the reference number changes every month, but the VPA (`spotify.bdsi@hdfcbank`) stays identical. Keying on the display name split one subscription into six weak singletons; keying on the VPA keeps it one clean subscription.

3. **Aggregators are split by amount and day.** A descriptor like `RAZORPAYSOFTWAREPRIV` hides many unrelated mandates. Grouping by the name alone produces garbage; grouping by descriptor plus amount plus day-of-month separates them cleanly, while a genuine price change on the same mandate is merged back into one charge and flagged.

4. **Investments are a separate lens, contributions only.** On a real statement, most auto-debits were SIPs (roughly thirty thousand rupees a month) versus a few hundred to a couple of thousand rupees of actual subscriptions. A tool that called the SIPs "subscriptions leaking money" would lose the user on the first screen. Lens B shows what was contributed, when, and whether a SIP ran on time or stopped. It never shows value, returns, XIRR, NAV, or units, because those are not in a bank statement.

5. **A privacy firewall runs down the middle.** Personal identifiers (name, address, account number, running balance) are stripped at ingestion, before anything reaches the model or storage. Only the derived charge list and your confirmations are stored, encrypted per user, and the raw statement is discarded after processing. A breach should expose "this person pays for Spotify", not a full transaction history and balances.

## A quick example (from the synthetic demo data)

A six-month statement across two accounts produces a clean split:

- **Lens B, investments:** about thirty-two thousand rupees a month across a Groww SIP, an Axis mutual fund SIP, an Aditya Birla SIP, a clearing-corporation equity buy, and a recurring deposit. Tracked, never flagged as a leak.
- **Lens A, subscriptions:** about eighteen hundred rupees a month across Spotify, Netflix, YouTube Premium, and two mandates billed through Razorpay. Netflix is flagged for a price rise (499 to 649), and Spotify is flagged as a duplicate billed on both accounts.

## Tech stack

- **Python 3.12**, **Streamlit** for the app surface
- **Anthropic Claude** for enrichment only, tiered (a fast model for bulk labelling, a stronger model reserved for hard cases), behind a JSON-schema contract
- **pandas / openpyxl** for tabular ingestion, **jsonschema** for the data contracts
- **cryptography** for per-user AES-GCM encryption (a scrypt-wrapped data key), **boto3** for Cloudflare R2 on the hosted deploy
- **pytest** for the test suite

## Guardrails (settled decisions)

These are non-negotiable, because breaking any one of them silently destroys trust in a money tool: deterministic money math only; UPI keyed on the VPA and ACH on descriptor plus amount plus day; investments never called leaks; a mandatory mapping-confirmation checkpoint; detect over-inclusively by loosening thresholds, never by letting the model guess; strip PII before the model or storage; store only the derived list; multi-account from day one. The full rationale is in [`recurring-charge-auditor-SPEC.md`](recurring-charge-auditor-SPEC.md).

## Testing

A pytest suite of 90 tests covers the parts where consistency matters: the encryption round-trips and per-user isolation, all three debit/credit conventions and PII stripping, VPA and aggregator keying, cadence and confidence tiers, price-creep and duplicate detection, and the numbers-blind enrichment contract. A one-command eval scorer over labelled synthetic statements is the next milestone.

```bash
python -m pytest -q
```

## Privacy

Personal data shared by real users is treated carefully: identifiers are stripped at ingestion, only the derived charge list and confirmations are stored (encrypted per user, so the host holds only ciphertext), and the raw statement is discarded after processing. The public demo runs on synthetic data only.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt

python samples/generate_samples.py     # build synthetic statements
python scripts/build_demo_cache.py     # build the zero-cost demo data
python -m pytest -q                    # run the tests

# the Streamlit app is in progress; once ready:
# streamlit run src/app.py
```

Copy `.env.example` to `.env` and paste your own Anthropic API key to enable enrichment. The key stays server-side and is never committed. With no key, the deterministic detection still runs end to end.

## Roadmap

- **Phase 1 (MVP, current):** tabular upload from any bank, mapping confirmation, deterministic detection, LLM enrichment, the two-lens review UI, per-user encrypted storage, cross-account duplicates, price-creep detection.
- **Phase 1.5:** digital PDF ingestion.
- **Phase 2 (Act):** per-merchant cancellation steps, pre-filled cancel messages, pre-renewal and pre-trial nudges.
- **Phase 3:** email-receipt ingestion for trial-conversion and price-hike signals.
- **Phase 4:** Account Aggregator as the consented, scalable backbone.

## Note

This is a personal project built for learning and portfolio purposes, shared with friends for feedback. It organizes and explains what leaves your account. It is not financial or investment advice.
