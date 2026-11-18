"""Turn fetched HTML and sitemap XML into plain data. Standard library only.

Everything here is untrusted input from the open web: output is length-capped, and nothing is
executed or followed except through fetcher.py's checks.
"""
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

MAX_TITLE_CHARS = 300
MAX_HEADING_CHARS = 200
MAX_PAGE_TEXT_CHARS = 200_000       # extracted text kept per page
MAX_LINKS_PER_PAGE = 500
MAX_JSON_LD_BLOCKS = 20
MAX_JSON_LD_CHARS = 200_000

# Never text: scripts, styles, embedded objects, interactive widgets.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas",
              "select", "button", "form", "textarea"}
# Site chrome repeated on every page: dropped so it doesn't flood the index.
_CHROME_TAGS = {"nav", "header", "footer", "aside"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
              "track", "wbr"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "li", "ul", "ol", "dl", "dt", "dd", "tr",
               "table", "blockquote", "pre", "figure", "figcaption", "address", "details", "summary",
               "h4", "h5", "h6"}
_CELL_TAGS = {"td", "th"}
# Inline elements that sites place side by side as separate items ("Book a call" + "See demo"):
# a space after each keeps their words apart. Extra spaces are collapsed later.
_SEPARATED_INLINE_TAGS = {"a", "span", "label", "small", "time", "strong", "b", "em", "i", "sup", "sub",
                          "code", "mark", "cite", "abbr"}
HEADING_PATH_LEVELS = 2         # a section is named by its nearest two headings ("Pricing › Team")
CONTACT_LINK_LABELS = {"mailto": "Email", "tel": "Phone"}
MAX_CONTACT_CHARS = 120
_SECTION_HEADINGS = {"h1": 1, "h2": 2, "h3": 3}
_SPACES = re.compile(r"[ \t\r\f\v ]+")


@dataclass
class Section:
    """A run of page text under one heading path."""
    heading: str            # "About › Our team" (h1-h3 path); "" before the first heading
    text: str


@dataclass
class Page:
    """Everything extracted from one HTML page."""
    title: str = ""
    description: str = ""
    canonical: str | None = None
    noindex: bool = False
    sections: list[Section] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    json_ld: list = field(default_factory=list)

    @property
    def text_chars(self) -> int:
        """Total characters of extracted text (for size checks)."""
        return sum(len(s.text) for s in self.sections)


class _PageParser(HTMLParser):
    """Streaming HTML parser that collects title, headings, text, links and JSON-LD."""
    def __init__(self, base_url: str):
        """Start an empty page; `base_url` resolves relative links and canonicals."""
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.page = Page()
        self._skip_depth = 0           # inside <script>, <style>, ...
        self._chrome_depth = 0         # inside <nav>, <header>, ...
        self._main_depth = 0
        self._saw_main = False
        self._in_title = False
        self._title_parts: list[str] = []
        self._heading_level = 0        # inside an h1-h3
        self._heading_parts: list[str] = []
        self._path: list[tuple[int, str]] = []          # current (level, heading) stack
        self._ld_parts: list[str] | None = None         # inside <script type=application/ld+json>
        # Text pieces: (in_main, section heading, text). <main> wins if the page has one.
        self._pieces: list[tuple[bool, str, str]] = []
        self._text_chars = 0

    # -- tags --
    def handle_starttag(self, tag, attrs):
        """Track where we are (skipped, chrome, main, heading) and react to tags."""
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and attr.get("type", "").lower() == "application/ld+json":
            self._ld_parts = []
        if tag in _VOID_TAGS:
            self._void(tag, attr)
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _CHROME_TAGS:
            self._chrome_depth += 1
        if tag == "main":
            self._main_depth += 1
            self._saw_main = True
        if tag == "title":
            self._in_title = True
        elif tag in _SECTION_HEADINGS and not self._hidden:
            self._heading_level = _SECTION_HEADINGS[tag]
            self._heading_parts = []
        elif tag == "a" and attr.get("href"):
            self._link(attr["href"])
            self._contact(attr["href"])
        if tag in _BLOCK_TAGS:
            self._add("\n")
        elif tag in _CELL_TAGS:
            self._add(" | ")

    def handle_startendtag(self, tag, attrs):
        """Self-closing tags (<br/>): handle as a start tag, then an end tag."""
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        """Leave the region the tag opened; block tags end the current line."""
        if tag in _VOID_TAGS:
            return
        if tag == "script" and self._ld_parts is not None:
            self._json_ld("".join(self._ld_parts))
            self._ld_parts = None
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _CHROME_TAGS:
            self._chrome_depth = max(0, self._chrome_depth - 1)
        if tag == "main":
            self._main_depth = max(0, self._main_depth - 1)
        if tag == "title":
            self._in_title = False
        elif tag in _SECTION_HEADINGS and self._heading_level:
            self._close_heading()
        if tag in _BLOCK_TAGS:
            self._add("\n")
        elif tag in _SEPARATED_INLINE_TAGS:
            self._text(" ")

    def handle_data(self, data):
        """Text between tags: JSON-LD is collected raw, everything else as text."""
        if self._ld_parts is not None:
            self._ld_parts.append(data)
            return
        if self._skip_depth:
            return
        # Source newlines are just whitespace in HTML; real breaks come from block tags.
        self._text(data.replace("\r", " ").replace("\n", " "))

    def _text(self, data: str) -> None:
        """Route inline text to the title, the open heading, or the page body."""
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        elif self._heading_level:
            self._heading_parts.append(data)
        elif not self._chrome_depth:
            self._add(data)

    # -- helpers --
    @property
    def _hidden(self) -> bool:
        """True inside skipped tags (scripts, forms...) or site chrome (nav, footer...)."""
        return bool(self._skip_depth or self._chrome_depth)

    def _void(self, tag: str, attr: dict[str, str]) -> None:
        """Handle tags with no content: <br>, <meta> robots/description, <link rel=canonical>."""
        if tag == "br":
            if self._heading_level or self._in_title:
                self._text(" ")             # "Intelligence, engineered<br>to the summit"
            else:
                self._add("\n")
        elif tag == "meta":
            name = attr.get("name", "").lower()
            if name == "robots" and "noindex" in attr.get("content", "").lower():
                self.page.noindex = True
            elif name == "description" and not self.page.description:
                self.page.description = _clean(attr.get("content", ""))[:MAX_TITLE_CHARS]
        elif tag == "link" and "canonical" in attr.get("rel", "").lower().split() and attr.get("href"):
            self.page.canonical = urljoin(self.base_url, attr["href"].strip())

    def _link(self, href: str) -> None:
        """Collect an absolute http(s) link (fragment dropped) for link-following crawls."""
        if len(self.page.links) >= MAX_LINKS_PER_PAGE:
            return
        url = urljoin(self.base_url, href.strip())
        if urlsplit(url).scheme in ("http", "https"):
            self.page.links.append(url.split("#", 1)[0])

    def _contact(self, href: str) -> None:
        """Label contact links ("Email: hi@shop.com"): the bare address alone matches neither
        "what's your email?" in vector search nor the word "email" in text search."""
        scheme, _, rest = href.strip().partition(":")
        label = CONTACT_LINK_LABELS.get(scheme.lower())
        value = unquote(rest.split("?", 1)[0]).strip()
        if label and value and len(value) <= MAX_CONTACT_CHARS and not self._hidden:
            self._add(f" {label}: {value} ")

    def _json_ld(self, raw: str) -> None:
        """Parse one JSON-LD script; malformed or oversized ones are ignored."""
        if len(self.page.json_ld) >= MAX_JSON_LD_BLOCKS or len(raw) > MAX_JSON_LD_CHARS:
            return
        try:
            self.page.json_ld.append(json.loads(raw))
        except ValueError:
            pass   # malformed JSON-LD is common on the web: ignore it

    def _close_heading(self) -> None:
        """Finish an h1-h3: it becomes the current heading path for following text."""
        text = _clean("".join(self._heading_parts))[:MAX_HEADING_CHARS]
        level = self._heading_level
        self._heading_level = 0
        if not text:
            return
        while self._path and self._path[-1][0] >= level:
            self._path.pop()
        self._path.append((level, text))
        self._add("\n")

    def _add(self, text: str) -> None:
        """Append body text under the current heading path (dropped if hidden or over the cap)."""
        if self._hidden or self._text_chars > MAX_PAGE_TEXT_CHARS:
            return
        heading = " › ".join(h for _, h in self._path[-HEADING_PATH_LEVELS:])
        self._pieces.append((self._main_depth > 0, heading, text))
        self._text_chars += len(text)

    def finish(self) -> Page:
        """Assemble sections (only <main> text when the page has one) and return the page."""
        self.close()
        self.page.title = _clean("".join(self._title_parts))[:MAX_TITLE_CHARS]
        pieces = [(h, t) for in_main, h, t in self._pieces if in_main or not self._saw_main]
        sections: list[Section] = []
        for heading, text in pieces:
            if sections and sections[-1].heading == heading:
                sections[-1].text += text
            else:
                sections.append(Section(heading, text))
        for section in sections:
            section.text = _normalise_block(section.text)
        self.page.sections = [s for s in sections if s.text]
        return self.page


def _clean(text: str) -> str:
    """Collapse runs of whitespace into single spaces and trim."""
    return _SPACES.sub(" ", text).strip()


def _normalise_block(text: str) -> str:
    """Collapse spaces within lines and drop empty lines."""
    lines = (_clean(line).strip(" |") for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def parse_html(html: str, base_url: str) -> Page:
    """Extract a Page from raw HTML; `base_url` is the page's own (final) URL."""
    parser = _PageParser(base_url)
    parser.feed(html)
    return parser.finish()


# --- Sitemaps ---------------------------------------------------------------------------------

class SitemapError(ValueError):
    """The sitemap could not be used (invalid XML, a DTD, or not a sitemap at all)."""


def parse_sitemap(xml: bytes) -> tuple[list[str], list[str]]:
    """Return (page URLs, child sitemap URLs) from a <urlset> or <sitemapindex>.

    DTDs are refused outright: sitemaps never need them, and it rules out entity tricks.
    """
    head = xml[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in xml.lower():
        raise SitemapError("sitemap contains a DTD")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SitemapError(f"invalid XML: {exc}") from exc
    kind = root.tag.rsplit("}", 1)[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    if kind == "sitemapindex":
        return [], locs
    if kind == "urlset":
        return locs, []
    raise SitemapError(f"not a sitemap (<{kind}>)")
