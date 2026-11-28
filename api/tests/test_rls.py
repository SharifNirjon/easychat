"""Row-level security: the API's own database role can only ever see the current tenant."""
import psycopg
import pytest
from conftest import APP_URL, KEY_A, KEY_B


def tenant_ids(owner_db) -> dict[str, str]:
    """{slug: tenant id as text} for the seeded test tenants."""
    return {slug: str(tid) for slug, tid in owner_db.execute("SELECT slug, id FROM tenants")}


@pytest.fixture
def two_conversations(owner_db):
    """One conversation with one private message per tenant, inserted as owner."""
    ids = tenant_ids(owner_db)
    out = {}
    for slug, key in (("tenant-a", KEY_A), ("tenant-b", KEY_B)):
        site_key_id = owner_db.execute("SELECT id FROM site_keys WHERE public_key = %s", (key,)).fetchone()[0]
        cid = owner_db.execute(
            "INSERT INTO conversations (tenant_id, site_key_id) VALUES (%s, %s) RETURNING id",
            (ids[slug], site_key_id),
        ).fetchone()[0]
        owner_db.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', %s)",
            (ids[slug], cid, f"private to {slug}"),
        )
        out[slug] = cid
    return out


@pytest.fixture
def app_db():
    """A connection as the API's own role (not owner, not superuser)."""
    with psycopg.connect(APP_URL) as conn:   # the API's role: not owner, not superuser
        yield conn


def as_tenant(conn, tenant_id):
    """Scope the current transaction to one tenant, as db.tenant_tx does."""
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


def test_api_role_cannot_bypass_rls(app_db):
    """The API's role is neither superuser nor table owner."""
    row = app_db.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row == (False, False)
    owners = {r[0] for r in app_db.execute(
        "SELECT tableowner FROM pg_tables WHERE tablename IN ('tenants','site_keys','conversations','messages')")}
    assert app_db.execute("SELECT current_user").fetchone()[0] not in owners


def test_no_tenant_set_sees_nothing(app_db, two_conversations):
    """With no tenant set, every table looks empty (fail closed)."""
    for table in ("tenants", "site_keys", "conversations", "messages"):
        assert app_db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_tenant_sees_only_its_own_rows(app_db, owner_db, two_conversations):
    """A tenant sees its own rows only, even when asking by id."""
    ids = tenant_ids(owner_db)
    as_tenant(app_db, ids["tenant-a"])
    contents = [r[0] for r in app_db.execute("SELECT content FROM messages")]
    assert contents == ["private to tenant-a"]
    assert app_db.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
    assert app_db.execute("SELECT slug FROM tenants").fetchall() == [("tenant-a",)]
    # Even asking for B's conversation by id returns nothing.
    assert app_db.execute("SELECT * FROM messages WHERE conversation_id = %s",
                          (two_conversations["tenant-b"],)).fetchall() == []


def test_tenant_cannot_write_rows_for_another_tenant(app_db, owner_db, two_conversations):
    """Inserting rows for another tenant is refused."""
    ids = tenant_ids(owner_db)
    as_tenant(app_db, ids["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):   # WITH CHECK violation
        app_db.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', 'x')",
            (ids["tenant-b"], two_conversations["tenant-b"]),
        )


def test_tenant_cannot_update_another_tenants_rows(app_db, owner_db, two_conversations):
    """Updates silently match nothing across tenants."""
    ids = tenant_ids(owner_db)
    as_tenant(app_db, ids["tenant-a"])
    cur = app_db.execute("UPDATE conversations SET user_turns = 99 WHERE id = %s", (two_conversations["tenant-b"],))
    assert cur.rowcount == 0


def test_api_role_cannot_delete_tenants(app_db, owner_db):
    """The API's role can't delete tenants (or their site keys)."""
    as_tenant(app_db, tenant_ids(owner_db)["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("DELETE FROM tenants")


def test_delete_my_data_only_reaches_the_current_tenant(app_db, owner_db, two_conversations):
    """Since step 9 the API may delete conversations ("delete my data"), but RLS confines even an
    unfiltered DELETE to the current tenant: the other tenant's chat survives."""
    as_tenant(app_db, tenant_ids(owner_db)["tenant-a"])
    assert app_db.execute("DELETE FROM conversations").rowcount == 1
    app_db.commit()
    left = [r[0] for r in owner_db.execute("SELECT content FROM messages")]
    assert left == ["private to tenant-b"]


def test_message_cannot_point_at_another_tenants_conversation(owner_db, two_conversations):
    """The composite FK blocks cross-tenant links, even for the owner."""
    ids = tenant_ids(owner_db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):   # composite FK, even for the owner
        owner_db.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', 'x')",
            (ids["tenant-a"], two_conversations["tenant-b"]),
        )


def test_resolve_site_key_ignores_revoked_keys(app_db, owner_db):
    """A revoked site key stops resolving at once."""
    assert app_db.execute("SELECT slug FROM resolve_site_key(%s)", (KEY_A,)).fetchone() == ("tenant-a",)
    owner_db.execute("UPDATE site_keys SET revoked_at = now() WHERE public_key = %s", (KEY_A,))
    try:
        assert app_db.execute("SELECT * FROM resolve_site_key(%s)", (KEY_A,)).fetchone() is None
    finally:
        owner_db.execute("UPDATE site_keys SET revoked_at = NULL WHERE public_key = %s", (KEY_A,))


# --- users (step 7) -------------------------------------------------------------------------

@pytest.fixture
def two_users(owner_db):
    """One user per test tenant, inserted as owner."""
    ids = tenant_ids(owner_db)
    for slug in ("tenant-a", "tenant-b"):
        owner_db.execute("INSERT INTO users (tenant_id, email) VALUES (%s, %s)", (ids[slug], f"owner@{slug}.example"))
    yield ids
    owner_db.execute("DELETE FROM users WHERE email LIKE 'owner@tenant-%'")


def test_users_are_isolated_per_tenant(app_db, two_users):
    """A tenant sees only its own users; with no tenant set, none."""
    assert app_db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    as_tenant(app_db, two_users["tenant-a"])
    assert [r[0] for r in app_db.execute("SELECT email FROM users")] == ["owner@tenant-a.example"]


def test_resolve_login_returns_only_the_exact_email(app_db, two_users):
    """The pre-tenant login lookup answers for exactly one address, case-insensitively, and nothing else."""
    row = app_db.execute("SELECT tenant_id FROM resolve_login('OWNER@tenant-b.example')").fetchone()
    assert str(row[0]) == two_users["tenant-b"]
    assert app_db.execute("SELECT * FROM resolve_login('%')").fetchone() is None


def test_api_role_cannot_create_owners_or_tenants_directly(app_db, two_users):
    """Accounts (tenant + owner) are only created through create_signup(). Since step 10 the API
    role may add MEMBERS (invites), only to its own tenant, and never an owner."""
    as_tenant(app_db, two_users["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO users (tenant_id, email) VALUES (%s, 'x@y.example')", (two_users["tenant-a"],))
    app_db.rollback()
    as_tenant(app_db, two_users["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO users (tenant_id, email, role) VALUES (%s, 'm@y.example', 'member')",
                       (two_users["tenant-b"],))
    app_db.rollback()
    as_tenant(app_db, two_users["tenant-a"])
    app_db.execute("INSERT INTO users (tenant_id, email, role) VALUES (%s, 'm@y.example', 'member')",
                   (two_users["tenant-a"],))
    assert app_db.execute("DELETE FROM users WHERE role = 'owner'").rowcount == 0     # owners can't be removed
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO tenants (slug, name, config) VALUES ('t-x', 'x', '{}')")


# --- admin (step 10) ----------------------------------------------------------------------------

def test_entries_and_audit_are_isolated_per_tenant(app_db, owner_db):
    """A tenant sees only its own knowledge entries and audit trail; the audit can't be edited."""
    ids = tenant_ids(owner_db)
    owner_db.execute("INSERT INTO kb_entries (tenant_id, section, title, body) VALUES (%s, 'S', 'B only', 'x')",
                     (ids["tenant-b"],))
    owner_db.execute("INSERT INTO admin_audit (tenant_id, user_email, action) VALUES (%s, 'b@b.example', 'x')",
                     (ids["tenant-b"],))
    as_tenant(app_db, ids["tenant-a"])
    for table in ("kb_entries", "admin_audit"):
        assert app_db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert app_db.execute("DELETE FROM kb_entries").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("DELETE FROM admin_audit")


def test_chat_retention_follows_each_tenants_setting(app_db, owner_db, two_conversations):
    """purge_old_conversations(): tenant A keeps chats 7 days, B the default 90. A 30-day-old
    chat goes for A only; the function leaves no tenant set behind."""
    ids = tenant_ids(owner_db)
    owner_db.execute("UPDATE tenants SET config = jsonb_set(config, '{requests}', '{\"chat_retention_days\": 7}') "
                     "WHERE slug = 'tenant-a'")
    owner_db.execute("UPDATE conversations SET last_message_at = now() - interval '30 days'")
    try:
        assert app_db.execute("SELECT purge_old_conversations()").fetchone()[0] == 1
        assert app_db.execute("SELECT current_setting('app.tenant_id', true)").fetchone()[0] in ("", None)
        app_db.commit()
        left = {r[0] for r in owner_db.execute("SELECT tenant_id::text FROM conversations")}
        assert left == {ids["tenant-b"]}
    finally:
        owner_db.execute("UPDATE tenants SET config = config - 'requests' WHERE slug = 'tenant-a'")


# --- order connections (step 8) -------------------------------------------------------------

def test_order_connections_are_isolated_per_tenant(app_db, owner_db):
    """A tenant sees only its own order system; with no tenant set, none; it can't write another's."""
    ids = tenant_ids(owner_db)
    owner_db.execute("INSERT INTO order_connections (tenant_id, kind, endpoint, credentials) "
                     "VALUES (%s, 'api', 'https://orders.tenant-b.example/x', 'sealed')", (ids["tenant-b"],))
    assert app_db.execute("SELECT count(*) FROM order_connections").fetchone()[0] == 0
    as_tenant(app_db, ids["tenant-a"])
    assert app_db.execute("SELECT count(*) FROM order_connections").fetchone()[0] == 0
    assert app_db.execute("DELETE FROM order_connections").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO order_connections (tenant_id, kind, endpoint, credentials) "
                       "VALUES (%s, 'api', 'https://x.example/x', 'x')", (ids["tenant-b"],))


# --- chat-form requests (step 9) ----------------------------------------------------------------

def test_requests_and_webhooks_are_isolated_per_tenant(app_db, owner_db, two_conversations):
    """A tenant sees, changes and deletes only its own requests and webhook; it can't file a
    request for another tenant, or attach one to another tenant's conversation."""
    ids = tenant_ids(owner_db)
    owner_db.execute("INSERT INTO requests (tenant_id, conversation_id, kind, payload, consent_text, expires_at) "
                     "VALUES (%s, %s, 'lead', '{\"email\": \"b@b.example\"}', 'ok', now() + interval '1 day')",
                     (ids["tenant-b"], two_conversations["tenant-b"]))
    owner_db.execute("INSERT INTO request_webhooks (tenant_id, url, secret) VALUES (%s, 'https://b.example/hook', "
                     "'sealed')", (ids["tenant-b"],))
    assert app_db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0          # no tenant set
    as_tenant(app_db, ids["tenant-a"])
    for table in ("requests", "request_webhooks"):
        assert app_db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert app_db.execute(f"DELETE FROM {table}").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("INSERT INTO requests (tenant_id, kind, payload, consent_text, expires_at) "
                       "VALUES (%s, 'lead', '{}', 'ok', now())", (ids["tenant-b"],))


def test_a_request_cannot_point_at_another_tenants_conversation(owner_db, two_conversations):
    """The composite FK keeps a request inside its tenant, even for the owner."""
    ids = tenant_ids(owner_db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        owner_db.execute("INSERT INTO requests (tenant_id, conversation_id, kind, payload, consent_text, expires_at) "
                         "VALUES (%s, %s, 'lead', '{}', 'ok', now())", (ids["tenant-a"], two_conversations["tenant-b"]))


def test_purge_deletes_only_expired_requests(app_db, owner_db):
    """Retention: the purge function (callable by the API's role) removes every tenant's expired
    requests and nothing else."""
    ids = tenant_ids(owner_db)
    for slug, expires in (("tenant-a", "now() - interval '1 minute'"), ("tenant-b", "now() - interval '1 day'"),
                          ("tenant-a", "now() + interval '1 day'")):
        owner_db.execute("INSERT INTO requests (tenant_id, kind, payload, consent_text, expires_at) "
                         f"VALUES (%s, 'lead', '{{}}', 'ok', {expires})", (ids[slug],))
    assert app_db.execute("SELECT purge_expired_requests()").fetchone()[0] == 2
    app_db.commit()
    assert owner_db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
