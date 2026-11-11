"""Apply pending SQL migrations from api/migrations/ in filename order.

    python migrate.py

Connects with MIGRATION_DATABASE_URL — the schema owner. The API itself never gets these
credentials; it uses DATABASE_URL (a non-owner member of easychat_app, so RLS applies to it).

In APP_ENV=local/test this also creates the API's login role from DATABASE_URL if it is
missing. In staging/production that role is created by infrastructure (Terraform), not here.
"""
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import psycopg
from dotenv import load_dotenv
from psycopg import sql

load_dotenv(Path(__file__).parent / ".env")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing required env var {name} (see api/.env.example)")
    return value


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.stem in applied:
            continue
        with conn.transaction():  # each file applies completely or not at all
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        done.append(path.stem)
    return done


def ensure_dev_app_role(conn: psycopg.Connection, database_url: str) -> None:
    """Local/test only: create the API's login role (from DATABASE_URL) as a member of easychat_app."""
    url = urlparse(database_url)
    user, password = unquote(url.username or ""), unquote(url.password or "")
    if not user or not password:
        sys.exit("DATABASE_URL must include a user and password")
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(user), sql.Literal(password)
        )
    )
    conn.execute(sql.SQL("GRANT easychat_app TO {}").format(sql.Identifier(user)))


def main() -> None:
    with psycopg.connect(env("MIGRATION_DATABASE_URL"), autocommit=True) as conn:
        done = apply_migrations(conn)
        print(f"migrations applied: {', '.join(done) if done else 'none (up to date)'}")
        if env("APP_ENV") in ("local", "test"):
            ensure_dev_app_role(conn, env("DATABASE_URL"))
            print("dev app role ready")


if __name__ == "__main__":
    main()
