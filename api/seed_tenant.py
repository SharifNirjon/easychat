"""Create or update a tenant and its site key from a JSON file (idempotent).

    python seed_tenant.py seeds/culpine.json

Uses MIGRATION_DATABASE_URL (owner). This is the admin path until self-serve onboarding
(roadmap step 7) creates tenants through the API.
"""
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from models import TenantSeed

load_dotenv(Path(__file__).parent / ".env")


def seed(conn: psycopg.Connection, tenant: TenantSeed) -> None:
    with conn.transaction():
        tenant_id = conn.execute(
            "INSERT INTO tenants (slug, name, config, limits) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, config = EXCLUDED.config, "
            "limits = EXCLUDED.limits RETURNING id",
            (tenant.slug, tenant.name, Jsonb(tenant.config.model_dump(mode="json")),
             Jsonb(tenant.limits.model_dump())),
        ).fetchone()[0]
        owner = conn.execute(
            "SELECT tenant_id FROM site_keys WHERE public_key = %s", (tenant.site_key,)
        ).fetchone()
        if owner and owner[0] != tenant_id:
            raise SystemExit(f"site key {tenant.site_key} already belongs to another tenant")
        conn.execute(
            "INSERT INTO site_keys (tenant_id, public_key, allowed_origins) VALUES (%s, %s, %s) "
            "ON CONFLICT (public_key) DO UPDATE SET allowed_origins = EXCLUDED.allowed_origins, revoked_at = NULL",
            (tenant_id, tenant.site_key, tenant.allowed_origins),
        )
    print(f"tenant {tenant.slug} ready: site key {tenant.site_key}, origins {', '.join(tenant.allowed_origins)}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python seed_tenant.py <seed.json>")
    tenant = TenantSeed.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        sys.exit("Missing required env var MIGRATION_DATABASE_URL (see api/.env.example)")
    with psycopg.connect(url, autocommit=True) as conn:
        seed(conn, tenant)


if __name__ == "__main__":
    main()
