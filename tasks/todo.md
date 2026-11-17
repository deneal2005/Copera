# Delivery Checklist

- [x] Migration/config/auth foundation, one-time setup, tenant scope, staff grants, and super-admin shop management.
- [x] Customer shop page, multi-file validation/upload, per-file settings, local PDF/image previews, quote, secure ticket tracking, and page-count review.
- [x] Server pricing in integer paise, independent duplex rates, fees/minimum/tax/percentage discount, counter workflow, and credential-gated Razorpay integration.
- [x] Responsive owner dashboard, order search/filters, manual and agent print workflows, queue controls, notifications, refunds, reports, and shop activity history.
- [x] Paired Print Agent with discovery, authenticated polling, private document fetch, spooler submission, retry ledger, diagnostics, and manual fallback.
- [x] Configurable shop/pricing/retention/printing settings, super-admin routes, backup command, Docker packaging, and operator documentation.
- [x] Unit and HTTP coverage for pricing, signatures/webhooks/refunds, tenant scoping, order lifecycle, print-agent behavior, super-admin access, and document retention.
- [x] Local browser check of first-time owner login, dashboard, and activity view.

## Deployment validation inputs

- Razorpay keys and a configured webhook are required to validate live online checkout/refunds.
- A shop computer and supported printer are required to validate real discovery, settings, spool output, and physical print quality. The agent's test-print command is implemented; no physical printer test is claimed here.
- SQLite supports one application instance on one host. Multi-instance deployment needs a PostgreSQL adapter, which is outside this V1 runtime.
