"""Row-level security: the API's own database role can only ever see the current tenant."""
import psycopg
import pytest
from conftest import APP_URL, KEY_A, KEY_B


def tenant_ids(owner_db) -> dict[str, str]:
    return {slug: str(tid) for slug, tid in owner_db.execute("SELECT slug, id FROM tenants")}


@pytest.fixture
def two_conversations(owner_db):
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
    with psycopg.connect(APP_URL) as conn:   # the API's role: not owner, not superuser
        yield conn


def as_tenant(conn, tenant_id):
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


def test_api_role_cannot_bypass_rls(app_db):
    row = app_db.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row == (False, False)
    owners = {r[0] for r in app_db.execute(
        "SELECT tableowner FROM pg_tables WHERE tablename IN ('tenants','site_keys','conversations','messages')")}
    assert app_db.execute("SELECT current_user").fetchone()[0] not in owners


def test_no_tenant_set_sees_nothing(app_db, two_conversations):
    for table in ("tenants", "site_keys", "conversations", "messages"):
        assert app_db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_tenant_sees_only_its_own_rows(app_db, owner_db, two_conversations):
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
    ids = tenant_ids(owner_db)
    as_tenant(app_db, ids["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):   # WITH CHECK violation
        app_db.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', 'x')",
            (ids["tenant-b"], two_conversations["tenant-b"]),
        )


def test_tenant_cannot_update_another_tenants_rows(app_db, owner_db, two_conversations):
    ids = tenant_ids(owner_db)
    as_tenant(app_db, ids["tenant-a"])
    cur = app_db.execute("UPDATE conversations SET user_turns = 99 WHERE id = %s", (two_conversations["tenant-b"],))
    assert cur.rowcount == 0


def test_api_role_cannot_delete_or_change_tenants(app_db, owner_db):
    as_tenant(app_db, tenant_ids(owner_db)["tenant-a"])
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_db.execute("DELETE FROM messages")


def test_message_cannot_point_at_another_tenants_conversation(owner_db, two_conversations):
    ids = tenant_ids(owner_db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):   # composite FK, even for the owner
        owner_db.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content) VALUES (%s, %s, 'user', 'x')",
            (ids["tenant-a"], two_conversations["tenant-b"]),
        )


def test_resolve_site_key_ignores_revoked_keys(app_db, owner_db):
    assert app_db.execute("SELECT slug FROM resolve_site_key(%s)", (KEY_A,)).fetchone() == ("tenant-a",)
    owner_db.execute("UPDATE site_keys SET revoked_at = now() WHERE public_key = %s", (KEY_A,))
    try:
        assert app_db.execute("SELECT * FROM resolve_site_key(%s)", (KEY_A,)).fetchone() is None
    finally:
        owner_db.execute("UPDATE site_keys SET revoked_at = NULL WHERE public_key = %s", (KEY_A,))
