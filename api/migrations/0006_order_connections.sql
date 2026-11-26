-- 0006: order tracking (roadmap step 8): how each tenant's orders can be looked up.
--
-- One connection per tenant: a Shopify store or the shop's own order API. The credentials are
-- encrypted by the API before they get here (secretbox.py, AES-256-GCM bound to the tenant),
-- so this table never holds a usable secret, and a database dump doesn't either.
--
-- One-time codes and "this chat may see this order" grants live in Redis (they expire by
-- themselves); order data itself is never stored: it is fetched live from the shop each time.

CREATE TABLE order_connections (
  tenant_id    uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('shopify', 'api')),
  -- Shopify: the store's myshopify.com domain. API: the https URL orders are fetched from.
  endpoint     text NOT NULL CHECK (length(endpoint) BETWEEN 3 AND 500),
  credentials  bytea NOT NULL CHECK (length(credentials) <= 4096),    -- sealed by secretbox.py
  created_at   timestamptz NOT NULL DEFAULT now(),
  checked_at   timestamptz             -- last time a test lookup by the owner worked
);

ALTER TABLE order_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_connections FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON order_connections
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON order_connections TO easychat_app;
