"""Self-serve onboarding API (roadmap step 7): the backend of the onboarding app (/app).

Every route needs a signed-in session (auth.current_session); the tenant comes from the session,
and every query runs inside db.tenant_tx(), so RLS confines it to that tenant.

  GET  /api/onboarding            the whole onboarding state (the app renders from this)
  POST /api/onboarding/site       {url}: read the homepage -> suggested branding, find the sitemap,
                                  create the site key, start the first crawl
  POST /api/onboarding/sitemap    raw sitemap XML upload (instead of / after the URL)
  PUT  /api/onboarding/assistant  the owner's edits: names, texts, colours
  POST /api/onboarding/crawl      crawl again (free plan: limits.daily_crawls per day)

Ownership of a website is proven by installing the widget on it: a site key only works from its
allowed origins (the owner's site, plus the app itself for the preview), so anyone may *preview*
an assistant for any site, but it only goes live where the owner can paste the script tag.
mark_installed() records the first load from the owner's site (called by GET /config).
"""
import json
import logging
import secrets
from urllib.parse import urljoin, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError

import crawler
import db
import limits
import order_connections
from auth import Session, current_session, same_origin
from blocks import LOCAL_HOSTS
from branding import (DEFAULT_BOT_NAME, accent_for, assistant_config, name_from_host, normalize_hex,
                      parse_homepage, readable_primary)
from config import settings
from extract import SitemapError, parse_sitemap
from fetcher import (HTML_TYPES, PAGE_MAX_BYTES, XML_TYPES, FetchError, Fetcher, check_url, host_key)
from jobs import Queue, RedisQueue
from models import HEX_COLOR, TenantConfig, TenantLimits
from tenancy import Tenant

log = logging.getLogger("easychat.onboarding")

router = APIRouter(prefix="/api/onboarding", dependencies=[Depends(same_origin)])
queue: Queue = RedisQueue(limits.client)

SITEMAP_UPLOAD_PATH = "/api/onboarding/sitemap"
SITEMAP_UPLOAD_MAX_BYTES = 1_000_000             # main.py raises the body limit for this path only
MANIFEST_TYPES = ("application/manifest+json", "application/json")
MANIFEST_MAX_BYTES = 100_000
MAX_SITEMAPS = 5
ACTIVE_CRAWL_STATES = ("queued", "running")


# --- Reading the tenant -------------------------------------------------------------------------

async def _tenant_row(tenant_id: UUID) -> dict:
    """The tenant with its (single) active site key, as RLS lets this tenant see it."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT t.id, t.name, t.config, t.limits, t.status, t.plan, t.site_url, "
            "k.id AS site_key_id, k.public_key, k.allowed_origins, k.installed_at "
            "FROM tenants t LEFT JOIN site_keys k ON k.tenant_id = t.id AND k.revoked_at IS NULL "
            "WHERE t.id = %s ORDER BY k.created_at DESC LIMIT 1", (tenant_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Please sign in")
    return row


async def _latest_crawl(tenant_id: UUID) -> dict | None:
    """The tenant's most recent crawl job (its counters drive the progress bar)."""
    async with db.tenant_tx(tenant_id) as conn:
        cur = await conn.execute(
            "SELECT status, pages_found, pages_indexed, pages_unchanged, pages_skipped, pages_failed, "
            "error, started_at, finished_at FROM crawl_jobs ORDER BY started_at DESC LIMIT 1")
        return await cur.fetchone()


def snippet(public_key: str) -> str:
    """The script tag the owner pastes into their site."""
    return f'<script src="{settings.widget_url}" data-key="{public_key}" async></script>'


async def _state(session: Session) -> dict:
    """Everything the onboarding app shows, in one response."""
    row = await _tenant_row(session.tenant_id)
    config = TenantConfig.model_validate(row["config"])
    plan = TenantLimits.model_validate(row["limits"])
    crawl = await _latest_crawl(session.tenant_id)
    key = row["public_key"]
    return {
        "email": session.email,
        "status": row["status"],
        "plan": row["plan"],
        "site_url": row["site_url"],
        "assistant": {
            "company_name": config.company_name, "bot_name": config.bot_name,
            "headline": config.greeting.headline, "subtitle": config.greeting.subtitle,
            "teaser": config.teaser, "primary": config.theme.primary, "accent": config.theme.accent,
        },
        "crawl": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in crawl.items()} if crawl else None,
        "site_key": key,
        "snippet": snippet(key) if key else None,
        "widget_url": settings.widget_url,
        "install_origins": [o for o in (row["allowed_origins"] or []) if o != settings.app_origin],
        "installed_at": row["installed_at"].isoformat() if row["installed_at"] else None,
        "limits": {"max_pages": plan.max_pages, "daily_crawls": plan.daily_crawls,
                   "crawls_left_today": await limits.crawls_left(session.tenant_id, plan)},
        "orders": await order_connections.summary(session.tenant_id),      # step 8; never credentials
        "orders_available": settings.secrets_key is not None,
    }


@router.get("")
async def state(session: Session = Depends(current_session)) -> dict:
    """The onboarding state for the signed-in owner."""
    return await _state(session)


# --- Crawling -----------------------------------------------------------------------------------

async def start_crawl(tenant_id: UUID, plan: TenantLimits) -> None:
    """Queue a crawl for the worker, unless one is already waiting or running (counts the daily cap)."""
    latest = await _latest_crawl(tenant_id)
    if latest and latest["status"] in ACTIVE_CRAWL_STATES:
        return
    await limits.count_crawl(tenant_id, plan)
    job_id = await crawler.queue_job(tenant_id)
    await queue.enqueue("crawl", {"tenant_id": str(tenant_id), "crawl_job_id": str(job_id)})


@router.post("/crawl")
async def crawl_again(session: Session = Depends(current_session)) -> dict:
    """Re-read the website (e.g. after the owner changed it)."""
    row = await _tenant_row(session.tenant_id)
    if not row["config"].get("knowledge"):
        raise HTTPException(status_code=409, detail="Add your website first")
    await start_crawl(session.tenant_id, TenantLimits.model_validate(row["limits"]))
    return await _state(session)


# --- Step: your website -------------------------------------------------------------------------

class SiteIn(BaseModel):
    """The owner's website (or its sitemap URL)."""
    url: str = Field(min_length=3, max_length=2000)


def _www_variants(host: str) -> list[str]:
    """'shop.com' -> ['shop.com', 'www.shop.com'] (and the reverse); local hosts stay as they are."""
    name = host.split(":")[0]
    if name in LOCAL_HOSTS or name.replace(".", "").isdigit():
        return [host]
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    return [host, other]


def parse_site_url(raw: str) -> tuple[str, str, str, bool]:
    """(normalised URL, page URL to read, scheme://host root, whether it is a sitemap).
    422 for unusable URLs."""
    raw = raw.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    local = parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local and settings.is_dev):
        raise HTTPException(status_code=422, detail="Enter an https:// address")
    if not parts.hostname or parts.username or parts.password:
        raise HTTPException(status_code=422, detail="Enter a website address like https://myshop.com")
    root = f"{parts.scheme}://{parts.netloc.lower()}"
    url = parts._replace(fragment="", netloc=parts.netloc.lower(), path=parts.path or "/").geturl()
    is_sitemap = parts.path.lower().endswith(".xml")
    # A sitemap URL: read the homepage next to it (https://shop.com/sitemap.xml -> https://shop.com/).
    return url, (urljoin(url, "./") if is_sitemap else url), root, is_sitemap


async def _theme_from_manifest(fetcher: Fetcher, manifest_url: str) -> str | None:
    """theme_color from the site's web app manifest, if it has a valid one."""
    try:
        result = await fetcher.get(manifest_url, MANIFEST_TYPES, MANIFEST_MAX_BYTES)
        return normalize_hex(str(json.loads(result.text()).get("theme_color", "")))
    except (FetchError, ValueError, AttributeError):
        return None


async def _find_sitemaps(fetcher: Fetcher, root: str) -> list[str]:
    """Sitemaps listed in robots.txt, else /sitemap.xml if it parses; [] if the site has none."""
    hosts = fetcher.allowed_hosts
    listed = []
    for url in await fetcher.robots_sitemaps(root + "/"):
        try:
            listed.append(check_url(url, hosts, fetcher.allow_local))
        except FetchError:
            continue                                 # a sitemap on another host: not ours to crawl
    if listed:
        return listed[:MAX_SITEMAPS]
    try:
        parse_sitemap((await fetcher.get(f"{root}/sitemap.xml", XML_TYPES, PAGE_MAX_BYTES)).body)
        return [f"{root}/sitemap.xml"]
    except (FetchError, SitemapError):
        return []


async def _save_site(tenant_id: UUID, row: dict, site_url: str, config: dict, origins: list[str]) -> None:
    """Store the site and config, and create or update the tenant's site key."""
    try:
        TenantConfig.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="We couldn't build a valid configuration "
                                                    "from that website") from exc
    site_changed = row["site_url"] != site_url
    async with db.tenant_tx(tenant_id) as conn:
        await conn.execute("UPDATE tenants SET name = %s, config = %s, site_url = %s WHERE id = %s",
                           (config["company_name"], Jsonb(config), site_url, tenant_id))
        if row["site_key_id"] is None:
            await conn.execute(
                "INSERT INTO site_keys (tenant_id, public_key, allowed_origins) VALUES (%s, %s, %s)",
                (tenant_id, f"pk_live_{secrets.token_hex(12)}", origins))
        else:
            await conn.execute(
                "UPDATE site_keys SET allowed_origins = %s, "
                "installed_at = CASE WHEN %s THEN NULL ELSE installed_at END WHERE id = %s",
                (origins, site_changed, row["site_key_id"]))
    if site_changed and row["site_key_id"] is not None:
        await limits.client.delete(_installed_key(row["site_key_id"]))


@router.post("/site")
async def set_site(body: SiteIn, session: Session = Depends(current_session)) -> dict:
    """Read the homepage, suggest branding, find the sitemap, create the site key, start the crawl."""
    url, page, root, is_sitemap = parse_site_url(body.url)
    host = urlsplit(root).netloc
    hosts = frozenset(_www_variants(host))
    # A sitemap in a folder with no page of its own (/shop/sitemap.xml): read the site root instead.
    candidates = [page, root + "/"] if is_sitemap and page != root + "/" else [page]
    async with Fetcher(hosts, allow_local=settings.is_dev) as fetcher:
        for candidate in candidates:
            try:
                homepage = await fetcher.get(candidate, HTML_TYPES, PAGE_MAX_BYTES)
                break
            except FetchError as exc:
                if candidate == candidates[-1]:
                    raise HTTPException(status_code=422, detail=f"We couldn't open {root}: {exc}") from exc
        branding = parse_homepage(homepage.text(), homepage.url)
        theme = branding.theme_color
        if theme is None and branding.manifest_url:
            theme = await _theme_from_manifest(fetcher, branding.manifest_url)
        sitemaps = [url] if is_sitemap else await _find_sitemaps(fetcher, root)

    final = urlsplit(homepage.url)                   # after redirects (e.g. to www.)
    site_root = f"{final.scheme}://{final.netloc.lower()}"
    row = await _tenant_row(session.tenant_id)
    plan = TenantLimits.model_validate(row["limits"])
    company = branding.company_name or name_from_host(final.netloc)
    config = assistant_config(company, row["config"].get("bot_name") or DEFAULT_BOT_NAME,
                              readable_primary(theme), branding.privacy_url or site_root + "/",
                              branding.contact_url)
    config["image_hosts"] = sorted(hosts)
    config["knowledge"] = {"sitemaps": sitemaps, "max_pages": plan.max_pages} if sitemaps else \
        {"start_urls": [homepage.url], "max_pages": plan.max_pages}
    origins = [f"{final.scheme}://{h}" for h in _www_variants(final.netloc.lower())] + [settings.app_origin]
    await _save_site(session.tenant_id, row, site_root, config, origins)
    await start_crawl(session.tenant_id, plan)
    return await _state(session)


@router.post("/sitemap")
async def upload_sitemap(request: Request, session: Session = Depends(current_session)) -> dict:
    """Use an uploaded sitemap.xml (raw XML body) for the pages to index, then crawl."""
    row = await _tenant_row(session.tenant_id)
    if not row["site_url"]:
        raise HTTPException(status_code=409, detail="Add your website first")
    try:
        pages, children = parse_sitemap(await request.body())
    except SitemapError as exc:
        raise HTTPException(status_code=422, detail=f"That file isn't a usable sitemap: {exc}") from exc
    hosts = frozenset(_www_variants(urlsplit(row["site_url"]).netloc))

    def on_site(urls: list[str]) -> list[str]:
        """The URLs that are on the owner's own site (others are dropped, never fetched)."""
        kept = []
        for url in urls:
            try:
                kept.append(check_url(url, hosts, settings.is_dev))
            except FetchError:
                continue
        return kept

    plan = TenantLimits.model_validate(row["limits"])
    pages, children = on_site(pages)[:plan.max_pages], on_site(children)[:MAX_SITEMAPS]
    if not pages and not children:
        raise HTTPException(status_code=422, detail=f"That sitemap has no pages on {row['site_url']}")
    config = dict(row["config"])
    config["knowledge"] = {"page_urls": pages, "sitemaps": children, "max_pages": plan.max_pages}
    await _save_site(session.tenant_id, row, row["site_url"], config, row["allowed_origins"])
    await start_crawl(session.tenant_id, plan)
    return await _state(session)


# --- Step: your assistant -----------------------------------------------------------------------

class AssistantIn(BaseModel):
    """What the owner may edit in the assistant step (validated again by TenantConfig)."""
    company_name: str = Field(min_length=1, max_length=120)
    bot_name: str = Field(min_length=1, max_length=40)
    headline: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(min_length=1, max_length=300)
    teaser: str = Field(min_length=1, max_length=200)
    primary: str = Field(pattern=HEX_COLOR)
    accent: str | None = Field(default=None, pattern=HEX_COLOR)    # None: derived from primary


@router.put("/assistant")
async def edit_assistant(body: AssistantIn, session: Session = Depends(current_session)) -> dict:
    """Save the owner's edits to the assistant's names, texts and colours."""
    row = await _tenant_row(session.tenant_id)
    config = dict(row["config"])
    config.update(company_name=body.company_name.strip(), bot_name=body.bot_name.strip(),
                  teaser=body.teaser.strip())
    config["greeting"] = {**config["greeting"], "headline": body.headline.strip(), "subtitle": body.subtitle.strip()}
    primary = body.primary.upper()
    config["theme"] = {**config["theme"], "primary": primary, "accent": (body.accent or accent_for(primary)).upper()}
    try:
        TenantConfig.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Some of those values aren't valid") from exc
    async with db.tenant_tx(session.tenant_id) as conn:
        await conn.execute("UPDATE tenants SET name = %s, config = %s WHERE id = %s",
                           (config["company_name"], Jsonb(config), session.tenant_id))
    return await _state(session)


# --- Install detection --------------------------------------------------------------------------

def _installed_key(site_key_id: UUID) -> str:
    """Redis flag: this site key has been seen on its owner's site (saves a DB write per page view)."""
    return f"installed:{site_key_id}"


async def mark_installed(tenant: Tenant, origin: str) -> None:
    """The widget loaded from an allowed origin other than our preview: the owner installed it.
    The first time, record it and take the tenant live."""
    if origin == settings.app_origin:
        return
    try:
        if not await limits.client.set(_installed_key(tenant.site_key_id), "1", nx=True):
            return                                   # already recorded
    except Exception:  # Redis down: skip; the next page view tries again
        log.warning("install check skipped: redis unavailable")
        return
    try:
        async with db.tenant_tx(tenant.id) as conn:
            await conn.execute("UPDATE site_keys SET installed_at = now() WHERE id = %s AND installed_at IS NULL",
                               (tenant.site_key_id,))
            await conn.execute("UPDATE tenants SET status = 'live' WHERE id = %s AND status = 'onboarding'",
                               (tenant.id,))
    except Exception:  # best effort: recording an install must never break a tenant's widget
        log.exception("could not record the install for tenant %s", tenant.slug)
        await limits.client.delete(_installed_key(tenant.site_key_id))   # try again next page view
        return
    log.info("widget installed for tenant %s", tenant.slug)
