# SOYER ACCOUNTING

Installable PWA accounting workspace for SOYER EXCHANGE, backed by PostgreSQL. Telegram bot is used as a notification companion.

## Features
- Password-protected PWA with signed secure session cookie (7-day session).
- Installable PWA for desktop and mobile (service worker + manifest).
- Dashboard, exchange history, expenses, partner shares and payouts.
- Telegram notifications for new PWA operations.
- Two admins by Telegram ID for the Telegram companion bot.
- Manual exchange entry for USDT→UAH and UAH→USDT.
- Gross profit = payout amount - cost basis, in the payout currency.
- Automatic 80% Denis / 20% Egor accrual.
- Expenses, partner payout records, exchange history.
- FastAPI-compatible HTTP endpoint implemented with aiohttp:
  `POST /api/exchanges/success`
- PostgreSQL persistence and idempotency via `external_id`.

## Important accounting convention
For each exchange, `paid` and `cost` must be entered in the same currency: the currency paid to the customer. Gross profit is `paid - cost`. The received amount is recorded for audit/reference. Expenses are tracked separately and do not change the agreed gross-profit split.

## Deploy on Render
1. Create a Telegram bot with @BotFather and copy its token.
2. Create a GitHub repository named `soyer-accounting` and upload all project files.
3. In Render choose **New → Blueprint**, connect the repository and deploy `render.yaml`.
4. In the service Environment set:
   - `BOT_TOKEN`: token from BotFather
   - `ADMIN_IDS`: `DENIS_ID,EGOR_ID`
   - `DENIS_ID`: Denis Telegram numeric ID
   - `EGOR_ID`: Egor Telegram numeric ID
   - `APP_PASSWORD`: shared password for the regular workspace account
   - `ADMIN_PASSWORD`: separate administrator password for Egor
   - `SESSION_SECRET`: long random secret; keep it stable between deploys
   Render creates PostgreSQL and supplies `DATABASE_URL`.
5. Deploy. Open `https://soyer-accounting.onrender.com` and sign in with `APP_PASSWORD`. Send `/start` to the Telegram bot to get the app link and receive operation notifications.

## Getting Telegram IDs
Each admin opens the bot and sends `/id`. Use the returned numeric IDs in Render Environment. Do not publish bot token or API secret.

## Main exchange bot integration
POST JSON to `/api/exchanges/success` with header `X-Soyer-Signature`.
Signature is lowercase hex HMAC-SHA256 of the exact raw JSON body using `API_SECRET`.

Example body:
```json
{
  "external_id": "soyer-order-123",
  "direction": "USDT_UAH",
  "received": "100",
  "paid": "4660",
  "cost": "4620",
  "note": "order 123"
}
```
The `external_id` must be unique. Repeated IDs are rejected by PostgreSQL uniqueness. Only call this endpoint after the exchange is genuinely marked successful.

## PWA installation

Open the service URL in Chrome or Edge and use the install icon in the address bar (or the in-app Install button when offered). On iPhone, use Share → Add to Home Screen. The app requires an internet connection for live PostgreSQL data.

## Limitations of this MVP
- Partner reports currently show UAH accruals; USDT exchange accruals are retained separately in the database and are not yet combined or converted into UAH.
- Payout records are tracked by partner and currency; before recording a payout, reconcile it against the partner's due balance in that currency.
- This is operational bookkeeping software, not a substitute for statutory accounting, tax reporting, AML/KYC checks, or bank reconciliation. Review figures with a qualified accountant.
- Do not use example numbers as real financial data.
