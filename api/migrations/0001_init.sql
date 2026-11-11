-- 0001: tenants, site keys, conversations, messages — all tenant-scoped with row-level security.
--
-- Roles:
--   * the migration role (table owner) runs this file;
--   * `easychat_app` is a NOLOGIN group role holding the API's privileges. The API logs in as a
--     member of it that is NOT the owner and NOT superuser, so RLS always applies to it.
-- RLS reads the current tenant from the `app.tenant_id` setting, which db.tenant_tx() sets per
-- transaction with SET LOCAL. If it isn't set, every policy matches nothing (fail closed).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'easychat_app') THEN
    CREATE ROLE easychat_app NOLOGIN;
  END IF;
END $$;

-- The tenant for the current transaction, or NULL if none was set.
CREATE FUNCTION current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE TABLE tenants (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug       text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name       text NOT NULL,
  config     jsonb NOT NULL,                 -- public widget config, validated by TenantConfig
  limits     jsonb NOT NULL DEFAULT '{}',    -- overrides of the default plan caps
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site_keys (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  public_key      text NOT NULL UNIQUE CHECK (public_key ~ '^pk_[A-Za-z0-9_]{8,64}$'),
  allowed_origins text[] NOT NULL CHECK (cardinality(allowed_origins) > 0),
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz
);
CREATE INDEX site_keys_tenant_idx ON site_keys (tenant_id);

CREATE TABLE conversations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- random: also the visitor's handle
  tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  site_key_id     uuid NOT NULL REFERENCES site_keys (id),
  user_turns      integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_message_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, tenant_id)                                        -- target of messages' composite FK
);
CREATE INDEX conversations_tenant_recent_idx ON conversations (tenant_id, last_message_at DESC);

CREATE TABLE messages (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id       uuid NOT NULL,
  conversation_id uuid NOT NULL,
  role            text NOT NULL CHECK (role IN ('user', 'assistant')),
  content         text NOT NULL CHECK (length(content) BETWEEN 1 AND 20000),
  model           text,
  tokens_in       integer,
  tokens_out      integer,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- A message can only belong to a conversation of the same tenant.
  FOREIGN KEY (conversation_id, tenant_id) REFERENCES conversations (id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, id);

-- Row-level security applies to every role except the table owner and superusers.
-- FORCE extends it to the owner as well (defence in depth). tenants and site_keys are not
-- forced because resolve_site_key() below reads them as the owner, before a tenant is known.
ALTER TABLE tenants       ENABLE ROW LEVEL SECURITY;
ALTER TABLE site_keys     ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages      FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenants
  USING (id = current_tenant_id()) WITH CHECK (id = current_tenant_id());
CREATE POLICY tenant_isolation ON site_keys
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON conversations
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON messages
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- The one lookup that has to happen before the tenant is known: public site key -> tenant.
-- SECURITY DEFINER runs it as the owner (bypassing RLS) but it only ever returns the row for
-- the exact, non-revoked key it was given.
CREATE FUNCTION resolve_site_key(p_public_key text)
RETURNS TABLE (
  tenant_id uuid, site_key_id uuid, allowed_origins text[],
  slug text, name text, config jsonb, limits jsonb
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT t.id, k.id, k.allowed_origins, t.slug, t.name, t.config, t.limits
  FROM site_keys k JOIN tenants t ON t.id = k.tenant_id
  WHERE k.public_key = p_public_key AND k.revoked_at IS NULL
$$;

REVOKE ALL ON FUNCTION resolve_site_key(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_site_key(text) TO easychat_app;
GRANT EXECUTE ON FUNCTION current_tenant_id() TO easychat_app;

GRANT USAGE ON SCHEMA public TO easychat_app;
GRANT SELECT ON tenants, site_keys TO easychat_app;
GRANT SELECT, INSERT, UPDATE ON conversations TO easychat_app;
GRANT SELECT, INSERT ON messages TO easychat_app;
