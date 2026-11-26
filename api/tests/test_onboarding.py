"""Self-serve onboarding (onboarding.py, worker.crawl_job): website -> branding -> site key ->
queued crawl -> install detection. A fake website stands in for the network."""
import httpx
import pytest
from test_auth import APP, Outbox, sign_in
from test_knowledge import embedder  # noqa: F401  (fixture: deterministic fake embeddings)

import crawler
import mailer
import onboarding
import worker
from branding import WHITE_TEXT_MIN_CONTRAST, contrast_with_white
from config import settings
from fetcher import FetchError, FetchResult, check_url

SITE = "https://green-leaf.example"
HOMEPAGE = """<html><head><title>Green Leaf | Plants</title>
<meta property="og:site_name" content="Green Leaf"><meta name="theme-color" content="#f4f4f4"></head>
<body><main><h1>Green Leaf</h1><p>Indoor plants delivered in Dhaka. Free delivery over $40.</p>
<a href="/shipping">Shipping</a> <a href="/privacy">Privacy</a> <a href="/contact">Contact</a></main></body></html>"""
SHIPPING = "<html><head><title>Shipping</title></head><body><main><p>Delivery takes 2 days.</p></main></body></html>"
SITEMAP_XML = f"<urlset><url><loc>{SITE}/</loc></url><url><loc>{SITE}/shipping</loc></url></urlset>"


class FakeWebsite:
    """Stands in for fetcher.Fetcher (onboarding and crawler): same URL rules, pages from `pages`."""
    pages: dict[str, tuple[str, str]] = {}

    def __init__(self, allowed_hosts, allow_local=False):
        """Same constructor as fetcher.Fetcher."""
        self.allowed_hosts, self.allow_local = allowed_hosts, allow_local

    async def __aenter__(self):
        """Nothing to open."""
        return self

    async def __aexit__(self, *exc):
        """Nothing to close."""

    async def get(self, url, accept, max_bytes):
        """Serve a page after the real URL checks; 404 for anything unknown."""
        url = check_url(url, self.allowed_hosts, self.allow_local)
        if url not in self.pages:
            raise FetchError("HTTP 404", status=404)
        content_type, body = self.pages[url]
        if content_type not in accept:
            raise FetchError("unexpected content type", blocked=True)
        return FetchResult(url, 200, content_type, "utf-8", body.encode(), {})

    async def allowed_by_robots(self, url):
        """Everything may be crawled."""
        return True

    async def robots_sitemaps(self, url):
        """`Sitemap:` lines of the fake robots.txt."""
        robots = self.pages.get(f"{SITE}/robots.txt", ("", ""))[1]
        return [line.split(":", 1)[1].strip() for line in robots.splitlines() if line.lower().startswith("sitemap:")]


class FakeQueue:
    """Stands in for the Redis job queue: records what was enqueued."""

    def __init__(self):
        """No jobs yet."""
        self.jobs: list[tuple[str, dict]] = []

    async def enqueue(self, name, payload):
        """Record the job."""
        self.jobs.append((name, payload))
        return f"job-{len(self.jobs)}"


@pytest.fixture
def website(monkeypatch):
    """The fake Green Leaf website, used by onboarding and the crawler."""
    monkeypatch.setattr(FakeWebsite, "pages", {
        f"{SITE}/": ("text/html", HOMEPAGE),
        f"{SITE}/shipping": ("text/html", SHIPPING),
        f"{SITE}/robots.txt": ("text/plain", f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n"),
        f"{SITE}/sitemap.xml": ("application/xml", SITEMAP_XML),
    })
    monkeypatch.setattr(onboarding, "Fetcher", FakeWebsite)
    monkeypatch.setattr(crawler, "Fetcher", FakeWebsite)
    return FakeWebsite.pages


@pytest.fixture
def jobs(monkeypatch):
    """Capture queued jobs instead of using Redis."""
    fake = FakeQueue()
    monkeypatch.setattr(onboarding, "queue", fake)
    return fake


@pytest.fixture
async def owner(client, monkeypatch, website, jobs):
    """A signed-in new owner (the client carries the session cookie)."""
    box = Outbox()
    monkeypatch.setattr(mailer, "send_email", box)
    await sign_in(client, box)
    return client


async def add_site(client, url: str = SITE) -> httpx.Response:
    """The website step."""
    return await client.post("/api/onboarding/site", json={"url": url}, headers=APP)


async def test_new_owner_starts_with_no_site(owner):
    """Right after sign-up: onboarding, free plan, no site, no key."""
    state = (await owner.get("/api/onboarding")).json()
    assert (state["status"], state["plan"], state["site_url"], state["site_key"]) == ("onboarding", "free", None, None)
    assert state["limits"] == {"max_pages": 50, "daily_crawls": 3, "crawls_left_today": 3}


async def test_website_step_brands_keys_and_queues_a_crawl(owner, jobs, owner_db):
    """One URL in: suggested branding, a site key for the site + preview, the sitemap, a queued crawl."""
    state = (await add_site(owner, "green-leaf.example")).json()       # scheme added automatically
    assistant = state["assistant"]
    assert assistant["company_name"] == "Green Leaf" and assistant["bot_name"] == "Ava"
    assert contrast_with_white(assistant["primary"]) >= WHITE_TEXT_MIN_CONTRAST    # #f4f4f4 was darkened
    assert state["site_key"].startswith("pk_live_")
    assert state["snippet"] == f'<script src="{settings.widget_url}" data-key="{state["site_key"]}" async></script>'
    assert state["install_origins"] == [SITE, "https://www.green-leaf.example"]
    assert state["crawl"]["status"] == "queued"
    assert jobs.jobs[-1][0] == "crawl"
    origins, config = owner_db.execute(
        "SELECT k.allowed_origins, t.config FROM site_keys k JOIN tenants t ON t.id = k.tenant_id "
        "WHERE k.public_key = %s", (state["site_key"],)).fetchone()
    assert settings.app_origin in origins                                  # the preview works
    assert config["knowledge"]["sitemaps"] == [f"{SITE}/sitemap.xml"] and config["knowledge"]["max_pages"] == 50
    assert config["privacy_url"] == f"{SITE}/privacy"


async def test_a_site_without_sitemap_is_crawled_by_following_links(owner, website, owner_db):
    """No robots.txt sitemap and no /sitemap.xml: start at the homepage and follow links."""
    del website[f"{SITE}/robots.txt"], website[f"{SITE}/sitemap.xml"]
    state = (await add_site(owner)).json()
    config = owner_db.execute("SELECT t.config FROM site_keys k JOIN tenants t ON t.id = k.tenant_id "
                              "WHERE k.public_key = %s", (state["site_key"],)).fetchone()[0]
    assert config["knowledge"] == {"start_urls": [f"{SITE}/"], "max_pages": 50}


@pytest.mark.parametrize("url", ["http://green-leaf.example", "ftp://green-leaf.example", "https://user:pw@x.example"])
async def test_unsafe_site_urls_are_refused(owner, url):
    """Only https (http only for localhost in dev), and no credentials in the URL."""
    assert (await add_site(owner, url)).status_code == 422


async def test_an_unreachable_site_is_explained(owner):
    """A site we can't open gets a clear 422, not a half-made assistant."""
    r = await add_site(owner, "https://nowhere.example")
    assert r.status_code == 422 and "couldn't open" in r.json()["detail"]


async def test_one_crawl_at_a_time_and_three_a_day(owner, jobs, owner_db):
    """No second crawl while one waits; at most daily_crawls started per day."""
    await add_site(owner)
    await owner.post("/api/onboarding/crawl", headers=APP)
    assert len(jobs.jobs) == 1                                          # the first is still queued
    for expected in (200, 200, 429):
        owner_db.execute("UPDATE crawl_jobs SET status = 'done' WHERE status = 'queued'")
        assert (await owner.post("/api/onboarding/crawl", headers=APP)).status_code == expected
    assert len(jobs.jobs) == 3


async def test_assistant_edits_are_saved_and_validated(owner):
    """The owner can rename and recolour; invalid values are 422."""
    await add_site(owner)
    edit = {"company_name": "Green Leaf Co", "bot_name": "Fern", "headline": "how can I *help*?",
            "subtitle": "Plants and pots.", "teaser": "Hi!", "primary": "#1a3d7c"}
    state = (await owner.put("/api/onboarding/assistant", json=edit, headers=APP)).json()
    assert state["assistant"]["bot_name"] == "Fern" and state["assistant"]["primary"] == "#1A3D7C"
    assert state["assistant"]["accent"] != "#1A3D7C"                     # derived tint
    for bad in ({"primary": "red"}, {"teaser": "x" * 201}, {"bot_name": ""}):
        assert (await owner.put("/api/onboarding/assistant", json={**edit, **bad}, headers=APP)).status_code == 422


async def test_uploaded_sitemap_keeps_only_the_owners_pages(owner, owner_db):
    """An uploaded sitemap can't make us crawl other people's sites."""
    upload = {"Content-Type": "application/xml", **APP}
    assert (await owner.post("/api/onboarding/sitemap", content=SITEMAP_XML, headers=upload)).status_code == 409
    await add_site(owner)
    xml = (f"<urlset><url><loc>{SITE}/shipping</loc></url><url><loc>https://victim.example/</loc></url>"
           f"<url><loc>http://green-leaf.example/plain-http</loc></url></urlset>")
    state = (await owner.post("/api/onboarding/sitemap", content=xml, headers=upload)).json()
    config = owner_db.execute("SELECT t.config FROM site_keys k JOIN tenants t ON t.id = k.tenant_id "
                              "WHERE k.public_key = %s", (state["site_key"],)).fetchone()[0]
    assert config["knowledge"]["page_urls"] == [f"{SITE}/shipping"]
    foreign = "<urlset><url><loc>https://victim.example/</loc></url></urlset>"
    assert (await owner.post("/api/onboarding/sitemap", content=foreign, headers=upload)).status_code == 422
    dtd = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><urlset></urlset>'
    assert (await owner.post("/api/onboarding/sitemap", content=dtd, headers=upload)).status_code == 422


async def test_install_is_detected_on_the_owners_site_only(owner, client):
    """The preview doesn't count; the first load on the owner's own site marks it installed and live."""
    key = (await add_site(owner)).json()["site_key"]
    await client.get("/config", params={"key": key}, headers={"Origin": settings.app_origin})
    state = (await owner.get("/api/onboarding")).json()
    assert state["installed_at"] is None and state["status"] == "onboarding"
    assert (await client.get("/config", params={"key": key}, headers={"Origin": SITE})).status_code == 200
    state = (await owner.get("/api/onboarding")).json()
    assert state["installed_at"] is not None and state["status"] == "live"


async def test_the_widget_on_someone_elses_site_is_refused(owner, client):
    """A key only works on its owner's origins (and the preview): ownership is proven by installing."""
    key = (await add_site(owner)).json()["site_key"]
    assert (await client.get("/config", params={"key": key}, headers={"Origin": "https://victim.example"})).status_code == 403


async def test_owners_only_see_their_own_tenant(app, owner, monkeypatch):
    """Two owners, two tenants: each session sees only its own state."""
    first = (await add_site(owner)).json()
    box = Outbox()
    monkeypatch.setattr(mailer, "send_email", box)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as other:
        await sign_in(other, box, "someone@else.example")
        second = (await other.get("/api/onboarding")).json()
    assert second["email"] == "someone@else.example" and second["site_key"] is None
    assert first["site_key"] and first["email"] == "owner@green-leaf.example"


async def test_the_worker_crawls_a_queued_job(owner, jobs, embedder, owner_db):  # noqa: F811
    """The queued crawl, run by the worker, indexes the site and completes its job row."""
    await add_site(owner)
    name, payload = jobs.jobs[-1]
    await worker.HANDLERS[name](payload)
    status, indexed = owner_db.execute("SELECT status, pages_indexed FROM crawl_jobs WHERE id = %s",
                                       (payload["crawl_job_id"],)).fetchone()
    assert (status, indexed) == ("done", 2)
    state = (await owner.get("/api/onboarding")).json()
    assert state["crawl"]["status"] == "done" and state["crawl"]["pages_indexed"] == 2
