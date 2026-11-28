-- 0008: admin v0 (roadmap step 10): inbox, leads, knowledge editing, team, audit, chat retention.

-- Inbox: the team marks conversations resolved.
ALTER TABLE conversations
  ADD COLUMN status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
  ADD COLUMN resolved_at timestamptz;

-- Leads: where the team is with each request.
ALTER TABLE requests
  ADD COLUMN status text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'closed'));

-- Knowledge: the team's own entries become pages of kind 'entry' (never removed by a crawl);
-- a crawled page can be hidden (its chunks are dropped and the crawler skips it).
ALTER TABLE kb_pages DROP CONSTRAINT kb_pages_kind_check;
ALTER TABLE kb_pages ADD CONSTRAINT kb_pages_kind_check CHECK (kind IN ('page', 'article', 'entry'));
ALTER TABLE kb_pages ADD COLUMN hidden boolean NOT NULL DEFAULT false;

-- An entry has a draft (title, body) and what is live (published_*). Publishing (the worker)
-- indexes the draft and copies it to published_*; deleting a published entry is itself a
-- change that waits for Publish (deleted = true).
CREATE TABLE kb_entries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  section         text NOT NULL CHECK (length(section) BETWEEN 1 AND 60),
  title           text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  body            text NOT NULL CHECK (length(body) BETWEEN 1 AND 8000),
  published_title text CHECK (length(published_title) <= 200),
  published_body  text CHECK (length(published_body) <= 8000),
  deleted         boolean NOT NULL DEFAULT false,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  published_at    timestamptz
);
CREATE INDEX kb_entries_tenant_idx ON kb_entries (tenant_id, section, title);

-- Team: owners invite members (members can do everything except manage the team).
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('owner', 'member'));

-- Who did what that matters for privacy: exports, deletions, publishing, team changes.
CREATE TABLE admin_audit (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
  user_email text NOT NULL CHECK (length(user_email) <= 254),
  action     text NOT NULL CHECK (length(action) <= 60),
  detail     jsonb NOT NULL DEFAULT '{}' CHECK (octet_length(detail::text) <= 2000),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_audit_tenant_idx ON admin_audit (tenant_id, created_at DESC);

ALTER TABLE kb_entries  ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_entries  FORCE ROW LEVEL SECURITY;
ALTER TABLE admin_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON kb_entries
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY tenant_isolation ON admin_audit
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- Chat retention: each tenant's conversations older than its setting (config.requests.
-- chat_retention_days, default 90) are deleted, messages with them. Each DELETE names its tenant
-- explicitly: the function's owner may be a superuser (local docker), which bypasses RLS, so RLS
-- must not be what keeps one tenant's setting off another tenant's rows.
CREATE FUNCTION purge_old_conversations() RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  t record;
  n integer;
  total integer := 0;
BEGIN
  FOR t IN SELECT id, COALESCE((config -> 'requests' ->> 'chat_retention_days')::integer, 90) AS days
           FROM tenants LOOP
    PERFORM set_config('app.tenant_id', t.id::text, true);     -- for RLS, when the owner isn't a superuser
    DELETE FROM conversations
     WHERE tenant_id = t.id AND last_message_at < now() - make_interval(days => t.days);
    GET DIAGNOSTICS n = ROW_COUNT;
    total := total + n;
  END LOOP;
  PERFORM set_config('app.tenant_id', '', true);
  RETURN total;
END
$$;
REVOKE ALL ON FUNCTION purge_old_conversations() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_old_conversations() TO easychat_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON kb_entries TO easychat_app;
GRANT SELECT, INSERT ON admin_audit TO easychat_app;
-- (conversations, requests and kb_pages already have UPDATE from earlier migrations)
GRANT INSERT, DELETE ON users TO easychat_app;
-- The API may only ever add or remove MEMBERS (invites); owners come from create_signup() alone.
CREATE POLICY members_only_insert ON users AS RESTRICTIVE FOR INSERT TO easychat_app
  WITH CHECK (role = 'member');
CREATE POLICY members_only_delete ON users AS RESTRICTIVE FOR DELETE TO easychat_app
  USING (role = 'member');
