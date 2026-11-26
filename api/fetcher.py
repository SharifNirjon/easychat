"""SSRF-safe HTTP fetching for the crawler. Plain HTTP only: no browser, no JavaScript.

The crawler fetches URLs that come from tenant config and from crawled pages (sitemaps, links,
redirects), so every URL is hostile until checked. Defences, in order:

  1. URL rules: https only (http only for localhost in local/test), no credentials, and the host
     must be one of the crawl's allowed hosts (the hosts of the tenant's own sitemaps/start URLs).
  2. DNS: every address the host resolves to must be public (no loopback, private, link-local
     incl. the cloud metadata IP 169.254.169.254, CGNAT, multicast, reserved; IPv4 and IPv6).
  3. Pinning: the connection goes to the address that was checked (_PublicOnlyBackend), so DNS
     rebinding can't swap in an internal address between the check and the connect.
  4. Redirects are followed by hand, at most MAX_REDIRECTS, and each hop goes through 1-3 again.
  5. Caps: response size (streamed, aborted when exceeded), total time, content type; no
     compression (Accept-Encoding: identity, so no zip bombs); env proxies are never used.
  6. Politeness: robots.txt is honoured and requests to one host are spaced out.
"""
import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import anyio
import httpcore

from blocks import LOCAL_HOSTS

log = logging.getLogger("easychat.fetcher")

USER_AGENT = "easychat-crawler/0.1 (+site assistant indexing)"
MAX_REDIRECTS = 5
MAX_URL_CHARS = 2000
CONNECT_TIMEOUT_SECONDS = 5
TOTAL_TIMEOUT_SECONDS = 15
MIN_SECONDS_BETWEEN_REQUESTS = 0.5     # per host: at most ~2 requests/second
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

HTML_TYPES = ("text/html", "application/xhtml+xml")
XML_TYPES = ("application/xml", "text/xml", "text/plain")   # sitemaps are often served as text/plain
TEXT_TYPES = ("text/plain",)
PAGE_MAX_BYTES = 2_000_000
SITEMAP_MAX_BYTES = 5_000_000
ROBOTS_MAX_BYTES = 200_000


class FetchError(Exception):
    """A URL that was refused or couldn't be fetched. `blocked` = refused by policy, not a network error."""

    def __init__(self, message: str, blocked: bool = False, status: int | None = None):
        """Keep the reason, whether policy refused it, and the HTTP status if any."""
        super().__init__(message)
        self.blocked = blocked
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    """A successful (2xx) response, after redirects, with its body fully read."""
    url: str                 # final URL, after redirects
    status: int
    content_type: str        # lowercased media type, no parameters
    charset: str | None
    body: bytes
    headers: dict[str, str]  # lowercased names; repeated headers joined with ", "

    def text(self) -> str:
        """The body decoded with the declared charset (UTF-8 if none), never raising."""
        return self.body.decode(self.charset or "utf-8", errors="replace")


# --- 1. URL rules --------------------------------------------------------------------------

def host_key(url: str) -> str:
    """'host' or 'host:port' (lowercase), as used in allowed_hosts."""
    return urlsplit(url).netloc.lower()


def check_url(url: str, allowed_hosts: frozenset[str], allow_local: bool) -> str:
    """Return the URL without its fragment, or raise FetchError(blocked=True)."""
    if len(url) > MAX_URL_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise FetchError("invalid url", blocked=True)
    parts = urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        raise FetchError("url must have a host and no credentials", blocked=True)
    local_http = parts.scheme == "http" and allow_local and parts.hostname in LOCAL_HOSTS
    if parts.scheme != "https" and not local_http:
        raise FetchError("url must be https", blocked=True)
    if parts.netloc.lower() not in allowed_hosts:
        raise FetchError("host is not one of this crawl's sites", blocked=True)
    return parts._replace(fragment="").geturl()


# --- 2 + 3. DNS checks, pinned to the checked address ---------------------------------------

def is_public_ip(address: str) -> bool:
    """True only for globally routable unicast addresses (never internal ones)."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])   # drop an IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped                                 # ::ffff:10.0.0.1 is 10.0.0.1
    return ip.is_global and not ip.is_multicast


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself, refuses non-public addresses, and connects to the checked one.

    TLS still verifies the certificate against the original hostname: httpcore passes it as the
    server name to start_tls() separately from the address we connect to.
    """

    def __init__(self, allow_local: bool):
        """Wrap httpcore's default network backend; `allow_local` = local dev."""
        self._inner = httpcore.AnyIOBackend()
        self._allow_local = allow_local

    def _allowed(self, host: str, address: str) -> bool:
        """May we connect to this address for this host?"""
        if self._allow_local and host in LOCAL_HOSTS:
            return ipaddress.ip_address(address).is_loopback   # local dev: loopback only, nothing else
        return is_public_ip(address)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        """Resolve, refuse if any address is non-public, then connect to a checked one."""
        try:
            infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses or not all(self._allowed(host, a) for a in addresses):
            # Every address must pass: otherwise a host could mix a public and a private record.
            raise _BlockedAddress(host)
        # Try the checked addresses in order (e.g. "localhost" is ::1 and 127.0.0.1).
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        raise httpcore.ConnectError(f"cannot connect to {host}") from last_error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        """Unix sockets are never allowed: they reach local services."""
        raise _BlockedAddress("unix socket")

    async def sleep(self, seconds: float) -> None:
        """Delegate httpcore's internal sleeps to the wrapped backend."""
        await self._inner.sleep(seconds)


class _BlockedAddress(httpcore.ConnectError):
    """Raised by the backend when a host resolves to a non-public address."""
    def __init__(self, host: str):
        """Name the host in the message (never the address it resolved to)."""
        super().__init__(f"{host} resolves to a non-public address")


# --- 4-6. The fetcher ------------------------------------------------------------------------

@dataclass
class Fetcher:
    """One crawl's HTTP client. Use as `async with Fetcher(...) as fetcher:`."""
    allowed_hosts: frozenset[str]
    allow_local: bool = False
    _pool: httpcore.AsyncConnectionPool | None = field(default=None, init=False)
    _last_request: dict[str, float] = field(default_factory=dict, init=False)
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict, init=False)

    async def __aenter__(self) -> "Fetcher":
        """Create the connection pool, wired to the public-only network backend."""
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PublicOnlyBackend(self.allow_local), max_connections=4, retries=0
        )
        return self

    async def __aexit__(self, *exc) -> None:
        """Close every pooled connection."""
        if self._pool:
            await self._pool.aclose()

    async def get(self, url: str, accept: tuple[str, ...], max_bytes: int) -> FetchResult:
        """GET a URL under all the rules above. Raises FetchError; never returns a non-2xx."""
        current = check_url(url, self.allowed_hosts, self.allow_local)
        for _ in range(MAX_REDIRECTS + 1):
            status, headers, body = await self._request(current, max_bytes)
            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise FetchError(f"redirect without location ({status})", status=status)
                current = check_url(urljoin(current, location), self.allowed_hosts, self.allow_local)
                continue
            if not 200 <= status < 300:
                raise FetchError(f"HTTP {status}", status=status)
            media_type, charset = _parse_content_type(headers.get("content-type", ""))
            if media_type not in accept:
                raise FetchError(f"unexpected content type {media_type or 'none'}", blocked=True)
            return FetchResult(current, status, media_type, charset, body, headers)
        raise FetchError("too many redirects")

    async def allowed_by_robots(self, url: str) -> bool:
        """robots.txt for the URL's site; a missing or unreadable robots.txt allows everything."""
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._robots:
            parser: RobotFileParser | None = None
            try:
                result = await self.get(f"{key}/robots.txt", TEXT_TYPES, ROBOTS_MAX_BYTES)
                parser = RobotFileParser()
                parser.parse(result.text().splitlines())
            except FetchError:
                parser = None
            self._robots[key] = parser
        parser = self._robots[key]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    async def robots_sitemaps(self, url: str) -> list[str]:
        """The `Sitemap:` URLs in the robots.txt of the URL's site (onboarding finds sitemaps here)."""
        await self.allowed_by_robots(url)              # reads and caches robots.txt
        parts = urlsplit(url)
        parser = self._robots[f"{parts.scheme}://{parts.netloc}"]
        return list(parser.site_maps() or []) if parser else []

    async def _request(self, url: str, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        """One GET without redirects: status, headers and a size-capped body."""
        assert self._pool is not None, "use Fetcher as an async context manager"
        await self._wait_turn(host_key(url))
        request_headers = [
            (b"User-Agent", USER_AGENT.encode()),
            (b"Accept", b"text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8"),
            (b"Accept-Encoding", b"identity"),
        ]
        timeouts = {"connect": CONNECT_TIMEOUT_SECONDS, "read": TOTAL_TIMEOUT_SECONDS,
                    "write": TOTAL_TIMEOUT_SECONDS, "pool": TOTAL_TIMEOUT_SECONDS}
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                async with self._pool.stream("GET", url, headers=request_headers,
                                             extensions={"timeout": timeouts}) as response:
                    headers = _header_dict(response.headers)
                    declared = headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(f"response larger than {max_bytes} bytes", blocked=True)
                    chunks, size = [], 0
                    if response.status not in REDIRECT_STATUSES:
                        async for chunk in response.aiter_stream():
                            size += len(chunk)
                            if size > max_bytes:
                                raise FetchError(f"response larger than {max_bytes} bytes", blocked=True)
                            chunks.append(chunk)
                    return response.status, headers, b"".join(chunks)
        except _BlockedAddress as exc:
            raise FetchError(str(exc), blocked=True) from exc
        except TimeoutError as exc:
            raise FetchError("timed out") from exc
        except httpcore.TimeoutException as exc:
            raise FetchError("timed out") from exc
        except httpcore.NetworkError as exc:
            raise FetchError(f"network error: {type(exc).__name__}") from exc
        except httpcore.ProtocolError as exc:
            raise FetchError(f"protocol error: {type(exc).__name__}") from exc
        except httpcore.UnsupportedProtocol as exc:
            raise FetchError("unsupported protocol", blocked=True) from exc

    async def _wait_turn(self, host: str) -> None:
        """Space out requests to one host (politeness, and our own crawl budget)."""
        last = self._last_request.get(host)
        if last is not None:
            wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request[host] = time.monotonic()


def _header_dict(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """Raw response headers -> {lowercase name: value}; repeated headers are joined."""
    headers: dict[str, str] = {}
    for name, value in raw:
        key = name.decode("latin-1").lower()
        text = value.decode("latin-1")
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return headers


def _parse_content_type(value: str) -> tuple[str, str | None]:
    """'text/html; charset=UTF-8' -> ('text/html', 'utf-8'); unknown charsets -> None."""
    media_type, _, params = value.partition(";")
    charset = None
    for param in params.split(";"):
        name, _, val = param.strip().partition("=")
        if name.lower() == "charset" and val:
            charset = val.strip().strip('"').lower()
    if charset:
        try:
            "".encode(charset)            # unknown charset names fall back to utf-8
        except LookupError:
            charset = None
    return media_type.strip().lower(), charset
