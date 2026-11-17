# Smart Xerox Management System

A QR based print ordering and shop operations app. Customers place orders without an account; owners and staff review orders, prices, payments, refunds, and printing from a responsive dashboard. A separate Python Print Agent can submit approved work to a local printer.

## Features

- Shop specific public order page, QR SVG, upload limits, per document print options, server side INR quotes, ticket, and private tracking link.
- Counter payment records and optional Razorpay checkout, callback verification, signed webhook handling, duplicate event protection, and refund records.
- Manual printing and an optional paired Linux/CUPS or Windows/SumatraPDF agent. Spooler acceptance does not mark paper as printed; an owner/staff member confirms completion.
- Shop scoped accounts, staff permissions, activity log, notifications, dashboard, CSV exports, and platform shop administration.
- Automatic cleanup of expired upload drafts and old files for completed, cancelled, or failed orders. Order, payment, refund, and audit records remain in the database.

## Requirements

- Python 3.13 or later. The web application uses only the Python standard library; no `pip install` step is needed.
- Linux printing requires CUPS commands (`lp`, `lpstat`) on the computer connected to the printer. Windows automatic printing requires SumatraPDF on that computer.
- LibreOffice is optional. DOC/DOCX uploads are only enabled when LibreOffice is installed; PDF, JPG, JPEG, and PNG are supported by default.

## Local setup

```sh
cp .env.example .env
python3 manage.py setup
python3 app.py
```

The setup command asks for the shop name, public URL slug, owner details, and password. Open [http://127.0.0.1:8080/admin](http://127.0.0.1:8080/admin) to sign in. Configure the profile and actual prices before sharing the shop link or QR; new shops start with no price table.

Useful commands:

```sh
python3 manage.py create-super-admin
python3 manage.py backup --output ./backups/xerox.sqlite3
python3 -m unittest discover -s tests -v
```

The SQLite database and private files default to `data/xerox.sqlite3` and `data/private-documents`. Keep both private and backed up. `SMX_DATABASE` and `SMX_STORAGE` can point to other paths.

## Docker Compose

Copy `.env.example` to `.env`, set the public URL and any payment credentials, then start the app:

```sh
docker compose up -d --build
docker compose exec smart-xerox python manage.py setup
```

The Compose file binds the app to `127.0.0.1:8080` and stores data in a named volume. Put a TLS reverse proxy in front of it. For a public installation set `SMX_PUBLIC_BASE_URL=https://your-domain`, `SMX_SECURE_COOKIES=true`, and configure the proxy to forward the original host. Keep the app port private to the proxy. The bundled server does not terminate TLS.

To make a backup, stop the service, copy the SQLite database and the entire private document directory or Docker volume, then restart. `manage.py backup` creates a consistent SQLite database backup; it does not copy document files. For restore, stop the service and restore both database and files from the same backup set before restarting.

SQLite is intended for a single application instance on one host. Do not run multiple replicas against a shared SQLite file. A PostgreSQL adapter and deployment migration are not included.

## Razorpay configuration

Set these values in the server environment, then restart:

```dotenv
RAZORPAY_KEY_ID=...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
```

Online checkout stays unavailable until key ID and secret are present. Set the gateway webhook URL to `https://your-domain/api/webhooks/razorpay` and use the same webhook secret in the environment. The service validates the callback signature and checks the provider's captured amount and currency before recording payment. Never put secrets in browser code or commit `.env`.

## Print Agent

The agent runs on the shop computer that can access the printer. Install Python 3.10+ there and copy the `print_agent/agent.py` file from this project. In the owner dashboard, create a one time pairing code and use the public HTTPS URL of the app (HTTP is accepted only for localhost).

Linux with CUPS:

```sh
lpstat -p -d
python3 print_agent/agent.py pair --url https://your-domain --name shop-printer
python3 print_agent/agent.py test
python3 print_agent/agent.py run
```

The test command asks you to type `YES` before submitting one page to the default ready printer. Confirm the page physically came out with the expected orientation, sides, color, and paper size. Run the agent under the same OS account that can access the printer.

Windows PowerShell:

```powershell
$env:SMX_SUMATRA_PATH = 'C:\Program Files\SumatraPDF\SumatraPDF.exe'
py print_agent\agent.py pair --url https://your-domain --name shop-printer
py print_agent\agent.py test
py print_agent\agent.py run
```

Set `SMX_SUMATRA_PATH` in the account's environment before pairing or running. To keep the agent running after sign in, create a Windows Task Scheduler task that starts `py.exe` with arguments `print_agent\agent.py run`, using the printer user's account. Keep the paired config under that account private; revoke the agent in the dashboard if the computer is replaced. The agent stores its token and retry ledger in the user's config directory.

Printer discovery is best effort and reflects the OS spooler, not a guaranteed hardware test. Verify a real test page before enabling automatic printing. Keep manual printing available as a fallback. The service does not claim the physical output was successful until a staff member confirms it.

## Retention, access, and troubleshooting

- Shop retention is configured in days (1–90). Expired drafts and old documents for completed, cancelled, or failed orders are removed when the app starts and once daily. Files belonging to unfinished orders are retained so production work is not stranded. Historical order, payment, refund, and audit rows are kept.
- Owner/staff passwords are stored as scrypt hashes. Private documents are stored outside the public static directory. Customer tracking links contain a secret token; treat them as private.
- If uploads fail, check the HTTP request limit, shop file size/count settings, available disk, and the file type. Word conversion needs LibreOffice installed on the server before enabling DOC/DOCX.
- If a print agent is offline, run `lpstat -p -d` (Linux), confirm the OS user can print, verify the server URL is reachable over HTTPS, and check the agent log in its config directory. A revoked or lost pairing token must be paired again.
- If an order is waiting for page review, enter the verified page counts in the owner dashboard before asking the customer to pay.
- Without Razorpay credentials, counter payment remains the available payment path when the shop allows it.

## Current deployment limits

This V1 supports a shop-wide percentage discount. It does not have customer-category, time-based, bulk-tier, page-range, or paper-type pricing, scanning, shop logo upload, business hours/holiday schedules, SMS/WhatsApp/email delivery, built-in malware scanning, subscriptions, or platform health settings. It also has no multi-instance PostgreSQL adapter. Validate HTTPS, backups, a Razorpay test transaction and webhook, and physical printer behavior in the target installation before accepting live orders.
