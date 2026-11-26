-- 0007: chat forms (roadmap step 9): requests a visitor sends to the shop's team.
--
-- A request (return, contact details for a follow-up, "talk to a person") is stored only after
-- the visitor ticked the consent box; consent_text is the exact wording they agreed to. Each row
-- carries its own expiry (the tenant's retention setting) and purge_expired_requests() deletes
-- it after that. The payload is what the visitor saw in the confirmation card, nothing more.
--
-- Delivery (email to the team, signed webhook) is done by the worker from the row: the job in
-- Redis carries only the request id, never the visitor's details.

CREATE TABLE requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  conversation_id uuid,                     -- NULL once the conversation itself is deleted
  kind            text NOT NULL CHECK (kind IN ('return', 'lead', 'handoff')),
  payload         jsonb NOT NULL CHECK (octet_length(payload::text) <= 20000),
  consent_text    text NOT NULL CHECK (length(consent_text) BETWEEN 1 AND 1000),
  consented_at    timestamptz NOT NULL DEFAULT now(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,
  emailed_at      timestamptz,               -- delivered to the team's inbox
  webhook_sent_at timestamptz,               -- delivered to the tenant's webhook (if it has one)
  webhook_error   text CHECK (length(webhook_error) <= 300),
  -- A request can only point at a conversation of the same tenant.
  FOREIGN KEY (conversation_id, tenant_id) REFERENCES conversations (id, tenant_id)
    ON DELETE SET NULL (conversation_id)
);
CREATE INDEX requests_tenant_created_idx ON requests (tenant_id, created_at DESC);
CREATE INDEX requests_conversation_idx ON requests (conversation_id);
CREATE INDEX requests_expires_idx ON requests (expires_at);

-- Where requests are POSTed (optional): one webhook per tenant. The signing secret is sealed by
-- the API (secretbox.py, bound to the tenant) before it gets here, like order credentials.
CREATE TABLE request_webhooks (
  tenant_id   uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
  url         text NOT NULL CHECK (length(url) BETWEEN 10 AND 500),
  secret      bytea NOT NULL CHECK (length(secret) <= 1024),
  created_at  timestamptz NOT NULL DEFAULT now(),
  checked_at  timestamptz              -- last time a test delivery got a 2xx
);

-- requests is not FORCEd (like tenants and users): purge_expired_requests() below deletes
-- expired rows of every tenant as the owner. The API's role is always subject to the policy.
ALTER TABLE requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON requests
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

ALTER TABLE request_webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_webhooks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON request_webhooks
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- Retention: the only cross-tenant write, and it can only delete rows whose time is up.
CREATE FUNCTION purge_expired_requests() RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  WITH gone AS (DELETE FROM requests WHERE expires_at < now() RETURNING 1)
  SELECT count(*)::integer FROM gone
$$;
REVOKE ALL ON FUNCTION purge_expired_requests() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_expired_requests() TO easychat_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON requests, request_webhooks TO easychat_app;
-- "Delete my data" (step 9): a visitor may delete their own conversation and its messages.
GRANT DELETE ON conversations, messages TO easychat_app;
