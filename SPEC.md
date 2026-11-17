# Spec: Smart Xerox Management System — V1

## Objective

Build the mobile-first QR ordering and shop operations system described in the user-provided project brief (`Pasted text.txt`). Customers can order without an account; shop staff manage orders and settings; platform administrators manage shops. Manual printing and a separately installed local print agent are both first-class workflows. The same order, price, payment state, and audit history must carry through either print path.

## Decisions and assumptions

- The supplied brief is the product contract; this spec records the implementation architecture and acceptance bar.
- This workspace is empty and the adjacent Rent Mate project is unrelated; do not import its code or data.
- Runtime available here is Python 3.13 with the standard library and SQLite; Node, PostgreSQL, and Docker are absent. Use a dependency-free Python HTTP service and SQLite for a runnable single-instance installation. All business records are tenant-scoped. The database module is an explicit seam for a PostgreSQL adapter before multi-instance/higher-concurrency deployment.
- Razorpay is real and credential-gated: no test success is fabricated. Counter payment is available when enabled. Payment state changes only after server-side verification or an authorized staff record.
- Customer tracking uses a random, unguessable bearer secret in addition to the human-readable ticket. Customers never receive private document URLs in public status responses.
- CUPS/Linux printing uses installed OS commands. Windows printing uses a configured local PDF print bridge and must be hardware-validated on the target machine.
- DOC/DOCX conversion is only enabled when LibreOffice is installed; otherwise the upload fails clearly. Page estimates that cannot be verified are flagged for staff review.

## Tech stack

- Python 3.13 standard library: `http.server`, `sqlite3`, `hashlib`, `hmac`, `secrets`, `urllib`, `email.parser`.
- Browser UI: semantic HTML, CSS, and vanilla JavaScript, served same-origin by the Python application.
- Durable development/single-shop deployment database: SQLite with foreign keys, WAL, indexes, and versioned SQL migrations. Private local document storage with per-shop/order ownership enforced by server routes.
- Print Agent: separate Python standard-library process; CUPS on Linux; configurable PDF print executable on Windows.
- No package manager or third-party dependency is required to run the application.

## Commands

- Run: `python3 app.py`
- Initialize a shop/admin: `python3 manage.py setup` (reads secrets/settings from environment and refuses if setup is already complete)
- Print Agent: `python3 print_agent/agent.py`
- Tests: `python3 -m unittest discover -s tests -v`
- Database migration: applied automatically at startup from `migrations/` in order; backup: `python3 manage.py backup --output <path>`.

## Project structure

```text
app.py                 HTTP service and request boundary
manage.py              one-time setup and backup commands
smarth_xerox/          database, auth, pricing, orders, payment, agent, and service modules
migrations/            ordered SQLite schema changes
static/                mobile customer and responsive owner UI
print_agent/           separately configurable local printer worker
tests/                 unit and HTTP workflow tests
docs/                  installation, payment, printing, security, backup, and troubleshooting
data/                  runtime database and private files (gitignored)
```

## Code style

Use typed, small Python functions; validate input once at the HTTP boundary; keep SQL parameterized; pass tenant scope explicitly; keep payment and print state transitions in service functions.

```python
def quote_document(settings: dict, pages: int, copies: int, duplex: bool) -> int:
    unit = settings["bw_duplex_per_sheet"] if duplex else settings["bw_simplex_per_side"]
    billable_units = (pages + 1) // 2 if duplex else pages
    return billable_units * copies * unit
```

Money is stored as integer paise. IDs, ticket references, and tokens are generated with `secrets`; passwords are hashed with `hashlib.scrypt`; signatures use `hmac.compare_digest`.

## Testing strategy

- `unittest` for pricing/page ranges, password/session and signature checks, tenant guards, and state transition/idempotency rules.
- HTTP integration tests against a temporary SQLite database for setup, upload, quote, order creation, tracking isolation, owner order updates, payment callbacks, and agent pairing/claim/report flows.
- Static checks: Python byte compilation. Manual UI/device validation is documented; no physical-printer validation is claimed without hardware.

## Boundaries

- Always: recompute price and print eligibility on the server; enforce shop scope and role permissions; use private documents; make payment and agent events idempotent; record spooler and physical-print states separately; document credential/hardware gates.
- Ask first: adding paid SaaS dependencies, sending customer data to external services, or changing the supplied product scope.
- Never: commit secrets or customer documents; trust browser payment/price/status values; expose documents publicly; silently convert/accept corrupt files; mark a spooler submission as physically printed; remove failed orders.

## Success criteria

- A new owner can initialize one shop, sign in, configure profile/prices, and download its public QR.
- A customer can upload multiple supported documents without registering, set distinct print options, receive a server-calculated quote, select an allowed payment path, and get a ticket plus secure tracking link.
- Owner/staff APIs and UI are permission checked and tenant scoped; the owner can find an order, privately preview/download its documents, use browser printing, record completion/failure, and see reports/audit history.
- Razorpay order creation, callback verification, webhook signature validation, and duplicate-event handling are implemented; online checkout stays unavailable without keys.
- A paired Print Agent discovers local printers, polls only its shop, securely fetches claimed jobs, submits to the OS spooler, reports job IDs/errors, reconnects, and never sets physical completion without owner confirmation. Manual fallback preserves the order.
- Setup, environment variables, production reverse-proxy/TLS, payment, agent installation, retention, backup/restore, and physical-printer validation instructions are present.
- Automated tests cover core security and workflow rules; all available checks pass. External-credential and physical-hardware gates are reported explicitly.

## Out of scope for V1

- Horizontal multi-instance service while SQLite is the active adapter; a PostgreSQL adapter is required before that deployment shape.
- Automated malware scanning unless a scanner is present and configured.
- SMS/WhatsApp/email delivery; in-app notifications are the initial channel.
- Subscription billing and customer accounts; neither is required for a standard print order.

## Open questions

- None block implementation. Razorpay keys, public HTTPS hostname, and a physical printer are deployment inputs, not design blockers.
