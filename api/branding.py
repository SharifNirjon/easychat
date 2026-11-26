"""Auto branding (roadmap step 7): suggest a tenant's assistant config from its homepage.

Pure functions: HTML in, suggestions out. No network, no settings. Everything read from the page
is untrusted: values are trimmed, checked and finally validated by models.TenantConfig, and the
owner reviews and edits them before going live. Templates only, no LLM (a product decision).
"""
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

DEFAULT_BOT_NAME = "Ava"
DEFAULT_PRIMARY = "#1F5F4A"            # easychat green: used when the site declares no usable colour
WHITE_TEXT_MIN_CONTRAST = 4.5          # WCAG AA: the widget header has white text on `primary`
ACCENT_WHITE_MIX = 0.78                # accent = primary mixed with this much white (soft tint)
MAX_COMPANY_CHARS = 60                 # long <title>s make a poor company name
MAX_LINK_TEXT_CHARS = 80

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_TITLE_SEPARATOR = re.compile(r"\s+[|·•–—:-]\s+")


@dataclass
class Branding:
    """What a homepage says about the brand (all optional: plenty of sites declare little)."""
    company_name: str | None = None
    theme_color: str | None = None     # '#rrggbb' from <meta name="theme-color">
    manifest_url: str | None = None    # web app manifest: may hold theme_color / name
    privacy_url: str | None = None     # same-site link whose text or URL mentions privacy
    contact_url: str | None = None     # same-site contact page


class _HomepageParser(HTMLParser):
    """Collects the few tags branding needs; ignores everything else."""

    def __init__(self, base_url: str):
        """Resolve links against `base_url`; only same-site links are kept."""
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.host = urlsplit(base_url).netloc.lower()
        self.meta: dict[str, str] = {}
        self.title = ""
        self.manifest: str | None = None
        self.links: list[tuple[str, str]] = []      # (absolute same-site URL, link text)
        self._in_title = False
        self._link: str | None = None
        self._link_text = ""

    def handle_starttag(self, tag, attrs):
        """Record meta values, the manifest link, the title and same-site anchors."""
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:site_name", "application-name", "theme-color") and key not in self.meta:
                self.meta[key] = a.get("content", "").strip()
        elif tag == "link" and "manifest" in a.get("rel", "").lower().split():
            self.manifest = self._same_site(a.get("href", ""))
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            self._link, self._link_text = self._same_site(a.get("href", "")), ""

    def handle_endtag(self, tag):
        """Close the title or the current anchor."""
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link:
            self.links.append((self._link, " ".join(self._link_text.split())[:MAX_LINK_TEXT_CHARS]))
            self._link = None

    def handle_data(self, data):
        """Text of the title or of the current anchor."""
        if self._in_title:
            self.title += data
        elif self._link:
            self._link_text += data

    def _same_site(self, href: str) -> str | None:
        """An absolute http(s) URL on the homepage's own host, or None."""
        url = urljoin(self.base_url, href.strip())
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.netloc.lower() == self.host:
            return parts._replace(fragment="").geturl()
        return None


def _find_link(links: list[tuple[str, str]], word: str) -> str | None:
    """The first link whose text or URL contains `word`."""
    return next((url for url, text in links if word in text.lower() or word in url.lower()), None)


def _letters(text: str) -> str:
    """Lowercase letters and digits only ('Green-Leaf Co.' -> 'greenleafco')."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_from_title(title: str, host: str) -> str | None:
    """The brand part of a <title>. Titles are 'Tagline | Brand' as often as 'Brand | Tagline', so
    prefer the part that matches the domain name (fernwood.com: 'Fernwood Supply'); else the first."""
    parts = [p for p in _TITLE_SEPARATOR.split(" ".join(title.split())) if p.strip()]
    if not parts:
        return None
    label = _letters(host.split(":")[0].removeprefix("www.").split(".")[0])
    matching = [p for p in parts if len(label) >= 3 and len(_letters(p)) >= 3
                and (label in _letters(p) or _letters(p) in label)]
    return (matching or parts)[0]


def parse_homepage(html: str, base_url: str) -> Branding:
    """Branding hints from a homepage's HTML."""
    parser = _HomepageParser(base_url)
    parser.feed(html)
    name = parser.meta.get("og:site_name") or parser.meta.get("application-name") or \
        name_from_title(parser.title, parser.host)
    return Branding(
        company_name=clean_name(name),
        theme_color=normalize_hex(parser.meta.get("theme-color")),
        manifest_url=parser.manifest,
        privacy_url=_find_link(parser.links, "privacy"),
        contact_url=_find_link(parser.links, "contact"),
    )


# --- Names and colours --------------------------------------------------------------------------

def clean_name(name: str | None) -> str | None:
    """A short, single-line company name, or None if there's nothing usable."""
    if not name:
        return None
    name = " ".join(name.split())[:MAX_COMPANY_CHARS].strip(" -|·:")
    return name or None


def name_from_host(host: str) -> str:
    """'www.green-leaf.com' -> 'Green Leaf' (the fallback when a site has no usable name)."""
    label = host.split(":")[0].removeprefix("www.").split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part) or "My business"


def normalize_hex(value: str | None) -> str | None:
    """'#abc' / 'aabbcc' / '#AABBCC' -> '#AABBCC'; None for anything else (named colours, rgb())."""
    match = _HEX.match((value or "").strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return f"#{digits.upper()}"


def _rgb(color: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> (r, g, b)."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _luminance(color: str) -> float:
    """WCAG relative luminance of a '#RRGGBB' colour (0 black .. 1 white)."""
    def channel(value: int) -> float:
        """sRGB channel to linear light."""
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_with_white(color: str) -> float:
    """WCAG contrast ratio of white text on this background (1..21)."""
    return 1.05 / (_luminance(color) + 0.05)


def _mix(color: str, other: str, amount: float) -> str:
    """`color` moved `amount` (0..1) of the way towards `other`."""
    mixed = (round(a + (b - a) * amount) for a, b in zip(_rgb(color), _rgb(other)))
    return "#" + "".join(f"{v:02X}" for v in mixed)


def readable_primary(color: str | None) -> str:
    """The brand colour, darkened just enough for white header text to pass WCAG AA."""
    color = normalize_hex(color) or DEFAULT_PRIMARY
    for _ in range(20):
        if contrast_with_white(color) >= WHITE_TEXT_MIN_CONTRAST:
            return color
        color = _mix(color, "#000000", 0.1)
    return DEFAULT_PRIMARY


def accent_for(primary: str) -> str:
    """A soft tint of the primary colour, for the avatar and highlights."""
    return _mix(primary, "#FFFFFF", ACCENT_WHITE_MIX)


# --- Config templates ---------------------------------------------------------------------------

def assistant_config(company: str, bot_name: str, primary: str, privacy_url: str,
                     contact_url: str | None = None) -> dict:
    """A complete TenantConfig dict for a tenant's assistant, from the template texts."""
    config: dict = {
        "company_name": company,
        "bot_name": bot_name,
        "greeting": {"title": "Hi there,", "headline": "how can I *help*?",
                     "subtitle": f"Ask me anything about {company}."},
        "disclosure": f"{bot_name} is an AI assistant. It answers from {company}'s website and can make mistakes.",
        "teaser": f"Hi! Questions about {company}? Ask me.",
        "privacy_url": privacy_url,
        "theme": {"primary": primary, "accent": accent_for(primary)},
        "fallback": {"text": "I couldn't find that on our website. Our team can help."},
    }
    if contact_url and contact_url.startswith("https://"):     # Fallback.contact_url must be https
        config["fallback"]["contact_url"] = contact_url
    return config


def placeholder_config(privacy_url: str) -> dict:
    """The config of a brand-new sign-up, before they've told us their website."""
    return assistant_config("My business", DEFAULT_BOT_NAME, DEFAULT_PRIMARY, privacy_url)
