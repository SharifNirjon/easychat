"""HTML/sitemap extraction (extract.py), schema.org products (products.py) and chunking (chunker.py)."""
import json

import pytest

from chunker import MAX_CHARS, MAX_CHUNKS_PER_PAGE, chunk_sections
from extract import Section, SitemapError, parse_html, parse_sitemap
from products import page_kind, products_from_json_ld

PAGE = """<!doctype html><html><head>
<title> Pricing &amp; plans | Shop </title>
<meta name="description" content="Our plans">
<link rel="canonical" href="/pricing">
<script>var secret = "not text";</script><style>p{color:red}</style>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Shop"}</script>
<script type="application/ld+json">{not json</script>
</head><body>
<header><nav><a href="/">Home</a> <a href="/about#team">About</a></nav></header>
<aside>Newsletter signup</aside>
<main>
  <h1>Pricing</h1>
  <p>Simple   plans for
     everyone.</p>
  <h2>Starter</h2><ul><li>One site</li><li>$9 / month</li></ul>
  <h3>Limits</h3><p>Up to 1,000 chats.</p>
  <h2>Team</h2><table><tr><th>Seats</th><td>10</td></tr></table>
  <form><button>Buy</button><input value="hidden"></form>
  <a href="mailto:x@y.z">mail</a> <a href="https://other.example/page">other</a>
</main>
<footer>© Shop — all rights reserved</footer>
</body></html>"""


def test_parse_html_keeps_main_content_and_drops_chrome():
    page = parse_html(PAGE, "https://shop.example/pricing?ref=x")
    text = "\n".join(s.text for s in page.sections)
    assert page.title == "Pricing & plans | Shop"
    assert page.description == "Our plans"
    assert page.canonical == "https://shop.example/pricing"
    assert "Simple plans for everyone." in text
    assert "$9 / month" in text and "Seats | 10" in text
    for gone in ("not text", "color:red", "Home", "Newsletter", "all rights reserved", "Buy", "hidden"):
        assert gone not in text


def test_headings_become_a_section_path():
    page = parse_html(PAGE, "https://shop.example/pricing")
    headings = [s.heading for s in page.sections]
    assert headings == ["Pricing", "Pricing › Starter", "Starter › Limits", "Pricing › Team"]   # nearest two


def test_side_by_side_inline_items_and_heading_breaks_keep_words_apart():
    page = parse_html('<h1>Intelligence, engineered<br>to the summit</h1>'
                      '<p><a href="/a">Try demos</a><a href="/b">Estimate</a> <span>(01)</span><span>easychat</span></p>',
                      "https://s.example/")
    assert page.sections[0].heading == "Intelligence, engineered to the summit"
    assert page.sections[0].text == "Try demos Estimate (01) easychat"


def test_links_and_json_ld():
    page = parse_html(PAGE, "https://shop.example/pricing")
    assert "https://shop.example/about" in page.links           # fragment dropped
    assert "https://other.example/page" in page.links            # off-site links kept; the crawler filters
    assert not any(link.startswith("mailto:") for link in page.links)
    assert page.json_ld == [{"@context": "https://schema.org", "@type": "Organization", "name": "Shop"}]


def test_contact_links_are_labelled_for_search():
    page = parse_html('<p>Write to <a href="mailto:hi%40shop.example?subject=Hi">us</a> or call '
                      '<a href="tel:+8801700000000">+880 1700 000000</a>.</p>'
                      '<footer><a href="mailto:footer@shop.example">x</a></footer>', "https://s.example/")
    text = page.sections[0].text
    assert "Email: hi@shop.example" in text and "Phone: +8801700000000" in text
    assert "footer@shop.example" not in text                     # site chrome stays out


def test_noindex_and_pages_without_main():
    page = parse_html('<meta name="robots" content="NOINDEX, follow"><p>Hello</p>', "https://s.example/")
    assert page.noindex and page.sections[0].text == "Hello"


def test_sitemap_urlset_and_index():
    urlset = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              b"<url><loc> https://s.example/a </loc></url><url><loc>https://s.example/b</loc></url></urlset>")
    assert parse_sitemap(urlset) == (["https://s.example/a", "https://s.example/b"], [])
    index = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             b"<sitemap><loc>https://s.example/s1.xml</loc></sitemap></sitemapindex>")
    assert parse_sitemap(index) == ([], ["https://s.example/s1.xml"])


@pytest.mark.parametrize("xml", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>',
    b"<html><body>not a sitemap</body></html>",
    b"<urlset><url>",
])
def test_bad_sitemaps_are_refused(xml):
    with pytest.raises(SitemapError):
        parse_sitemap(xml)


# --- Products -------------------------------------------------------------------------------

def test_products_from_json_ld_handles_graphs_offers_and_ratings():
    blocks = [{"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "ignored"},
        {"@type": ["Product"], "name": "  Glow   Drops ", "url": "/p/glow", "image": ["/img/glow.png"],
         "brand": {"@type": "Brand", "name": "Mirela"}, "description": "Bright.",
         "offers": [{"@type": "Offer", "price": "12.50", "priceCurrency": "usd",
                     "availability": "https://schema.org/OutOfStock"}],
         "aggregateRating": {"ratingValue": "9", "bestRating": "10", "reviewCount": "1,240"}},
    ]}]
    [product] = products_from_json_ld(blocks, "https://shop.example/p/glow")
    assert product.name == "Glow Drops" and product.brand == "Mirela"
    assert product.url == "https://shop.example/p/glow"
    assert product.image_url == "https://shop.example/img/glow.png"
    assert (product.price, product.currency, product.availability) == ("12.5", "USD", "Out of stock")
    assert product.rating == 4.5 and product.review_count == 1240
    assert "Price: 12.5 USD" in product.summary()


@pytest.mark.parametrize("blocks,kind", [
    ([{"@type": "BlogPosting", "headline": "RAG"}], "article"),
    ([{"@graph": [{"@type": "WebSite"}, {"@type": ["NewsArticle"]}]}], "article"),
    ([{"@type": "Organization"}, {"@type": "Product", "name": "X"}], "page"),
    ([], "page"), (["junk", None], "page"),
])
def test_page_kind_from_json_ld(blocks, kind):
    assert page_kind(blocks) == kind


@pytest.mark.parametrize("junk", [
    [{"@type": "Product"}],                                            # no name
    [{"@type": "Product", "name": 42, "offers": "free"}],              # wrong types
    [{"@type": "Thing", "name": "Not a product"}],
    ["text", None, 3],
])
def test_odd_json_ld_is_ignored_or_reduced_safely(junk):
    products = products_from_json_ld(junk, "https://s.example/")
    assert all(isinstance(p.name, str) and p.name for p in products)
    assert all(p.price is None for p in products)


def test_product_fields_are_capped_and_non_http_images_dropped():
    blocks = [{"@type": "Product", "name": "N" * 500, "description": "D" * 5000, "image": "javascript:alert(1)",
               "offers": {"price": "-3"}}]
    [product] = products_from_json_ld(json.loads(json.dumps(blocks)), "https://s.example/")
    assert len(product.name) == 200 and len(product.description) == 1000
    assert product.image_url is None and product.price is None


# --- Chunking -------------------------------------------------------------------------------

def test_small_sections_share_a_chunk_and_name_their_headings():
    chunks = chunk_sections([Section("Pricing", "Plans for everyone."), Section("Pricing › Team", "10 seats.")])
    assert len(chunks) == 1
    assert chunks[0].content == "## Pricing\nPlans for everyone.\n## Pricing › Team\n10 seats."
    assert chunks[0].heading == "Pricing"


def test_long_sections_split_within_limits_with_overlap():
    paragraphs = "\n".join(f"Paragraph {i} " + "word " * 60 for i in range(40))
    chunks = chunk_sections([Section("Guide", paragraphs)])
    assert len(chunks) > 3
    assert all(len(c.content) <= MAX_CHARS for c in chunks)
    assert all(c.content.startswith("## Guide\n") for c in chunks)          # every chunk names its section
    first_tail = chunks[0].content[-50:]
    assert first_tail in chunks[1].content                                   # overlap carries over


def test_one_giant_paragraph_is_cut_and_pages_are_capped():
    chunks = chunk_sections([Section("", "x" * 5000)])
    assert all(len(c.content) <= MAX_CHARS for c in chunks) and "".join(chunks[0].content.split())
    many = chunk_sections([Section("", "\n".join("y " * 400 for _ in range(500)))])
    assert len(many) == MAX_CHUNKS_PER_PAGE
