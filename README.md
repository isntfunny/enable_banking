# Enable Banking (Home Assistant Custom Integration)

Read balances and transactions from any PSD2/Open-Banking-capable bank (e.g.
**C24 Bank**, which has no FinTS/HBCI access) via the
[Enable Banking](https://enablebanking.com/) API. Structured analogously to
[`fints4_hacs`](../fints4/fints4_hacs), just for banks that only expose PSD2
instead of FinTS.

## Why Enable Banking and not GoCardless/finAPI direct?

- **finAPI direct**: requires your own PSD2/AISP license + eIDAS QWAC
  certificate — enterprise-only, not viable for a personal project.
- **GoCardless Bank Account Data**: closed to new signups since 2025.
- **Enable Banking**: free for individuals via a *"restricted production
  application"* — you register an app, whitelist your own account(s) by
  linking them once through their portal, no contract/company needed. See
  https://enablebanking.com/docs/api/linked-accounts/

## Prerequisites (do this once, outside Home Assistant)

1. Sign up at https://enablebanking.com/cp/applications
2. Register a new **Production** application, generate a key pair in the
   browser (downloads a `<app-id>.pem` private key — keep it!) and register
   **your Home Assistant callback URL** as a redirect URL:

   `https://<your-ha-url>/auth/external/callback`

   This must match exactly. The setup form shows you the correct URL for your
   instance, so you can copy it from there. Your Home Assistant needs to be
   reachable from the internet under that address — the bank redirects a
   browser to it.
3. Use **"Activate by linking accounts"** to whitelist your own bank
   account(s) — this switches the app from `Inactive` to `Active
   (restricted)` without needing a signed contract.

## Install (HACS)

1. HACS → **Integrations** → **⋮** → **Custom repositories**
2. Add this repository URL, category **Integration**
3. Install **Enable Banking**
4. Restart Home Assistant
5. Settings → **Devices & services** → **Add integration** → **Enable Banking**

## Manual install

Copy `custom_components/enable_banking/` into your Home Assistant config
folder at:

`<config>/custom_components/enable_banking/`

Then restart Home Assistant.

## Configuration (UI config flow)

You'll need:

- **Application ID** — from the Enable Banking control panel
- **Private key** — paste the full PEM content (not a file path; HA stores
  it in the config entry, same as any other integration credential)
- **Bank name / country** — exactly as returned by `GET /aspsps` (e.g.
  `C24` / `DE` — check via the API if unsure, display names sometimes
  differ from the bank's marketing name)
- **Consent validity** — in days, capped by the bank (usually 180)
- **PSU IP address** — optional public IPv4/IPv6 address to send as
  `Psu-Ip-Address` during startup and manual balance refreshes. You can also
  set or change this later in the integration's **Configure** dialog.

Submitting the form takes you straight to your bank. After you log in and
pick the accounts to share, the bank redirects back into Home Assistant and
the entry is created — nothing to copy and paste.

If your bank aborts with something like `server_error` (some connectors are
flaky, especially ones Enable Banking still marks as beta), the flow shows
you that code and you can simply try again. Where the bank offers several
authentication methods, picking a different one at its login page often
gets you through.

## Renewing the consent

PSD2 caps a consent at 180 days and there is **no refresh mechanism** — a
renewal always means logging in at the bank again. The integration makes
that as painless as it can be:

- `sensor.<bank>_consent_expires` holds the expiry as a timestamp, so you
  can build your own reminder automation on top of it
- 14 days before expiry a repair issue appears under **Settings → System →
  Repairs**
- if the consent lapses before you get to it, the integration raises a
  re-authentication prompt the moment polling fails

In every case the renewal is a single confirmation followed by the bank
login — application ID and private key are not asked for again. Select the
**same accounts** as before, otherwise entities belonging to the accounts
you dropped stop updating.

## Entities per linked account

- `sensor.<account> balance` — primary balance (state), all `balance_type`s
  the bank returned as attributes
- `sensor.<account> pending transactions` — sum of not-yet-booked (`PDNG`)
  transactions
- `event.<account> new transaction` — fires for every newly observed
  transaction (booked or pending), full transaction data in the event
  payload

## Rate limits — background polling vs. manual refresh

Banks limit background account-data access, often to four accesses per
day. The default polling interval is **360 minutes (six hours)**, giving
four scheduled refresh cycles per day. Startup/reloads, multiple endpoints,
and transaction pagination can consume additional quota depending on how
the bank counts requests; this interval cannot guarantee avoiding every
bank's limit.

Each configured integration now has one **Refresh balances** button
(**Kontostände aktualisieren** in German). It fetches balances for all
accounts belonging to that entry and immediately updates their sensors.
Transactions and pending-transaction data are retained. The button remains
available when a background request hits a rate limit.

To enable it for an existing integration:

1. Open **Settings → Devices & services → Enable Banking**.
2. Select **Configure** for the bank entry and enter the **PSU IP address**.
3. Save. The entry reloads, sending that IP on the initial account-details,
   balances and transactions requests. Subsequent button presses send it
   only on the balance requests.

Scheduled background polls omit the header. An empty IP keeps the original
header-free startup behaviour; the button will ask you to configure an IP.
The option can also be changed while an entry is waiting to retry setup.
No new bank login is needed to change it. Keep it up to date if your public
IP changes.

[Enable Banking documents PSU headers](https://enablebanking.com/docs/faq/)
as identifying an end-user actively requesting data, and the address should
be that user's actual public IP. The configured IP is also sent on automatic
Home Assistant restarts; restarting alone does not establish user presence.
Banks may still reject or limit requests carrying the header.

## Re-authentication

Consent needs renewal after the configured validity period (max. ~180 days
for most banks). Home Assistant will show a **Reauthenticate** notification
for this integration when the session expires — click it, log in again at
the bank, done. No need to re-enter application ID / private key.

## Tests

Tests load the integration in Home Assistant and intercept bank traffic;
no bank credentials or real account requests are used.

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/pytest
.venv/bin/ruff check --isolated --select E4,E7,E9,F custom_components tests
```
