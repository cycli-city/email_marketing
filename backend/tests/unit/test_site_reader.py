"""Reading a company's own site: page budget, redirect safety, failures, and what is (not) treated as visible text."""

from __future__ import annotations

from app.integrations.web.fetcher import FetchError
from app.services.discovery.crawl import MAX_TEXT_CHARS, SiteReader
from tests.web_fake import FakeFetcher, html, page

HOME = "https://clinic.example.com/"
CONTACT = "https://clinic.example.com/contact"
ABOUT = "https://clinic.example.com/about"

HOME_HTML = html(
    '<a href="/contact">Contact us</a> <a href="/about">About</a> <a href="mailto:info@clinic.example.com">Write to us</a>'
)
CONTACT_HTML = html(
    "<p>Reach the owner: owner@clinic.example.com or our partner at partner@partner.example.org</p>"
)
ABOUT_HTML = html("<p>Founded 1999. Email info@clinic.example.com</p>")


async def test_reads_the_home_page_then_the_contact_pages_and_puts_own_addresses_first() -> None:
    fetcher = FakeFetcher(
        {
            HOME: page(HOME, HOME_HTML),
            CONTACT: page(CONTACT, CONTACT_HTML),
            ABOUT: page(ABOUT, ABOUT_HTML),
        }
    )
    reading = await SiteReader(fetcher).read(HOME)

    assert reading.pages == (HOME, CONTACT, ABOUT)
    assert reading.problem is None
    assert [(e.address, e.same_site) for e in reading.emails] == [
        ("info@clinic.example.com", True),
        ("owner@clinic.example.com", True),
        ("partner@partner.example.org", False),
    ]
    # the first page that published an address is the provenance, not a later page repeating it
    assert reading.emails[0].page_url == HOME
    assert "Reach the owner" in reading.text


async def test_the_page_budget_is_respected() -> None:
    fetcher = FakeFetcher(
        {
            HOME: page(HOME, HOME_HTML),
            CONTACT: page(CONTACT, CONTACT_HTML),
            ABOUT: page(ABOUT, ABOUT_HTML),
        }
    )
    reading = await SiteReader(fetcher, max_pages=2).read(HOME)
    assert len(fetcher.requested) == 2
    assert len(reading.pages) == 2


async def test_a_page_that_cannot_be_read_does_not_stop_the_others() -> None:
    fetcher = FakeFetcher(
        {
            HOME: page(HOME, HOME_HTML),
            CONTACT: FetchError("timeout"),
            ABOUT: page(ABOUT, ABOUT_HTML),
        }
    )
    reading = await SiteReader(fetcher).read(HOME)
    assert reading.pages == (HOME, ABOUT)
    assert reading.problem == "timeout"
    assert [e.address for e in reading.emails] == ["info@clinic.example.com"]


async def test_a_home_page_that_cannot_be_read_yields_nothing_and_says_why() -> None:
    reading = await SiteReader(FakeFetcher({HOME: FetchError("disallowed by robots.txt")})).read(
        HOME
    )
    assert reading.pages == ()
    assert reading.emails == ()
    assert reading.text == ""
    assert reading.problem == "disallowed by robots.txt"


async def test_a_refusal_by_the_ssrf_guard_is_marked_unsafe() -> None:
    reading = await SiteReader(
        FakeFetcher({HOME: FetchError("private address", unsafe=True)})
    ).read(HOME)
    assert reading.problem == "private address (unsafe)"


async def test_a_redirect_to_a_different_site_is_ignored_even_if_it_publishes_addresses() -> None:
    elsewhere = "https://other-business.example.org/"
    fetcher = FakeFetcher(
        {HOME: page(elsewhere, html('<a href="mailto:sales@other-business.example.org">sales</a>'))}
    )
    reading = await SiteReader(fetcher).read(HOME)
    assert reading.pages == ()
    assert reading.emails == ()  # these would have looked "same-site" for the wrong business
    assert reading.text == ""
    assert reading.problem == "redirected to another site"


async def test_a_redirect_within_the_same_registrable_domain_is_fine() -> None:
    fetcher = FakeFetcher(
        {
            HOME: page(
                "https://www.clinic.example.com/en/",
                html('<a href="mailto:info@clinic.example.com">x</a>'),
            )
        }
    )
    reading = await SiteReader(fetcher).read(HOME)
    assert reading.pages == ("https://www.clinic.example.com/en/",)
    assert [e.address for e in reading.emails] == ["info@clinic.example.com"]


async def test_a_page_is_never_requested_twice() -> None:
    fetcher = FakeFetcher(
        {
            HOME: page(HOME, html('<a href="/">home</a> <a href="/contact">contact</a>')),
            CONTACT: page(CONTACT, html("hi")),
        }
    )
    await SiteReader(fetcher).read(HOME)
    assert len(fetcher.requested) == len(set(fetcher.requested))


async def test_scripts_and_styles_are_not_visible_text() -> None:
    markup = html(
        "<script>var secret = 'not for people';</script><style>.a{color:red}</style><p>Welcome to Smile Dental</p>"
    )
    reading = await SiteReader(FakeFetcher({HOME: page(HOME, markup)})).read(HOME)
    assert "Welcome to Smile Dental" in reading.text
    assert "not for people" not in reading.text
    assert "color:red" not in reading.text


async def test_the_text_is_bounded() -> None:
    reading = await SiteReader(
        FakeFetcher({HOME: page(HOME, html("<p>" + "word " * 100_000 + "</p>"))}), max_pages=1
    ).read(HOME)
    assert len(reading.text) <= MAX_TEXT_CHARS
