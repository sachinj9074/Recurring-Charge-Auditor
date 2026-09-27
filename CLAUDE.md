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
- Detect for recall, present for precision. The detector stays over-inclusive, but
  the default view is a short, confident shortlist of genuine subscriptions and
  bills. A charge qualifies only as a genuine recurring pattern: a steady amount
  (or a flagged price rise) AND steady timing for its cadence (monthly on ~the same
  day-of-month for a user-set minimum number of months, weekly on the same weekday
  for 8+ weeks, daily for 28+ days, or a near-uniform gap for the longer cadences).
  Two demotion tiers, and nothing is ever deleted (the user can promote from either):
    - Set aside: a short list of real patterns held back for a stated reason: below
      the user-set minimum value (default 100), a transfer between the user's own
      accounts (matched on the account-holder's own name), personal/P2P, or too
      little history yet (emerging).
    - Ignored: a collapsed, de-emphasized pile of spend with no steady amount or
      schedule (random daily food, cabs, cash/ATM, one-off vendor payments). It is
      kept out of the way so it does not crowd the review, not hard-deleted.
- No limit on which bank. Limit on file format only. Tabular (CSV/XLS/XLSX) is
  Phase 1; PDF is later.
- Strip personal identifiers (name, address, account number, balance) before
  anything reaches the LLM or storage.
- Store only the derived charge list and user confirmations. Discard the raw
  statement after processing. Keep the API key server-side.
- Multi-account from day one in the schema and engine, UI cap of 2 for the MVP.
  Every charge carries its source account.

## Current phase

Phase 1 (MVP), built. The open build decisions are settled: Streamlit (Python
3.12); Anthropic Claude for enrichment only; self-rolled per-user auth (PBKDF2)
with AES-GCM per-user encryption; storage on Cloudflare R2 when hosted, or a
git-ignored encrypted local folder otherwise. The pipeline (ingestion,
deterministic detection, the distillation funnel, enrichment, and the review UI), a
full pytest suite, and a strict eval scorer are in place. Remaining: the
subscription-first UI polish and personalization (M7), then hosted deploy (see
`DEPLOY.md`) and publishing. See section 12 of the spec for the module breakdown,
non-goals, and success criteria.
