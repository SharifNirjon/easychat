"""Admin v0 (admin.py, kb_admin.py, team in auth.py): roadmap step 10's "Done when":
the team reads chats, exports leads, edits + publishes the knowledge base."""
import csv
import io
import json

import pytest
from test_auth import APP, outbox  # noqa: F401  (fixture: captured emails)
from test_chat_forms import draft, send_lead  # noqa: F401  (fixture: stub summary draft)
from test_order_tracking import chat

import crawler
import grounding
import jobs
import kb_admin
import limits
import retrieval
from retrieval import Hit

OWNER = "owner@shop-a.example"
MEMBER = "member@shop-a.example"
DIM = 1024


# --- Fixtures -----------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_knowledge(owner_db):
    """Tenant A's knowledge starts and ends empty (other test files index pages for it)."""
    wipe = "DELETE FROM kb_pages WHERE tenant_id = (SELECT id FROM tenants WHERE slug = 'tenant-a')"
    owner_db.execute(wipe)
    yield
    owner_db.execute(wipe)


def tenant_a(owner_db):
    """Tenant A's id."""
    return owner_db.execute("SELECT id FROM tenants WHERE slug = 'tenant-a'").fetchone()[0]


async def sign_in_as(client, outbox, email):  # noqa: F811
    """Sign in with an emailed link (the client keeps the session cookie)."""
    assert (await client.post("/api/auth/start", json={"email": email}, headers=APP)).status_code == 202
    response = await client.post("/api/auth/verify", json={"token": outbox.last_token()}, headers=APP)
    assert response.status_code == 200, response.text


@pytest.fixture
async def team_a(client, outbox, owner_db):  # noqa: F811
    """Tenant A has an owner (signed in on `client`) and takes requests at its owner's address."""
    owner_db.execute("INSERT INTO users (tenant_id, email, role) VALUES (%s, %s, 'owner')", (tenant_a(owner_db), OWNER))
    await sign_in_as(client, outbox, OWNER)
    return client


@pytest.fixture
def vectors(monkeypatch):
    """Embeddings without the network: each text gets a fixed unit vector (so similarity is 1)."""
    async def fake_embed(texts):
        """One identical unit vector per text."""
        return [[0.0] * (DIM - 1) + [1.0] for _ in texts], 7 * len(texts)
    monkeypatch.setattr(kb_admin, "embed_texts", fake_embed)
    monkeypatch.setattr(retrieval, "embed_texts", fake_embed)


async def lead_conversation(client, outbox):  # noqa: F811
    """A visitor (tenant A's widget) asks to be contacted and sends the form with consent."""
    _, cid = await chat(client, "Can someone contact me about 200 mugs?")
    await send_lead(client, cid)
    return cid


# --- Inbox --------------------------------------------------------------------------------------

async def test_the_team_reads_chats_with_their_requests(team_a, outbox):  # noqa: F811
    """Inbox list: newest first, with a preview and the request; the transcript and the consent."""
    _, plain = await chat(team_a, "Do you ship to Sylhet?")
    cid = await lead_conversation(team_a, outbox)
    listing = (await team_a.get("/api/admin/conversations")).json()
    assert [i["id"] for i in listing["items"]] == [cid, plain] and listing["total"] == 2
    assert listing["items"][0]["requests"] == [{"kind": "lead", "status": "new", "name": "Rafiq Hasan", "order": None}]
    only = (await team_a.get("/api/admin/conversations", params={"view": "requests"})).json()["items"]
    assert [i["id"] for i in only] == [cid]
    detail = (await team_a.get(f"/api/admin/conversations/{cid}")).json()
    assert [m["role"] for m in detail["messages"]][:2] == ["user", "assistant"]
    assert detail["requests"][0]["data"]["email"] == "rafiq@meghnabev.example"
    assert detail["requests"][0]["consent"].startswith("I agree to tenant-a Inc storing")


async def test_search_and_resolve(team_a):
    """Search finds words in messages (wildcards are literal); resolved chats move views."""
    _, cid = await chat(team_a, "Is the 50% sale on?")
    await chat(team_a, "Hello there")
    found = (await team_a.get("/api/admin/conversations", params={"q": "50%"})).json()["items"]
    assert [i["id"] for i in found] == [cid]
    assert (await team_a.get("/api/admin/conversations", params={"q": "%"})).json()["total"] == 1
    response = await team_a.put(f"/api/admin/conversations/{cid}/status", json={"status": "resolved"}, headers=APP)
    assert response.status_code == 200
    resolved = (await team_a.get("/api/admin/conversations", params={"view": "resolved"})).json()["items"]
    assert [i["id"] for i in resolved] == [cid]


async def test_another_tenants_chat_is_invisible(team_a, owner_db):
    """A chat of tenant B: not listed, 404 to read, resolve or delete."""
    from conftest import KEY_B, ORIGIN_B
    _, cid = await chat(team_a, "hi from B", key=KEY_B, origin=ORIGIN_B)
    assert (await team_a.get("/api/admin/conversations")).json()["total"] == 0
    assert (await team_a.get(f"/api/admin/conversations/{cid}")).status_code == 404
    assert (await team_a.put(f"/api/admin/conversations/{cid}/status", json={"status": "resolved"},
                             headers=APP)).status_code == 404
    assert (await team_a.delete(f"/api/admin/conversations/{cid}", headers=APP)).status_code == 404
    assert owner_db.execute("SELECT count(*) FROM conversations WHERE id = %s", (cid,)).fetchone()[0] == 1


async def test_deleting_a_chat_is_audited(team_a, outbox, owner_db):  # noqa: F811
    """The team deletes a chat (and its request); the audit trail says who."""
    cid = await lead_conversation(team_a, outbox)
    assert (await team_a.delete(f"/api/admin/conversations/{cid}", headers=APP)).status_code == 204
    assert owner_db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert owner_db.execute("SELECT user_email, action FROM admin_audit").fetchall() == [(OWNER, "conversation.delete")]


async def test_admin_needs_a_session_and_the_apps_origin(client, team_a):
    """Signed out: 401. A state change from another site: 403."""
    async with type(client)(transport=client._transport, base_url=client.base_url) as stranger:
        assert (await stranger.get("/api/admin/conversations")).status_code == 401
    response = await team_a.put("/api/admin/conversations/00000000-0000-0000-0000-000000000000/status",
                                json={"status": "resolved"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# --- Leads --------------------------------------------------------------------------------------

async def test_leads_are_listed_and_exported(team_a, outbox, owner_db):  # noqa: F811
    """Leads list with status changes; the CSV has the lead, and the export is audited."""
    await lead_conversation(team_a, outbox)
    leads = (await team_a.get("/api/admin/requests")).json()
    [lead] = leads["items"]
    assert lead["data"]["name"] == "Rafiq Hasan" and lead["status"] == "new"
    assert (await team_a.put(f"/api/admin/requests/{lead['id']}/status", json={"status": "contacted"},
                             headers=APP)).json() == {"status": "contacted"}
    response = await team_a.get("/api/admin/requests/export.csv")
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:5] == ["received", "type", "status", "name", "email"]
    assert rows[1][1:5] == ["lead", "contacted", "Rafiq Hasan", "rafiq@meghnabev.example"]
    assert owner_db.execute("SELECT action, detail FROM admin_audit").fetchall() == [
        ("requests.export", {"kind": "leads", "rows": 1})]


async def test_exported_cells_cannot_run_as_formulas(team_a, outbox):  # noqa: F811
    """A visitor who types '=HYPERLINK(...)' as their name gets a harmless text cell."""
    _, cid = await chat(team_a, "Can someone contact me?")
    await send_lead(team_a, cid, name="=HYPERLINK(\"https://evil.example\",\"click\")", summary="+1 call me")
    rows = list(csv.reader(io.StringIO((await team_a.get("/api/admin/requests/export.csv")).text)))
    assert rows[1][3].startswith("'=HYPERLINK") and rows[1][5] == "'+1 call me"


# --- Knowledge ----------------------------------------------------------------------------------

async def test_entries_are_drafts_until_published(team_a, owner_db, vectors):
    """Create -> a pending change, not searchable. Publish (queued for the worker) -> indexed as
    an 'entry' page, no change pending, findable by retrieval, without a source link."""
    entry = (await team_a.post("/api/admin/kb/entries", headers=APP, json={
        "section": "Services", "title": "Rush orders", "body": "We can print 200 mugs in 5 working days."})).json()
    assert entry["changed"] and not entry["live"]
    tenant = tenant_a(owner_db)
    assert owner_db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] == 0
    published = (await team_a.post("/api/admin/kb/publish", headers=APP)).json()
    assert published["publish"]["state"] == "queued"
    job = json.loads(await limits.client.lindex(jobs.QUEUE_KEY, 0))
    assert job == {**job, "name": "publish_kb", "payload": {"tenant_id": str(tenant)}}
    await kb_admin.publish(tenant)
    state = (await team_a.get("/api/admin/kb")).json()
    assert state["changes"] == 0 and state["entries"][0]["live"] and state["publish"]["state"] == "done"
    found = await retrieval.retrieve(tenant, "rush orders")
    assert found.hits and found.hits[0].kind == "entry" and "5 working days" in found.hits[0].content
    assert grounding.sources_block(found.hits) == []


async def test_editing_and_deleting_a_live_entry_waits_for_publish(team_a, owner_db, vectors):
    """A live entry keeps answering with its published text until the next Publish."""
    entry = (await team_a.post("/api/admin/kb/entries", headers=APP, json={
        "section": "S", "title": "Hours", "body": "Open 9-5."})).json()
    tenant = tenant_a(owner_db)
    await kb_admin.publish(tenant)
    await team_a.put(f"/api/admin/kb/entries/{entry['id']}", headers=APP,
                     json={"section": "S", "title": "Hours", "body": "Open 8-8."})
    assert "9-5" in owner_db.execute("SELECT content FROM kb_chunks").fetchone()[0]
    await kb_admin.publish(tenant)
    assert "8-8" in owner_db.execute("SELECT content FROM kb_chunks").fetchone()[0]
    assert (await team_a.delete(f"/api/admin/kb/entries/{entry['id']}", headers=APP)).status_code == 204
    assert owner_db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] == 1          # still live
    await kb_admin.publish(tenant)
    assert owner_db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] == 0
    assert (await team_a.get("/api/admin/kb")).json()["entries"] == []


async def test_a_crawl_never_removes_the_teams_entries(team_a, owner_db, vectors):
    """The end-of-crawl cleanup deletes unseen pages, never entries."""
    await team_a.post("/api/admin/kb/entries", headers=APP, json={"section": "S", "title": "T", "body": "B"})
    tenant = tenant_a(owner_db)
    await kb_admin.publish(tenant)
    assert await crawler._remove_unseen(tenant, "00000000-0000-0000-0000-000000000000") == 0
    assert owner_db.execute("SELECT kind FROM kb_pages").fetchall() == [("entry",)]


async def test_hiding_a_crawled_page_drops_it_from_answers(team_a, owner_db):
    """Hide: its chunks go now (and the crawler skips it); show: re-indexed at the next re-sync."""
    tenant = tenant_a(owner_db)
    page_id = owner_db.execute("INSERT INTO kb_pages (tenant_id, url, title, content_hash) VALUES "
                               "(%s, 'https://shop-a.example/old-offer', 'Old offer', 'h') RETURNING id",
                               (tenant,)).fetchone()[0]
    owner_db.execute("INSERT INTO kb_chunks (tenant_id, page_id, ord, content, text_hash, embedding) "
                     "VALUES (%s, %s, 0, '50%% off everything', 'x', %s::vector)",
                     (tenant, page_id, "[" + ",".join(["0"] * (DIM - 1) + ["1"]) + "]"))
    pages = (await team_a.get("/api/admin/kb")).json()["pages"]
    assert pages[0]["chunks"] == 1 and not pages[0]["hidden"]
    assert (await team_a.put(f"/api/admin/kb/pages/{page_id}", json={"hidden": True}, headers=APP)).status_code == 200
    assert owner_db.execute("SELECT count(*) FROM kb_chunks").fetchone()[0] == 0
    assert await crawler._existing(tenant, "https://shop-a.example/old-offer") == ("h", True)
    await team_a.put(f"/api/admin/kb/pages/{page_id}", json={"hidden": False}, headers=APP)
    assert await crawler._existing(tenant, "https://shop-a.example/old-offer") == ("", False)


async def test_the_test_console_uses_published_knowledge(team_a, owner_db, vectors, monkeypatch):
    """Matches from published entries only (drafts aren't searchable), plus a model answer."""
    import admin
    prompts = []

    async def fake_complete(messages, usage, max_tokens):
        """Answer from the prompt."""
        prompts.append(messages[0]["content"])
        return "Rush orders take 5 working days."
    monkeypatch.setattr(admin, "complete", fake_complete)
    await team_a.post("/api/admin/kb/entries", headers=APP, json={
        "section": "S", "title": "Rush orders", "body": "Rush orders take 5 working days."})
    empty = (await team_a.post("/api/admin/kb/ask", json={"question": "How fast are rush orders?"}, headers=APP)).json()
    assert empty["hits"] == []
    await kb_admin.publish(tenant_a(owner_db))
    answer = (await team_a.post("/api/admin/kb/ask", json={"question": "How fast are rush orders?"}, headers=APP)).json()
    assert answer["hits"][0]["title"] == "Rush orders" and answer["hits"][0]["url"] is None
    assert answer["answer"] == "Rush orders take 5 working days." and answer["knows"]
    assert "Written by the team: Rush orders" in prompts[-1]


def test_entries_have_no_source_links():
    """Sources list web pages only."""
    hits = [Hit(1, 1, None, "", "x", "entry:abc", "Rush", 0.9, "entry"),
            Hit(2, 2, None, "", "y", "https://shop.example/faq", "FAQ", 0.8, "page")]
    assert grounding.sources_block(hits) == [{"type": "sources", "items": [{"title": "FAQ",
                                                                             "url": "https://shop.example/faq"}]}]


# --- Team ---------------------------------------------------------------------------------------

async def test_owner_invites_a_member_who_can_read_but_not_manage_the_team(team_a, client, outbox, owner_db):  # noqa: F811
    """Invite: a member row + an emailed sign-in link. The member reads the inbox, can't invite;
    once removed, their session stops working at once."""
    response = await team_a.post("/api/admin/team", json={"email": MEMBER}, headers=APP)
    assert response.status_code == 201, response.text
    to, subject, text = outbox.sent[-1]
    assert to == MEMBER and "invited you to the tenant-a Inc team" in text
    member_id = response.json()["id"]
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=client._transport.app, client=("203.0.113.9", 1)),
                           base_url="http://api.test") as member:
        verify = await member.post("/api/auth/verify", json={"token": outbox.last_token()}, headers=APP)
        assert verify.status_code == 200
        assert (await member.get("/api/onboarding")).json()["role"] == "member"
        assert (await member.get("/api/admin/conversations")).status_code == 200
        assert (await member.post("/api/admin/team", json={"email": "x@shop-a.example"}, headers=APP)).status_code == 403
        assert (await team_a.delete(f"/api/admin/team/{member_id}", headers=APP)).status_code == 204
        assert (await member.get("/api/admin/conversations")).status_code == 401
    actions = [r[0] for r in owner_db.execute("SELECT action FROM admin_audit ORDER BY id")]
    assert actions == ["team.invite", "team.remove"]


async def test_invites_are_checked(team_a, owner_db):
    """An email that already has an account, a bad address, the owner removing themselves: refused."""
    owner_db.execute("INSERT INTO users (tenant_id, email) SELECT id, 'taken@b.example' FROM tenants "
                     "WHERE slug = 'tenant-b'")
    assert (await team_a.post("/api/admin/team", json={"email": "taken@b.example"}, headers=APP)).status_code == 409
    assert (await team_a.post("/api/admin/team", json={"email": "nope"}, headers=APP)).status_code == 422
    team = (await team_a.get("/api/admin/team")).json()
    owner_id = team["members"][0]["id"]
    assert (await team_a.delete(f"/api/admin/team/{owner_id}", headers=APP)).status_code == 404


async def test_the_team_size_is_capped(team_a, owner_db):
    """Free plan: team_members people at most (owner included)."""
    owner_db.execute("UPDATE tenants SET limits = '{\"team_members\": 2}' WHERE slug = 'tenant-a'")
    try:
        assert (await team_a.post("/api/admin/team", json={"email": "one@shop-a.example"}, headers=APP)).status_code == 201
        response = await team_a.post("/api/admin/team", json={"email": "two@shop-a.example"}, headers=APP)
        assert response.status_code == 409 and "2 people" in response.json()["detail"]
    finally:
        owner_db.execute("UPDATE tenants SET limits = '{}' WHERE slug = 'tenant-a'")


def test_the_worker_knows_the_publish_job():
    """The job the admin queues has a handler."""
    import worker
    assert kb_admin.PUBLISH_JOB in worker.HANDLERS
