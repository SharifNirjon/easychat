-- 0005: self-serve onboarding (roadmap step 7): accounts, tenant lifecycle, install detection.
--
-- Sign-in links and sessions live in Redis (they expire by themselves), not here.
-- Plan caps live in code (models.TenantLimits), so there is no `plans` table: every table keeps
-- a tenant_id (CLAUDE.md rule 4).

-- Tenant lifecycle. Existing (seeded) tenants are live; self-serve sign-ups start in onboarding
-- and go live when their widget is first seen on their own website.
ALTER TABLE tenants
  ADD COLUMN plan     text NOT NULL DEFAULT 'free' CHECK (plan IN ('free')),
  ADD COLUMN status   text NOT NULL DEFAULT 'live' CHECK (status IN ('onboarding', 'live', 'suspended')),
  ADD COLUMN site_url text CHECK (length(site_url) <= 2000);      -- the website the owner gave us

-- First time the widget loaded from the tenant's own site (not our preview page).
ALTER TABLE site_keys ADD COLUMN installed_at timestamptz;

-- A crawl can wait in the job queue before the worker starts it.
ALTER TABLE crawl_jobs DROP CONSTRAINT crawl_jobs_status_check;
ALTER TABLE crawl_jobs ADD CONSTRAINT crawl_jobs_status_check
  CHECK (status IN ('queued', 'running', 'done', 'failed'));

-- People who can sign in to the app. One email = one tenant for now (team invites: step 10).
CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  email         text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
  role          text NOT NULL DEFAULT 'owner' CHECK (role IN ('owner')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);
CREATE INDEX users_tenant_idx ON users (tenant_id);

-- Not FORCEd, like tenants: the two SECURITY DEFINER functions below read it as the owner.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON users
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- Sign-in happens before the tenant is known: email -> (user, tenant). Like resolve_site_key(),
-- it bypasses RLS but only ever returns the row for the exact email it is given.
CREATE FUNCTION resolve_login(p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.id, u.tenant_id FROM users u WHERE u.email = lower(p_email)
$$;

-- A new sign-up: create the tenant and its owner in one step (no tenant context exists yet).
-- If the email already has an account (two sign-in links used at once), return that one instead.
CREATE FUNCTION create_signup(p_email text, p_slug text, p_name text, p_config jsonb)
RETURNS TABLE (user_id uuid, tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_tenant uuid;
BEGIN
  RETURN QUERY SELECT u.id, u.tenant_id FROM users u WHERE u.email = lower(p_email);
  IF FOUND THEN
    RETURN;
  END IF;
  BEGIN
    INSERT INTO tenants (slug, name, config, status) VALUES (p_slug, p_name, p_config, 'onboarding')
      RETURNING id INTO v_tenant;
    RETURN QUERY INSERT INTO users (tenant_id, email) VALUES (v_tenant, lower(p_email))
      RETURNING users.id, users.tenant_id;
  EXCEPTION WHEN unique_violation THEN          -- lost a race for this email: undo, use the winner
    RETURN QUERY SELECT u.id, u.tenant_id FROM users u WHERE u.email = lower(p_email);
  END;
END
$$;

REVOKE ALL ON FUNCTION resolve_login(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION create_signup(text, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_login(text) TO easychat_app;
GRANT EXECUTE ON FUNCTION create_signup(text, text, text, jsonb) TO easychat_app;

-- What the onboarding app may change, always inside its own tenant (RLS).
GRANT UPDATE (name, config, status, site_url) ON tenants TO easychat_app;
GRANT INSERT, UPDATE (allowed_origins, installed_at) ON site_keys TO easychat_app;
GRANT SELECT, UPDATE (last_login_at) ON users TO easychat_app;
