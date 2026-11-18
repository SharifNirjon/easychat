"""SSRF defences of the crawler's fetcher (fetcher.py).

URL and DNS rules are tested directly; redirects, size and content-type caps against a real
HTTP server on 127.0.0.1 (allowed only because allow_local=True and the host is "localhost").
"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest

import fetcher
from fetcher import HTML_TYPES, Fetcher, FetchError, check_url, is_public_ip

SITE = frozenset({"shop.example"})


# --- URL rules ------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://shop.example/",                    # plain http
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://user:pw@shop.example/",           # credentials
    "https://other.example/",                  # not this crawl's site
    "https://shop.example.evil.example/",
    "https://shop.example/a b",                # whitespace
    "https://shop.example/" + "a" * 3000,
])
def test_check_url_refuses(url):
    with pytest.raises(FetchError) as info:
        check_url(url, SITE, allow_local=False)
    assert info.value.blocked


def test_check_url_drops_the_fragment():
    assert check_url("https://shop.example/p?q=1#reviews", SITE, False) == "https://shop.example/p?q=1"


def test_local_http_only_for_localhost_and_only_when_allowed():
    local = frozenset({"localhost:4173"})
    assert check_url("http://localhost:4173/", local, allow_local=True)
    with pytest.raises(FetchError):
        check_url("http://localhost:4173/", local, allow_local=False)


@pytest.mark.parametrize("address,public", [
    ("93.184.216.34", True),
    ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False),                # cloud metadata
    ("100.64.0.1", False),                     # CGNAT
    ("0.0.0.0", False), ("224.0.0.1", False), ("::1", False), ("fe80::1", False), ("fc00::1", False),
    ("::ffff:10.0.0.1", False),                # IPv4-mapped private
])
def test_is_public_ip(address, public):
    assert is_public_ip(address) is public


# --- DNS: the connect is refused when the name resolves to a non-public address --------------

def _fake_dns(monkeypatch, addresses):
    async def getaddrinfo(host, port, **_):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in addresses]
    monkeypatch.setattr(anyio, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("addresses", [
    ["10.0.0.5"], ["169.254.169.254"], ["127.0.0.1"],
    ["93.184.216.34", "10.0.0.5"],             # one bad record is enough to refuse
])
async def test_private_dns_answers_are_refused(monkeypatch, addresses):
    _fake_dns(monkeypatch, addresses)
    async with Fetcher(SITE) as f:
        with pytest.raises(FetchError) as info:
            await f.get("https://shop.example/", HTML_TYPES, 1000)
    assert info.value.blocked


async def test_localhost_is_refused_without_allow_local(monkeypatch):
    async with Fetcher(frozenset({"localhost"}), allow_local=False) as f:
        with pytest.raises(FetchError):
            await f.get("https://localhost/", HTML_TYPES, 1000)


# --- A real server on 127.0.0.1 ---------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        status, headers, body = self.routes.get(self.path, (404, {}, b"not found"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _DualStackServer(ThreadingHTTPServer):
    """Listens on ::1 and 127.0.0.1 alike, so "localhost" connects at once whichever it tries first."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


@pytest.fixture(scope="module")
def server():
    try:
        httpd = _DualStackServer(("::", 0), _Handler)
    except OSError:                                   # no IPv6 (e.g. some Docker networks)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://localhost:{httpd.server_address[1]}"
    _Handler.routes = {
        "/page": (200, {"Content-Type": "text/html; charset=utf-8"}, b"<p>hello</p>"),
        "/hop": (302, {"Location": "/page"}, b""),
        "/offsite": (302, {"Location": "https://evil.example/"}, b""),
        "/to-metadata": (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""),
        "/loop": (302, {"Location": "/loop"}, b""),
        "/big": (200, {"Content-Type": "text/html"}, b"x" * 5000),
        "/image": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
        "/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private\n"),
    }
    yield base
    httpd.shutdown()


@pytest.fixture(autouse=True)
def no_politeness_delay(monkeypatch):
    monkeypatch.setattr(fetcher, "MIN_SECONDS_BETWEEN_REQUESTS", 0)   # tests needn't wait on themselves


def _fetcher(base: str) -> Fetcher:
    return Fetcher(frozenset({base.split("//", 1)[1]}), allow_local=True)


async def test_fetches_a_page_and_follows_same_site_redirects(server):
    async with _fetcher(server) as f:
        result = await f.get(f"{server}/hop", HTML_TYPES, 1000)
    assert result.url == f"{server}/page" and result.text() == "<p>hello</p>" and result.charset == "utf-8"


@pytest.mark.parametrize("path", ["/offsite", "/to-metadata"])
async def test_redirects_off_site_or_to_internal_addresses_are_refused(server, path):
    async with _fetcher(server) as f:
        with pytest.raises(FetchError) as info:
            await f.get(f"{server}{path}", HTML_TYPES, 1000)
    assert info.value.blocked


async def test_redirect_loops_stop(server):
    async with _fetcher(server) as f:
        with pytest.raises(FetchError, match="too many redirects"):
            await f.get(f"{server}/loop", HTML_TYPES, 1000)


async def test_oversized_and_wrong_type_responses_are_refused(server):
    async with _fetcher(server) as f:
        with pytest.raises(FetchError, match="larger than"):
            await f.get(f"{server}/big", HTML_TYPES, 1000)
        with pytest.raises(FetchError, match="content type"):
            await f.get(f"{server}/image", HTML_TYPES, 1000)


async def test_http_errors_carry_their_status(server):
    async with _fetcher(server) as f:
        with pytest.raises(FetchError) as info:
            await f.get(f"{server}/missing", HTML_TYPES, 1000)
    assert info.value.status == 404 and not info.value.blocked


async def test_robots_txt_is_honoured(server):
    async with _fetcher(server) as f:
        assert await f.allowed_by_robots(f"{server}/page")
        assert not await f.allowed_by_robots(f"{server}/private/x")
