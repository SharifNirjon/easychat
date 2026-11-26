"""branding.py: homepage hints, readable colours and config templates. Pure: no network."""
import pytest

from branding import (DEFAULT_PRIMARY, WHITE_TEXT_MIN_CONTRAST, accent_for, assistant_config, contrast_with_white,
                      name_from_host, normalize_hex, parse_homepage, placeholder_config, readable_primary)
from models import TenantConfig

HOMEPAGE = """<html lang="en"><head>
<title>Green Leaf | Indoor plants delivered</title>
<meta property="og:site_name" content="  Green   Leaf Plants ">
<meta name="theme-color" content="#2a7">
<link rel="manifest" href="/site.webmanifest">
</head><body>
<a href="/contact-us">Contact</a>
<a href="https://evil.example/privacy">Privacy (elsewhere)</a>
<a href="/legal/privacy-policy">Privacy policy</a>
</body></html>"""


def test_homepage_hints_are_read():
    """Name, theme colour, manifest and same-site privacy/contact links are found."""
    hints = parse_homepage(HOMEPAGE, "https://greenleaf.example/")
    assert hints.company_name == "Green Leaf Plants"
    assert hints.theme_color == "#22AA77"
    assert hints.manifest_url == "https://greenleaf.example/site.webmanifest"
    assert hints.privacy_url == "https://greenleaf.example/legal/privacy-policy"   # not the off-site one
    assert hints.contact_url == "https://greenleaf.example/contact-us"


def test_title_is_the_fallback_name():
    """Without og:site_name, the first part of the <title> is used."""
    hints = parse_homepage("<title>Green Leaf – Indoor plants</title>", "https://greenleaf.example/")
    assert hints.company_name == "Green Leaf"


@pytest.mark.parametrize("title, host, expected", [
    ("Indoor plants and handmade pots | Fernwood Supply", "fernwood.example", "Fernwood Supply"),
    ("Fernwood Supply | Indoor plants", "www.fernwood.example", "Fernwood Supply"),
    ("Shop online – Green Leaf", "green-leaf.example", "Green Leaf"),
    ("Welcome | Home", "acme.example", "Welcome"),                 # nothing matches: the first part
])
def test_the_brand_part_of_a_title_is_chosen(title, host, expected):
    """'Tagline | Brand' and 'Brand | Tagline' both give the brand when it matches the domain."""
    assert parse_homepage(f"<title>{title}</title>", f"https://{host}/").company_name == expected


def test_a_page_with_no_hints():
    """A bare page gives no suggestions (the caller falls back to the host name)."""
    hints = parse_homepage("<p>hello</p>", "https://greenleaf.example/")
    assert hints.company_name is None and hints.theme_color is None
    assert name_from_host("www.green-leaf.example") == "Green Leaf"


@pytest.mark.parametrize("value, expected", [
    ("#abc", "#AABBCC"), ("aabbcc", "#AABBCC"), ("#AaBbCc", "#AABBCC"),
    ("red", None), ("rgb(1,2,3)", None), ("#abcd", None), (None, None), ("", None),
])
def test_hex_colours_are_normalised(value, expected):
    """Only hex colours are accepted; everything else is ignored."""
    assert normalize_hex(value) == expected


@pytest.mark.parametrize("brand", ["#FFFFFF", "#F0F0F0", "#FFE600", "#22AA77", "#000000", None, "nonsense"])
def test_header_colour_is_always_readable(brand):
    """White header text on the chosen primary passes WCAG AA, whatever the site declared."""
    primary = readable_primary(brand)
    assert contrast_with_white(primary) >= WHITE_TEXT_MIN_CONTRAST


def test_dark_brand_colours_are_kept_and_missing_ones_get_the_default():
    """A colour that is already readable is used as-is; no colour means the default."""
    assert readable_primary("#1A3D7C") == "#1A3D7C"
    assert readable_primary(None) == DEFAULT_PRIMARY
    assert accent_for("#000000") != "#000000"           # a lighter tint


def test_templates_are_valid_tenant_configs():
    """Every template validates as a TenantConfig, including a long company name."""
    TenantConfig.model_validate(placeholder_config("http://localhost:8000/app/"))
    config = assistant_config("X" * 60, "Ava", "#1A3D7C", "https://shop.example/privacy",
                              contact_url="https://shop.example/contact")
    TenantConfig.model_validate(config)
    assert config["fallback"]["contact_url"] == "https://shop.example/contact"


def test_plain_http_contact_page_is_not_offered():
    """The fallback's contact link must be https; an http one is left out rather than failing."""
    config = assistant_config("Shop", "Ava", "#1A3D7C", "http://localhost:3001/", contact_url="http://localhost:3001/c")
    assert "contact_url" not in config["fallback"]
    TenantConfig.model_validate(config)
