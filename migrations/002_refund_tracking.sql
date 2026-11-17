ALTER TABLE refunds ADD COLUMN idempotency_key TEXT;
ALTER TABLE refunds ADD COLUMN reason TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX refunds_idempotency_idx ON refunds(shop_id, idempotency_key);
