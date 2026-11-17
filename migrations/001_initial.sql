CREATE TABLE shops (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at TEXT NOT NULL
);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    global_role TEXT NOT NULL DEFAULT 'shop_user' CHECK (global_role IN ('shop_user', 'super_admin')),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE shop_members (
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'staff')),
    permissions_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (shop_id, user_id)
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX sessions_expiry_idx ON sessions(expires_at);

CREATE TABLE shop_settings (
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    setting_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (shop_id, setting_key)
);

CREATE TABLE pricing_history (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE drafts (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (shop_id, idempotency_key)
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    ticket TEXT NOT NULL,
    tracking_token_hash TEXT NOT NULL,
    customer_name TEXT,
    customer_phone TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_payment',
    payment_status TEXT NOT NULL DEFAULT 'pending',
    payment_method TEXT NOT NULL,
    subtotal_paise INTEGER NOT NULL CHECK (subtotal_paise >= 0),
    tax_paise INTEGER NOT NULL CHECK (tax_paise >= 0),
    total_paise INTEGER NOT NULL CHECK (total_paise >= 0),
    pricing_snapshot_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (shop_id, ticket),
    UNIQUE (shop_id, idempotency_key)
);
CREATE INDEX orders_shop_status_created_idx ON orders(shop_id, status, created_at DESC);
CREATE INDEX orders_shop_phone_idx ON orders(shop_id, customer_phone);

CREATE TABLE ticket_counters (
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    next_number INTEGER NOT NULL,
    PRIMARY KEY (shop_id, year)
);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    draft_id TEXT REFERENCES drafts(id) ON DELETE CASCADE,
    order_id TEXT REFERENCES orders(id) ON DELETE RESTRICT,
    original_name TEXT NOT NULL,
    storage_name TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    page_count INTEGER,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    settings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX documents_draft_idx ON documents(shop_id, draft_id);
CREATE INDEX documents_order_idx ON documents(shop_id, order_id);

CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL CHECK (provider IN ('razorpay', 'counter')),
    provider_order_id TEXT,
    provider_payment_id TEXT,
    amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, provider_order_id),
    UNIQUE (provider, provider_payment_id),
    UNIQUE (shop_id, idempotency_key)
);
CREATE INDEX payments_order_idx ON payments(shop_id, order_id);

CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    payment_id TEXT NOT NULL REFERENCES payments(id) ON DELETE RESTRICT,
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    provider_refund_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE print_agents (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    version TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    printers_json TEXT NOT NULL DEFAULT '[]',
    active_job_id TEXT,
    last_heartbeat_at TEXT,
    last_error TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE pairing_codes (
    code_hash TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE print_jobs (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    method TEXT NOT NULL CHECK (method IN ('manual', 'automatic')),
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT NOT NULL UNIQUE,
    agent_id TEXT REFERENCES print_agents(id) ON DELETE SET NULL,
    spooler_job_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX print_jobs_queue_idx ON print_jobs(shop_id, status, created_at);

CREATE TABLE print_job_attempts (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE RESTRICT,
    print_job_id TEXT NOT NULL REFERENCES print_jobs(id) ON DELETE RESTRICT,
    agent_id TEXT REFERENCES print_agents(id) ON DELETE SET NULL,
    state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    order_id TEXT REFERENCES orders(id) ON DELETE CASCADE,
    audience TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX notifications_shop_created_idx ON notifications(shop_id, created_at DESC);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    shop_id TEXT REFERENCES shops(id) ON DELETE SET NULL,
    actor_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX audit_shop_created_idx ON audit_log(shop_id, created_at DESC);

CREATE TABLE webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT
);
