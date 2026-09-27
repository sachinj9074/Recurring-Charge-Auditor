# Recurring Charge Auditor: Project Spec

Full scope across all phases. Phase 1 is scoped in build-ready detail; later
phases are scoped enough to protect Phase 1 decisions from being undone.
Read `CLAUDE.md` first for the short version and the hard guardrails.

---

## 1. What we are building

A recurring-charge audit tool for the Indian market. It ingests a person's bank
account data, finds every subscription and auto-debit they have, separates money
leaks from wealth-building, and helps them stop the charges they don't want.

Reference points: Rocket Money (US) proves the model. Walnut (India) tried an
SMS-scraping version before the data rails matured.

MVP intent: this is not a full public app on day one. It is a usable, working
version, password-protected, shared with friends, colleagues, and select
LinkedIn connections to get real-world feedback. The builder's own LLM API key
is involved, which is one reason access is gated. If it proves useful, it can
scale from there.

## 2. The problem

Five distinct failures:

1. Discovery. People don't know their full list. Charges are scattered across
   card auto-pays, UPI AutoPay, e-NACH, net-banking standing instructions, and
   app-store billing.
2. Zombies. Paying for services no longer used.
3. Silent price creep. Amounts rise without the person noticing.
4. Duplication. Same service paid twice, often across different cards or accounts.
5. Free-trial conversion. Trials converting to paid without a decision.

It stays unsolved because incentives are against the user: banks profit from
spend, app stores obscure the underlying merchant, and merchants bury the cancel
button.

## 3. Architecture: three layers

1. Ingest. This is a data-access problem, not an AI problem. MVP uses manual
   statement upload as the spine. Email is a later phase. SMS and Account
   Aggregator are out of MVP scope (see Phase Plan for why and when).
2. Detect. Deterministic code owns all money-sensitive logic: periodicity,
   amount, price-change, duplicates. The LLM owns only the fuzzy work: decoding
   cryptic descriptors into merchant identity, categorizing, and writing
   plain-language explanations. The LLM never does recurrence or amount math.
3. Act. Where willingness-to-pay lives, and where nobody solves it well.
   Per-merchant cancellation steps, pre-filled cancel messages, timed pre-renewal
   and pre-trial nudges. Fully automated cancellation is deliberately skipped
   early. This is a later phase.

## 4. Non-negotiable principles (guardrails)

These are settled decisions. They exist because breaking any one of them
silently destroys trust in a money tool. Do not "improve" the design by
reversing them without an explicit decision.

- Recurrence, cadence, amount, and price-change math live in deterministic code.
  Never in the LLM. Models invent patterns and answer differently run to run,
  which is fatal here.
- Investments and SIPs are never labelled as leaks or as money to cut. They are
  a separate category with their own view.
- The investment view shows contributions and consistency only. Never current
  value, returns, XIRR, NAV, or units. Those are not in a bank statement and
  require investment-side data. Walled off to a later phase or never.
- A mapping-confirmation checkpoint always runs before detection on any
  statement. The user confirms how columns were read, including which direction
  is money-out, before any number is trusted.
- Detect over-inclusively. Prefer surfacing a few false positives the user can
  dismiss over missing a real charge. Achieve this by loosening deterministic
  thresholds, not by letting the LLM guess.
- Strip personal identifiers before anything reaches the LLM or storage. The
  engine needs date, description, amount, direction, and which account. It does
  not need name, address, account number, or running balance.
- Store only the derived charge list plus user confirmations. Discard the raw
  statement after processing. A breach should expose "this person pays for
  Spotify", not their full transaction history and balances.

## 5. Findings from real-data validation

We ran the deterministic-plus-LLM approach on a real 6-month HDFC statement
(723 debits). These findings are engineering constraints, not trivia.

- Aggregator collision. Descriptors like `RAZORPAYSOFTWAREPRIV` and
  `INDIAN CLEARING CORP` look like one merchant but each hides five or more
  unrelated mandates at different amounts and on different days. Grouping by the
  descriptor name alone produces garbage. Grouping by descriptor plus amount plus
  day-of-month separates them cleanly.
- Descriptor drift. NPCI changed the UPI AutoPay narration format mid-window:
  `UPI-SPOTIFY...` became `UPI-AUTOPAY-SPOTIFY...`. A parser keyed on the
  human-readable name split every AutoPay subscription into two weaker-looking
  halves. The VPA (for example `spotify.bdsi@hdfcbank`) stays identical across
  the drift. So the VPA is the stable ground-truth merchant identity for UPI,
  and the display name is not. Key UPI recurrence on the VPA.
- Debit/credit convention is the most dangerous ingestion ambiguity. Indian
  statements use at least three: separate Withdrawal and Deposit columns, a
  single Amount column with a Dr/Cr flag, and a single signed column. Reading it
  wrong books deposits as expenses or drops debits entirely, and every number
  downstream is wrong while looking perfectly clean. This is why the
  mapping-confirmation checkpoint is non-negotiable.
- Category reality. On a real statement, most auto-debits were investments
  (SIPs across Axis MF, Birla MF, Groww, clearing-corp equity, an RD), roughly
  ₹26,000 a month, versus about ₹270 to ₹390 a month of genuine consumer
  subscriptions. A naive tool that called the ₹26,000 "subscriptions leaking
  money" would lose the user on the first screen. This is why the two-lens split
  is a Phase 1 requirement, not a nicety.

## 6. Detection engine (deterministic core)

- Merchant key:
  - UPI: the VPA (the token containing `@`), lower-cased. Not the display name.
  - ACH / e-NACH: the merchant descriptor, combined with amount and day-of-month
    to separate mandates that share an aggregator name.
  - Card / POS: normalized descriptor.
- Recurrence: group transactions by merchant key and amount bucket. Compute
  median gap between hits, number of distinct calendar months covered, and amount
  stability.
- Cadence classes: weekly, fortnightly, monthly, bi-monthly, quarterly, annual,
  plus a catch-all for irregular.
- Confidence tiers:
  - HIGH: monthly cadence, present in three or more distinct months, stable
    amount.
  - MEDIUM: monthly across two months, or a clean non-monthly cadence across
    three or more.
  - LOW: everything else that still repeats.
- Price-creep flag: same merchant key, stable cadence, changed amount across the
  window. Statement-derivable, so it is a Phase 1 deterministic feature.
- Duplicate flag: same normalized merchant across multiple accounts, or multiple
  mandates to the same merchant on one account.
- Internal-transfer awareness: transfers between the user's own accounts are not
  expenses. They rarely look recurring, but the review UI must let a user tag a
  charge as an internal transfer if one slips through.

## 7. Ingestion and normalization

The detection engine is bank-agnostic by construction. It operates on a
normalized schema and on national rails (UPI VPAs, ACH descriptors), which read
identically regardless of which bank issued the statement. All bank variation
lives in one thin layer: getting from a specific file to the normalized schema.

- No limit on which bank. A friend on ICICI, Axis, or anything else must be able
  to use it. The limit is on file format, never on the bank.
- Generic normalization layer:
  - Deterministic first: detect columns by header keywords (Date,
    Narration/Description, Withdrawal/Debit, Deposit/Credit, Balance). This alone
    handles most clean bank exports.
  - LLM fallback only when headers are missing or ambiguous: give the model a
    sample of rows and ask which column is date, description, debit, credit. This
    is what makes the system open to banks we have never seen, with zero per-bank
    code.
- Handle all three debit/credit conventions explicitly.
- Mapping-confirmation checkpoint before detection runs: show the inferred
  mapping, a three-row preview, and the debit/credit counts. One tap to confirm,
  or correct it. Non-negotiable.
- Strip personal identifiers at this layer, before anything is stored or sent to
  the LLM.

Format scope, stated precisely. "Format" does not mean CSV versus XLS; those are
interchangeable. The real cliff is tabular versus PDF.

- Tabular (CSV, XLS, XLSX) from any bank: Phase 1.
- Digital PDF: harder, because layout is positional not structural and lines
  wrap. Phase 1.5.
- Scanned or image PDF: needs OCR, and OCR errors on amounts amplify the silent
  miscompute risk. Its own hardening track, not Phase 1.

## 8. Two-lens output

One detection engine, two lenses on its output.

- Lens A, Subscriptions and bills: reviewed for leaks. The Rocket Money job.
- Lens B, Investments and commitments: tracked for completeness and consistency.

Why Lens B belongs here: the bank debit is the only source of truth that sees
across every platform at once. Platform apps only aggregate what runs through
them. A person's SIPs originate in five or six different places but all land as
debits on one account, so the statement sees them all. That is a genuine edge no
single aggregator has.

The hard line for Lens B: contributions versus performance. It can honestly show
what was contributed, when, how much, total deployed over the window, whether a
SIP ran on time or silently failed, and whether one stopped. It cannot show
current value or returns. If Lens B drifts toward "how is my portfolio doing", it
overpromises. Framing is deliberately: single source of truth for what leaves the
account, not a portfolio tracker.

Completeness is bounded by ingestion: "every SIP from the accounts you added",
never "every SIP you have". State this honestly in the UI. It is also the
argument for multi-account.

Grey areas are resolved by the confirmation loop. Example from real data: a flat
₹123 every month to a smallcase VPA reads more like a platform subscription fee
than a variable SIP. The engine tags it low-confidence and lets the user correct
the category.

## 9. Multi-account

Cost scales with the number of distinct statement formats, not the number of
accounts. Two accounts at the same bank is nearly free. Two banks means a second
trip through the generic normalization layer, which the design already handles.

- Build the data model and engine multi-account from day one. Every account is
  its own entity. Every transaction and every detected charge carries which
  account it came from. Detection runs per account. Retrofitting a single-account
  schema later is painful and touches everything.
- UI cap of 2 accounts for the MVP. This is a product limit, not an
  architectural one.
- Detection logic is unchanged by multi-account. A mandate is tied to one
  account and never hops, so recurrence math stays per account. No cross-account
  recurrence logic is needed.
- Bonus unlocked by multi-account: cross-account duplicate detection, the same
  service paid on two different cards. Because merchant identity is already
  normalized on the VPA, this is a thin post-detection merge, not new machinery.
  It directly addresses the duplication failure.
- Risk: two accounts per user is more financial data per head, so the honeypot
  grows per user. Hold the store-only-the-derived-list line firmly.

## 10. Data safety and privacy

This carries real weight because real people upload real statements.

- Minimize what reaches the LLM: descriptor, amount, date, direction, account
  reference only.
- Minimize what is stored: the derived charge list and user confirmations.
  Discard the raw statement after processing.
- Per-user authentication and isolated, encrypted storage.
- Password-gated access for the shared MVP.
- Data minimization is the highest-leverage safety move and is a Phase 1 design
  decision, not later hardening.

## 11. Phase plan

How the five failures map to phases (a completeness check):

- Discovery: Phase 1 core.
- Zombies: surfaced in Phase 1; actually killed in Phase 2 (Act).
- Price creep: Phase 1, statement-derivable.
- Duplication: Phase 1, same-account and cross-account.
- Free-trial conversion: Phase 3, needs the email signal. A weak statement-only
  heuristic is possible earlier but is not reliable.

Phases:

- Phase 1, MVP. Manual tabular upload from any bank, generic normalization with
  mapping confirmation, deterministic recurrence engine, LLM enrichment layer,
  two-lens output, confidence-tiered review and confirmation UI, per-user auth,
  minimal encrypted storage of the derived list, multi-account schema with a UI
  cap of 2, cross-account and same-account duplicate detection, price-creep
  detection. Deliverable: a usable, shareable, password-protected tool that
  produces a confirmed, categorized recurring-charge list per user. Detailed
  below.
- Phase 1.5. Digital PDF ingestion.
- Phase 2, Act. Per-merchant cancellation steps, pre-filled cancel messages,
  timed pre-renewal and pre-trial nudges. Assisted, not automated. This is the
  value and willingness-to-pay layer.
- Phase 3, Email ingestion. OAuth-based email-receipt parsing as a second data
  source. Adds merchant context, trial confirmations, price-hike notices, and
  renewal warnings. Strengthens price-creep and unlocks reliable trial-conversion
  detection. Brings a larger data-safety surface, hence its own phase.
- Phase 4, Account Aggregator. The consented, scalable, durable backbone.
  Requires FIU status or a regulated partner. Unlocks completeness, near
  real-time data, and the investment schema that could power a performance view.
  The "if it blows up" scaling path.
- Later or optional. Scanned-PDF OCR, embedded form-factor and partner
  distribution, automated cancellation.

## 12. Phase 1 detailed scope

Build modules:

1. Auth and user isolation. Per-user login, password-gated. Each user's data
   isolated from every other user's.
2. Ingestion and normalization. Accept CSV, XLS, XLSX. Generic column detection
   (deterministic keywords first, LLM fallback for unknown layouts). Handle all
   three debit/credit conventions. Mapping-confirmation checkpoint. PII stripping
   at ingestion. Attach each upload to an account entity, UI cap of 2.
3. Detection engine (deterministic). Merchant keying (UPI on VPA, ACH on
   descriptor plus amount plus day). Recurrence grouping and cadence
   classification. Confidence tiers. Price-creep flag. Duplicate flag.
   Internal-transfer awareness.
4. LLM enrichment layer (fuzzy, no money math). Map VPA or descriptor to a real
   brand name. Assign category: subscription/bill, investment/commitment,
   personal P2P, or vendor noise. Write a plain-language explanation per charge.
   Runs on minimized fields only. Returns a structured contract so it cannot
   inject or alter numbers.
5. Two-lens output and review UI. Lens A subscriptions, Lens B investments.
   Tiered display, high-confidence up top, low-confidence tucked into a secondary
   section. Confirmation loop: confirm or deny each charge, correct its category,
   tag internal transfers. Deny-to-dismiss. Lens B shows amount, date, cadence,
   total deployed in the window, and ran-on-time or stopped flags.
6. Storage (minimized). Persist the derived charge list, user confirmations, and
   account metadata. Discard the raw statement after processing. Encrypted at
   rest, per-user isolation.

Phase 1 non-goals (explicitly out):

- Email, SMS, or Account Aggregator ingestion.
- Automated cancellation.
- PDF of any kind (that is Phase 1.5).
- Investment performance, returns, or valuation.
- Real-time data.
- Native mobile.

Phase 1 success criteria:

On a real statement from a bank we have not seen before, the tool produces a
correctly-mapped, categorized, confidence-tiered recurring-charge list, with
subscriptions cleanly separated from investments, that a friend can review and
confirm in a few minutes, while storing only the derived list.

Open decisions for the build session (not yet settled):

- Tech stack. Left open deliberately. Constraints to honor: the builder's LLM
  API key must stay server-side and never be exposed to the browser; a real
  datastore with per-user isolated rows; simple enough to deploy and share
  behind a password. Pick the stack against these constraints and against
  whatever is installable and allowed in the build environment.
- Auth mechanism for the shared MVP (managed auth service versus a minimal
  self-rolled gate). Decide against the privacy and effort trade-off.
- Where the mapping-confirmation state and user confirmations are persisted.

## 13. Risk register

- Data access is regulated and some rails are closing (SMS deprecating, AA
  gated). Deferred by going manual-first.
- Financial-data honeypot. Mitigated by minimization: strip before LLM, store
  only the derived list.
- Discovery without action is low-value. The Act layer (Phase 2) is what makes
  the tool worth paying for.
- Retention paradox. A tool that fixes the problem once has weak recurring
  engagement. A reason the ongoing monitoring and nudges in later phases matter.
- Accuracy errors burn trust fast. Countered by deterministic money math, the
  mapping-confirmation checkpoint, and confidence tiers.
- Silent debit/credit mapping errors. The single most dangerous accuracy failure.
  Countered by the mandatory mapping checkpoint.
- Honeypot grows with multi-account. Hold the minimization line.

## 14. Form factor (later thinking, captured)

The best long-term home for this may be embedded inside an existing trusted
financial app rather than a standalone product, because that solves both
distribution and the data-access question at once. The MVP is a standalone
password-gated web app purely to gather feedback. Keep this open, do not build
toward standalone-forever assumptions.
