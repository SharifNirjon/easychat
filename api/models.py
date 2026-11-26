"""Pure data models shared by the API and the CLI scripts. No settings, no I/O.

Note on stored configs: unknown keys in tenants.config are ignored (pydantic's default), so a
row written by an older seed still loads; re-run seed_tenant.py to pick up new fields."""
import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, HttpUrl, field_validator, model_validator

from blocks import LOCAL_HOSTS, Icon

SITE_KEY_PATTERN = r"^pk_[A-Za-z0-9_]{8,64}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"
ORIGIN_PATTERN = r"^https?://[a-z0-9.-]+(:[0-9]{1,5})?$"   # scheme://host[:port], lowercase, no path
HOST_PATTERN = r"^[a-z0-9.-]+(:[0-9]{1,5})?$"              # host[:port], lowercase, no scheme

# TenantConfig fields the widget never receives (see tenancy.Tenant.public_config).
SERVER_ONLY_FIELDS = frozenset({"image_hosts", "knowledge", "fallback"})


def normalize_origin(origin: str) -> str:
    """Canonical form for comparing origins: trimmed, lowercase, no trailing slash."""
    return origin.strip().rstrip("/").lower()


class Greeting(BaseModel):
    """The home screen's greeting text."""
    title: str = Field(max_length=80)
    headline: str = Field(max_length=120)   # words wrapped in *asterisks* render in the accent serif font
    subtitle: str = Field(max_length=300)


class Theme(BaseModel):
    """Seed colours; the widget derives soft tints, borders and muted text from these."""
    primary: str = Field(pattern=HEX_COLOR)                       # header, visitor bubbles, main buttons
    accent: str = Field(pattern=HEX_COLOR)                        # avatar and highlights
    surface: str = Field(default="#FCFDF7", pattern=HEX_COLOR)    # panel background
    text: str = Field(default="#10211B", pattern=HEX_COLOR)       # body text


def _https_only(url: HttpUrl) -> HttpUrl:
    """Reject non-https URLs (e.g. plain http) in tenant config."""
    if url.scheme != "https":
        raise ValueError("must be an https URL")
    return url


HttpsUrl = Annotated[HttpUrl, AfterValidator(_https_only)]


class HomeAction(BaseModel):
    """A home-screen tile: tapping it either sends `message` or opens `url` (exactly one)."""
    icon: Icon
    title: str = Field(min_length=1, max_length=40)
    subtitle: str = Field(default="", max_length=60)
    message: str | None = Field(default=None, min_length=1, max_length=200)
    url: HttpsUrl | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> "HomeAction":
        """A tile must either send a message or open a URL, not both or neither."""
        if (self.message is None) == (self.url is None):
            raise ValueError("give exactly one of message or url")
        return self


class Home(BaseModel):
    """The welcome screen under the greeting (Mira design `widget.png`)."""
    promo: HomeAction | None = None
    cards: list[HomeAction] = Field(default_factory=list, max_length=4)
    chips_label: str = Field(default="Try asking", max_length=40)
    chips: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("chips")
    @classmethod
    def chips_are_short(cls, chips: list[str]) -> list[str]:
        """Every home chip must be 1-60 characters."""
        if any(not 1 <= len(chip) <= 60 for chip in chips):
            raise ValueError("each chip must be 1-60 characters")
        return chips


MAX_CRAWL_PAGES = 500   # hard ceiling per tenant; the free plan sets less via knowledge.max_pages


def _http_url(url: HttpUrl) -> HttpUrl:
    """Crawl targets: https, or http for localhost (local dev). fetcher.py re-checks at fetch time."""
    if url.scheme == "https" or (url.scheme == "http" and url.host in LOCAL_HOSTS):
        return url
    raise ValueError("must be an https URL (http only for localhost)")


CrawlUrl = Annotated[HttpUrl, AfterValidator(_http_url)]


class KnowledgeConfig(BaseModel):
    """What to crawl. The crawler never leaves the hosts of these URLs."""
    sitemaps: list[CrawlUrl] = Field(default_factory=list, max_length=5)
    # Pages to index as-is; with no sitemap, the crawler also follows their same-site links.
    start_urls: list[CrawlUrl] = Field(default_factory=list, max_length=20)
    # Exact pages from an uploaded sitemap file (onboarding): indexed as-is, links not followed.
    page_urls: list[CrawlUrl] = Field(default_factory=list, max_length=MAX_CRAWL_PAGES)
    max_pages: int = Field(default=200, ge=1, le=MAX_CRAWL_PAGES)

    @model_validator(mode="after")
    def has_a_source(self) -> "KnowledgeConfig":
        """There must be something to crawl: a sitemap, a start URL or a page list."""
        if not self.sitemaps and not self.start_urls and not self.page_urls:
            raise ValueError("give at least one sitemap, start URL or page URL")
        return self


class Fallback(BaseModel):
    """What to offer when the knowledge base has no answer (rendered as notice + chips)."""
    text: str = Field(min_length=1, max_length=300)
    contact_label: str = Field(default="Contact us", min_length=1, max_length=40)
    contact_url: HttpsUrl | None = None
    handoff_label: str = Field(default="Talk to a person", min_length=1, max_length=40)
    handoff_message: str | None = Field(default=None, min_length=1, max_length=200)


class TenantConfig(BaseModel):
    """Everything the widget may see about a tenant, plus SERVER_ONLY_FIELDS, which it never gets.
    Stored in tenants.config."""
    company_name: str = Field(max_length=120)
    bot_name: str = Field(max_length=40)
    greeting: Greeting
    disclosure: str | None = Field(default=None, max_length=400)
    teaser: str = Field(max_length=200)
    home: Home = Field(default_factory=Home)
    privacy_url: HttpUrl
    theme: Theme
    # Hosts ("host" or "host:port") that block images may load from. Server-side only.
    image_hosts: list[str] = Field(default_factory=list, max_length=10)
    knowledge: KnowledgeConfig | None = None   # server-only: what crawl.py indexes
    fallback: Fallback | None = None           # server-only: offered when there's no answer

    @field_validator("image_hosts")
    @classmethod
    def hosts_are_bare(cls, hosts: list[str]) -> list[str]:
        """Image hosts must be bare 'host' or 'host:port' (lowercased)."""
        cleaned = [h.strip().lower() for h in hosts]
        for host in cleaned:
            if not re.match(HOST_PATTERN, host):
                raise ValueError(f"not a bare host[:port]: {host!r}")
        return cleaned


class TenantLimits(BaseModel):
    """Free-plan caps (the only plan for now). A tenant's tenants.limits JSON overrides
    individual fields; seeded tenants set their own."""
    daily_tokens: int = Field(default=200_000, gt=0)
    monthly_conversations: int = Field(default=300, gt=0)
    conversation_user_turns: int = Field(default=40, gt=0)
    max_pages: int = Field(default=50, gt=0, le=MAX_CRAWL_PAGES)     # pages per crawl
    daily_crawls: int = Field(default=3, gt=0)                       # crawls started per day


class TenantSeed(BaseModel):
    """A tenant + site key definition file, as read by seed_tenant.py."""
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(max_length=120)
    site_key: str = Field(pattern=SITE_KEY_PATTERN)
    allowed_origins: list[str] = Field(min_length=1)
    limits: TenantLimits = TenantLimits()
    config: TenantConfig

    @field_validator("allowed_origins")
    @classmethod
    def origins_are_bare(cls, origins: list[str]) -> list[str]:
        """Origins must be bare 'scheme://host[:port]' (normalised)."""
        cleaned = [normalize_origin(o) for o in origins]
        for origin in cleaned:
            if not re.match(ORIGIN_PATTERN, origin):
                raise ValueError(f"not a bare origin (scheme://host[:port]): {origin!r}")
        return cleaned
