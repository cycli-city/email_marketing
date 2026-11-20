"""Published contact details out of a company's own pages: only what the site publishes, nothing invented."""

from __future__ import annotations

import json

import pytest

from app.integrations.web.extract import candidate_pages, extract_emails

PAGE = "https://clinic.example.com/contact"
GMAIL = "gmail" + ".com"


def cf_encode(address: str, key: int = 0x42) -> str:
    return bytes([key, *(b ^ key for b in address.encode())]).hex()


def found(html: str, url: str = PAGE) -> dict[str, tuple[str, bool]]:
    return {e.address: (e.method, e.same_site) for e in extract_emails(html, url)}


class TestEmails:
    def test_a_mailto_link(self) -> None:
        html = '<a href="mailto:Front.Desk@clinic.example.com?subject=Hi">Email our front desk</a>'
        (email,) = extract_emails(html, PAGE)
        assert (email.address, email.method, email.same_site, email.label) == (
            "front.desk@clinic.example.com", "mailto", True, "Email our front desk",
        )  # fmt: skip
        assert email.page_url == PAGE

    def test_several_addresses_in_one_mailto(self) -> None:
        assert set(found('<a href="mailto:a@clinic.example.com,b@clinic.example.com">x</a>')) == {
            "a@clinic.example.com",
            "b@clinic.example.com",
        }

    def test_an_encoded_mailto(self) -> None:
        assert "dr.smith@clinic.example.com" in found(
            '<a href="mailto:dr.smith%40clinic.example.com">x</a>'
        )

    def test_visible_text(self) -> None:
        html = "<footer><p>Write to us at <b>Info@Clinic.Example.com</b>, or call 555-0100.</p></footer>"
        assert found(html) == {"info@clinic.example.com": ("text", True)}

    def test_text_next_to_punctuation(self) -> None:
        assert set(found("<p>(office@clinic.example.com), or: sales@clinic.example.com.</p>")) == {
            "office@clinic.example.com",
            "sales@clinic.example.com",
        }

    def test_a_cloudflare_protected_address_is_what_a_browser_shows(self) -> None:
        html = f'<a class="__cf_email__" data-cfemail="{cf_encode("dentist@clinic.example.com")}">[email&#160;protected]</a>'
        assert found(html) == {"dentist@clinic.example.com": ("cloudflare", True)}

    @pytest.mark.parametrize("encoded", ["", "zz", "42", "4200", "ff" * 3])
    def test_a_broken_protected_address_is_ignored_not_fatal(self, encoded: str) -> None:
        assert found(f'<span data-cfemail="{encoded}"></span>') == {}

    def test_json_ld(self) -> None:
        data = {
            "@type": "Dentist",
            "name": "Example",
            "contactPoint": [{"@type": "ContactPoint", "email": "appointments@clinic.example.com"}],
            "email": "mailto:hello@clinic.example.com",
        }
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        assert set(found(html)) == {"appointments@clinic.example.com", "hello@clinic.example.com"}

    @pytest.mark.parametrize("blob", ["", "{not json", "[]", "null", '{"email": 5}', "x" * 300_000])
    def test_unusable_json_ld_is_ignored(self, blob: str) -> None:
        assert found(f'<script type="application/ld+json">{blob}</script>') == {}

    @pytest.mark.parametrize(
        "text",
        [
            "reach us: info [at] clinic [dot] example [dot] com",
            "reach us: info(at)clinic(dot)example(dot)com",
            "reach us: info {AT} clinic {DOT} example {DOT} com",
        ],
    )
    def test_the_common_obfuscations_people_write_on_purpose(self, text: str) -> None:
        assert found(f"<p>{text}</p>") == {"info@clinic.example.com": ("obfuscated", True)}

    def test_the_best_method_wins_for_one_address(self) -> None:
        html = '<p>info@clinic.example.com</p><a href="mailto:info@clinic.example.com">mail us</a>'
        (email,) = extract_emails(html, PAGE)
        assert (email.method, email.label) == ("mailto", "mail us")

    def test_addresses_at_other_domains_are_kept_but_flagged(self) -> None:
        result = found(
            f'<a href="mailto:owner.clinic@{GMAIL}">x</a><a href="mailto:info@clinic.example.com">y</a>'
        )
        assert result == {
            f"owner.clinic@{GMAIL}": ("mailto", False),
            "info@clinic.example.com": ("mailto", True),
        }

    def test_the_businesses_own_domain_sorts_first_within_a_method(self) -> None:
        html = f'<a href="mailto:a@{GMAIL}">x</a><a href="mailto:z@clinic.example.com">y</a>'
        assert [e.address for e in extract_emails(html, PAGE)] == [
            "z@clinic.example.com",
            f"a@{GMAIL}",
        ]

    @pytest.mark.parametrize(
        "junk",
        [
            "<img src='logo" + "@2x.png'>",
            "<p>background" + "@3x.webp and sprite" + "@1x.svg</p>",
            "<p>you" + "@yourdomain.com</p>",
            "<p>name@example.com, user@example.org</p>",
            "<p>john.doe" + "@company.com</p>",
            "<p>abcdef0123456789abcdef0123456789" + "@sentry.io</p>",
            "<p>not an email@ nowhere</p>",
            "<p>@clinic.example.com</p>",
            "<p>a@b</p>",
        ],
    )
    def test_things_that_look_like_addresses_but_are_not_are_dropped(self, junk: str) -> None:
        assert found(junk) == {}

    def test_hidden_content_is_not_published_content(self) -> None:
        html = """
        <script>var contact = "secret@clinic.example.com";</script>
        <style>.x::after{content:"style@clinic.example.com"}</style>
        <!-- comment@clinic.example.com -->
        <noscript>noscript@clinic.example.com</noscript>
        <p>visible@clinic.example.com</p>"""
        assert set(found(html)) == {"visible@clinic.example.com"}

    def test_hostile_html_does_not_break_extraction(self) -> None:
        html = "<p>" * 5000 + "ok@clinic.example.com" + "<<<>>>&&&" + "</div>" * 3000
        assert "ok@clinic.example.com" in found(html)

    def test_a_huge_page_is_bounded(self) -> None:
        html = "<p>filler</p>" * 400_000 + "<p>late@clinic.example.com</p>"  # beyond the size cap
        assert "late@clinic.example.com" not in found(html)

    def test_nothing_is_ever_invented_from_a_name(self) -> None:
        html = "<h1>Dr. Pat Doe, DDS</h1><p>Founder of Example Dental, example.com</p>"
        assert extract_emails(html, PAGE) == []

    def test_empty_or_garbage_input(self) -> None:
        assert extract_emails("", PAGE) == []
        assert extract_emails("\x00\x01 not html", PAGE) == []


# ───────────────────────────── which pages to read next ─────────────────────────────

HOME = "https://clinic.example.com/"


def links(*hrefs: str, text: str = "link") -> str:
    return "".join(f'<a href="{h}">{text}</a>' for h in hrefs)


class TestCandidatePages:
    def test_contact_and_about_style_pages_on_the_same_site_are_found_best_first(self) -> None:
        html = links("/about-us", "/contact", "/blog/post-1", "/team", "/services")
        assert candidate_pages(html, HOME) == [
            "https://clinic.example.com/contact",
            "https://clinic.example.com/team",
            "https://clinic.example.com/about-us",
        ]

    def test_the_link_text_counts_too(self) -> None:
        html = '<a href="/p/17">Contact us</a><a href="/p/18">Pricing</a>'
        assert candidate_pages(html, HOME) == ["https://clinic.example.com/p/17"]

    def test_the_limit(self) -> None:
        html = links("/contact", "/team", "/about", "/staff", "/doctors", "/people", "/providers")
        assert len(candidate_pages(html, HOME, limit=3)) == 3

    def test_duplicates_fragments_and_trailing_slashes_collapse(self) -> None:
        html = links(
            "/contact", "/contact/", "/contact#form", "https://clinic.example.com/contact?x=1"
        )
        assert candidate_pages(html, HOME) == [
            "https://clinic.example.com/contact",
            "https://clinic.example.com/contact?x=1",
        ]

    def test_the_page_itself_is_not_a_candidate(self) -> None:
        assert candidate_pages(links("/contact"), "https://clinic.example.com/contact") == []

    @pytest.mark.parametrize(
        "href",
        [
            "https://other-site.example.net/contact",  # off-site
            "https://facebook.com/clinic/about",
            "mailto:info@clinic.example.com",
            "tel:" + "+1512" + "5550100",
            "javascript:alert(1)",
            "#contact",
            "/contact.pdf",
            "/team/photo.jpg",
            "http://169.254.169.254/contact",  # an internal address dressed as a link
            "http://127.0.0.1/about",
            "http://localhost/contact",
            "ftp://clinic.example.com/contact",
            "",
        ],
    )
    def test_off_site_unsafe_and_non_page_links_are_never_candidates(self, href: str) -> None:
        assert candidate_pages(links(href), HOME) == []

    def test_subdomains_of_the_same_site_are_the_same_site(self) -> None:
        assert candidate_pages(links("https://austin.clinic.example.com/contact"), HOME) == [
            "https://austin.clinic.example.com/contact"
        ]

    def test_relative_links_resolve_against_the_page(self) -> None:
        assert candidate_pages(links("contact"), "https://clinic.example.com/en/") == [
            "https://clinic.example.com/en/contact"
        ]

    def test_other_languages_are_recognised(self) -> None:
        html = links("/kontakt", "/impressum")
        assert set(candidate_pages(html, "https://praxis.example.de/")) == {
            "https://praxis.example.de/kontakt",
            "https://praxis.example.de/impressum",
        }

    def test_a_page_with_no_domain_has_no_candidates(self) -> None:
        assert candidate_pages(links("/contact"), "http://127.0.0.1/") == []
