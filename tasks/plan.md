# Implementation Plan

## Architecture

The application uses a single same-origin Python HTTP service and a mobile-first browser client. SQLite is the runnable single-instance database in this environment. Requests resolve a shop from its public slug or authenticated membership; private routes receive `shop_id` from the server-side session, never from the submitted body. A separate Print Agent pairs once, then holds a revocable per-device secret and communicates over authenticated HTTPS polling. Payment and printing are separate state machines connected by server-controlled order eligibility.

## Dependency graph and slices

1. **Foundation:** migration runner, configuration, response/error helpers, setup, shop/user/session tables, scrypt login, CSRF and permission checks.
2. **Customer order:** shop public profile, private upload validation/storage, page estimate/range parser, per-document settings, customer tracking bearer, order creation/idempotency.
3. **Price/payment:** centralized integer-paise quote engine, pricing settings, Razorpay order and callback verification, raw-body webhook verification/deduplication, counter-pay workflow.
4. **Shop operations:** responsive dashboard, order search/filters, private preview/download, manual print, allowed status transitions, queue, audit, notification feed, CSV reports.
5. **Automatic print:** pairing codes, printer inventory, agent heartbeat/claim/download/report APIs, OS spool submission, retry limits, error fallback, owner verification.
6. **Settings/admin/deploy:** configurable shop/pricing/retention/print policy, staff grants, super-admin shop views, retention cleanup, setup/deploy/backup/troubleshooting/hardware validation docs.

## Risk controls

- **Cross-shop access:** every private SQL path includes server-derived shop scope; exercise IDs across two seeded shops in tests.
- **Upload abuse:** strict body/file limits, allowlisted extension plus signature checks, opaque names, private directory, safe conversion failure, per-shop retention.
- **Payment replay/manipulation:** DB uniqueness, amount/shop/order checks, constant-time HMAC comparison, raw webhook bytes, event dedupe, no client-supplied paid state.
- **Duplicate printing:** claim transitions are atomic, a job has unique stable identity, retries are bounded, spooler acceptance is a distinct state.
- **Hardware variability:** capability/status inspection is best-effort; reject unsupported requested options and retain manual fallback.
- **SQLite deployment limits:** ship backup/restore and single-instance guidance; block production multi-instance claims until a PostgreSQL adapter and migration path are implemented.

## Verification checkpoints

- Foundation: migration/setup/auth tests and byte compilation.
- Customer/payment: quote and range unit tests, upload/order/tracking HTTP flow, forged callback/webhook rejection tests.
- Operations: cross-tenant tests, print state transition/idempotency tests, export permissions.
- Agent: pairing expiry/revocation and duplicate claim/report tests; inspect available CUPS without claiming a physical test print.
- Final: full unit/HTTP suite, run instructions, environment sample, dependency-free audit, document external keys and printer validation status.
