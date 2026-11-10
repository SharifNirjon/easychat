"""Tenant registry: one JSON file per tenant in api/tenants/, validated at startup.

Every request resolves its tenant from the site id + the browser's Origin header
(CLAUDE.md rule 4). This file-based registry is temporary until the tenants table exists.
"""
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field, HttpUrl

TENANTS_DIR = Path(__file__).parent / "tenants"

SITE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


class Greeting(BaseModel):
    title: str
    headline: str   # words wrapped in *asterisks* render in the accent serif font
    subtitle: str


class Theme(BaseModel):
    primary: str = Field(pattern=HEX_COLOR)
    primary_dark: str = Field(pattern=HEX_COLOR)
    accent: str = Field(pattern=HEX_COLOR)


class Tenant(BaseModel):
    site_id: str = Field(pattern=SITE_ID_PATTERN)
    company_name: str
    bot_name: str
    greeting: Greeting
    disclosure: str
    teaser: str
    suggested_prompts: list[str] = Field(max_length=3)
    privacy_url: HttpUrl
    theme: Theme
    allowed_origins: list[str] = Field(min_length=1)

    def public_config(self) -> dict:
        """What the widget is allowed to see. Never includes allowed_origins."""
        return self.model_dump(mode="json", exclude={"allowed_origins"})


def load_tenants(directory: Path = TENANTS_DIR) -> dict[str, Tenant]:
    """Load and validate every tenant file. A bad file stops startup on purpose."""
    tenants: dict[str, Tenant] = {}
    for path in sorted(directory.glob("*.json")):
        tenant = Tenant.model_validate_json(path.read_text(encoding="utf-8"))
        if tenant.site_id in tenants:
            raise RuntimeError(f"Duplicate site_id {tenant.site_id!r} in {path.name}")
        tenants[tenant.site_id] = tenant
    if not tenants:
        raise RuntimeError(f"No tenant files found in {directory}")
    return tenants


def all_allowed_origins(tenants: dict[str, Tenant]) -> list[str]:
    """Union of every tenant's origins, used for CORS. Per-tenant checks happen in resolve_tenant."""
    return sorted({origin for t in tenants.values() for origin in t.allowed_origins})


def resolve_tenant(tenants: dict[str, Tenant], site_id: str, origin: str | None) -> Tenant:
    """Return the tenant for this request, or raise 404 (unknown site) / 403 (origin not allowed).

    A missing Origin is treated as not allowed: browsers always send it on these requests.
    """
    tenant = tenants.get(site_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unknown site")
    if origin is None or origin.rstrip("/") not in tenant.allowed_origins:
        raise HTTPException(status_code=403, detail="Origin not allowed for this site")
    return tenant
