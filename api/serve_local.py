"""Run the API locally, including on Windows.

    python serve_local.py            # http://127.0.0.1:8000
    PORT=8001 python serve_local.py

Why this exists: psycopg's async driver can't use Windows' default ProactorEventLoop, and
`uvicorn main:app` picks that loop on Windows, so startup fails with a PoolTimeout. This starts
uvicorn on a SelectorEventLoop instead. On Linux/macOS/Docker plain `uvicorn main:app` is fine.
"""
import asyncio
import os

import uvicorn

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8000"))


def main() -> None:
    """Start uvicorn on a SelectorEventLoop (psycopg's async driver needs one on Windows)."""
    server = uvicorn.Server(uvicorn.Config("main:app", host=HOST, port=PORT))
    # SelectorEventLoop works with psycopg on every OS; it's already the default off Windows.
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
