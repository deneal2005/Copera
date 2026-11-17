# Capability Map: Smart Xerox Management System

| Module | Responsibility | Depends on |
|---|---|---|
| identity-tenancy | Shop setup, owner/staff sessions, permissions, super-admin scope, tenant isolation | — |
| customer-orders | Public shop page, uploads, per-document print settings, protected tracking | identity-tenancy |
| pricing-payments | Server quotes, payment records, Razorpay orders/signatures/webhooks, counter payment | customer-orders |
| shop-operations | Owner dashboard, order queue, manual print, status updates, reports, notifications | customer-orders, pricing-payments |
| print-agent | Pairing, printer discovery, authenticated polling, secure downloads, spooler submission and fallback | shop-operations |
| settings-deployment | Shop/pricing/retention settings, audit history, backups, deployment and runbooks | all modules |

Build order: identity-tenancy → customer-orders → pricing-payments → shop-operations → print-agent → settings-deployment.

Cross-cutting requirements: application-enforced `shop_id` scope on every private record and query; explicit permission checks; idempotency for order/payment/print transitions; private document storage; append-only audit entries for sensitive actions; owner confirmation remains separate from spooler submission.

## V1 limits

- Discount support is a shop-wide percentage. Customer categories, time windows, bulk tiers, range-specific prices, paper types, and scanning are not implemented.
- Shop profile supports contact details, instructions, availability, and estimated wait time. Logo upload, business hours, and holiday schedules are not implemented.
- Notifications are in-app; SMS, email, and WhatsApp delivery are not configured.
- Super-admin shop management and aggregate analytics are implemented. Subscription billing, platform settings, and a system-health console are not.
- SQLite is single-instance only. Malware scanning and physical printer validation depend on deployment infrastructure and hardware.
