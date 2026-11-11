"""Per-request tenant resolution.

Every request resolves its tenant from the public site key + the browser's Origin header
(CLAUDE.md rule 4). Tenants and site keys live in Postgres; see migrations/0001_init.sql.
"""
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

import db
from models import SITE_KEY_PATTERN, TenantConfig, TenantLimits, normalize_origin

__all__ = ["SITE_KEY_PATTERN", "Tenant", "resolve_tenant"]


@dataclass(frozen=True)
class Tenant:
    id: UUID
    site_key_id: UUID
    slug: str
    config: TenantConfig
    limits: TenantLimits

    def public_config(self) -> dict:
        """What the widget is allowed to see. Never includes origins, limits or ids."""
        return self.config.model_dump(mode="json")


async def resolve_tenant(site_key: str, origin: str | None) -> Tenant:
    """Return the tenant for this request, or raise 404 (unknown key) / 403 (origin not allowed).

    A missing Origin is treated as not allowed: browsers always send it on these requests.
    Non-browser clients can forge Origin, so this is not authentication: the rate limits and
    budgets in limits.py are what bound abuse.
    """
    row = await db.resolve_site_key(site_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown site key")
    allowed = {normalize_origin(o) for o in row["allowed_origins"]}
    if origin is None or normalize_origin(origin) not in allowed:
        raise HTTPException(status_code=403, detail="Origin not allowed for this site")
    return Tenant(
        id=row["tenant_id"],
        site_key_id=row["site_key_id"],
        slug=row["slug"],
        config=TenantConfig.model_validate(row["config"]),
        limits=TenantLimits.model_validate(row["limits"]),
    )
