# CLAUDE.md: Recurring Charge Auditor

Project context loaded every session. Full detail lives in
`recurring-charge-auditor-SPEC.md`; read it before non-trivial work.

## Mission

A recurring-charge audit tool for the Indian market. Ingest a person's bank data,
find every subscription and auto-debit, separate money leaks from wealth-building
investments, and help them stop the charges they don't want. Reference: Rocket
Money (US). Currently an MVP: a working, password-gated version shared with
friends and colleagues for real feedback, using the builder's own LLM API key.

## Architecture in brief

Three layers. Ingest (manual tabular upload for the MVP), Detect (deterministic
core plus an LLM enrichment layer), Act (later phase). Detection is bank-agnostic:
it runs on a normalized schema and national rails, so all bank-specific variation
is confined to one thin ingestion layer.

## Guardrails: settled decisions, do not silently reverse

- Recurrence, cadence, amount, and price-change math live in deterministic code,
  never in the LLM. The LLM only decodes descriptors into merchant names,
  categorizes, and explains. It never touches numbers.
- Key UPI recurrence on the VPA (the `@` token), not the display name. The
  display name drifts (for example the NPCI `UPI-AUTOPAY-` format change); the
  VPA is stable ground truth.
- Key ACH/e-NACH on descriptor plus amount plus day-of-month. Descriptors like
  `RAZORPAYSOFTWAREPRIV` and `INDIAN CLEARING CORP` are aggregators hiding many
  distinct mandates.
- Never label investments or SIPs as leaks. They are a separate category with
  their own view.
- The investment view shows contributions and consistency only. Never value,
  returns, XIRR, NAV, or units.
- Always run a mapping-confirmation checkpoint before detection: show the inferred
  column mapping, a three-row preview, and debit/credit counts, and let the user
  confirm or correct, especially which direction is money-out.
- Detect over-inclusively by loosening deterministic thresholds, never by letting
  the LLM guess. False positives are dismissed by the user; misses are invisible.
- No limit on which bank. Limit on file format only. Tabular (CSV/XLS/XLSX) is
  Phase 1; PDF is later.
- Strip personal identifiers (name, address, account number, balance) before
  anything reaches the LLM or storage.
- Store only the derived charge list and user confirmations. Discard the raw
  statement after processing. Keep the API key server-side.
- Multi-account from day one in the schema and engine, UI cap of 2 for the MVP.
  Every charge carries its source account.

## Current phase

Phase 1 (MVP). See section 12 of the spec for the module breakdown, non-goals,
success criteria, and the open build decisions (stack, auth, persistence) that
are still to be made.
