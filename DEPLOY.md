# Deploying the hosted app

The app runs on Streamlit Community Cloud (free) with Cloudflare R2 (free tier) as
durable, encrypted storage for real accounts. This is a personal-scale setup for
you and a few trusted people, not a regulated financial service.

There are two shapes you can deploy:

- **Zero-cost demo (no API key).** The public demo works with no key at all:
  visitors log in as a sample profile and explore the two-lens review on the
  pre-baked synthetic data in `demo_cache/`. Nothing calls the API. Real accounts
  can be created, and deterministic detection still runs on an upload; only the
  merchant-naming step (enrichment) is skipped without a key.
- **Full app (API key + R2).** Adds two things: real users get durable, encrypted
  storage, and uploads get merchant names, categories, and explanations from the
  model. You pay the Anthropic API only for enrichment, capped per account per day.

The steps below set up the full app. For the zero-cost demo, skip Part A and leave
the API key unset in Part C.

Everything the app reads is listed in `.env.example`.

---

## Part A: create the Cloudflare R2 bucket

R2 is Cloudflare's S3-compatible object storage. The free tier comfortably covers
this project; the only real cost is the Anthropic API, not storage.

1. Sign in at [dash.cloudflare.com](https://dash.cloudflare.com) (a free account is
   fine). In the sidebar, open **R2**. Enabling R2 may ask for a card even though
   the free tier is free; that is Cloudflare's gate, not a charge for this usage.
2. **Create a bucket.** Give it a name, for example `recurring-charge-auditor`, pick
   a location near you, and create it. This name is your `R2_BUCKET`.
3. **Find your S3 API endpoint.** On the R2 overview (or the bucket's Settings) you
   will see an S3 API address of the form
   `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`. That whole URL is your
   `R2_ENDPOINT_URL`.
4. **Create an API token.** Go to **R2 > Manage R2 API Tokens > Create API token**.
   Choose **Object Read & Write**, scoped to the single bucket you just made (least
   privilege). Create it. Cloudflare shows an **Access Key ID** and a **Secret
   Access Key** once: copy both now.
   - Access Key ID -> `R2_ACCESS_KEY_ID`
   - Secret Access Key -> `R2_SECRET_ACCESS_KEY`

You now have four values: bucket, endpoint URL, access key id, secret access key.

---

## Part B: get an Anthropic API key

1. In the [Anthropic console](https://console.anthropic.com), create an API key.
2. Set a **hard monthly spend limit** on the key. The per-account daily cap bounds
   usage, but a spend limit is the backstop.

This is your `ANTHROPIC_API_KEY`. Skip this part for the zero-cost demo.

---

## Part C: point the deployed app at them (Streamlit secrets)

1. Push the repo to GitHub (already done for `main`).
2. At [share.streamlit.io](https://share.streamlit.io), choose **New app**, pick
   this repo, the `main` branch, and `src/app.py`. (If the app already exists, skip
   to its **Settings**.)
3. Open the app's **Settings > Secrets** and paste this TOML, filling in your
   values:

   ```toml
   ANTHROPIC_API_KEY    = "sk-ant-..."
   R2_BUCKET            = "recurring-charge-auditor"
   R2_ENDPOINT_URL      = "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
   R2_ACCESS_KEY_ID     = "..."
   R2_SECRET_ACCESS_KEY = "..."

   # Invite code for "Use it for real": only people you give it to can create an
   # account or sign in. Strongly recommended on a public deploy.
   REAL_ACCESS_CODE     = "pick-a-shared-code"

   # optional, these are the defaults
   DEMO_LIVE_UPLOADS      = "2"    # live "try your own file" trials per demo session
   REAL_STATEMENTS_PER_DAY = "10"  # statements a real account may process per day
   ```

   Leave `REAL_ACCESS_CODE` blank only if you want anyone to be able to create an
   account. With it set, share the code with your invitees; each of them still
   creates their own password-protected account. Rotate it any time by changing the
   value.

4. Save. Streamlit reboots the app with the new secrets. The app copies these
   secrets into the environment on startup, so the same code works locally from a
   `.env` and on Streamlit Cloud from its secret store.

Secrets live only in Streamlit's secret store (and, for local runs, in a
git-ignored `.env`). Never commit them: the repo is public.

---

## Part D: verify it works

1. Open the app. You should land on the **Explore the demo / Use it for real**
   screen.
2. **Demo:** log in as a sample profile (its password is shown on screen) and
   confirm the two-lens review renders, with subscriptions separated from
   investments. No API call happens.
3. **Real:** choose **Use it for real**, enter the access code, create an account,
   add a bank account, and upload a statement (try `samples/sample_hdfc.csv`).
   Confirm the mapping checkpoint appears, then detection and the review.
4. **Durability check:** reboot the app (or come back later), sign in again, and
   confirm your charges are still there. That proves they are in R2, not on the
   ephemeral Streamlit disk.
5. In the Cloudflare R2 bucket you will see keys under `users/` and `u/<your-id>/`.
   The charge objects are ciphertext: they cannot be read without the account
   password.

---

## What to tell the people you invite

- Their charge list is encrypted with **their** password. If they forget it, it
  **cannot be recovered** (there is no reset in this version).
- Their raw statement is discarded after processing; only the derived charge list
  and their confirmations are stored.
- This is a personal tool shared with people you trust, not a regulated financial
  service. It never gives financial advice; it organizes and explains recurring
  charges.

---

## Running it yourself instead (no cloud)

You do not need R2 to use the app privately. Run it locally (see the README): with
no `R2_*` set, real accounts are stored in a git-ignored, still-encrypted
`local_records/cloud/` folder on your own machine. That is durable and private, but
only reachable from that machine.
