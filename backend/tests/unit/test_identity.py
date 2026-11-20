"""ADR-028: spellings collapse to one identity; nothing is guessed."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.identity import (
    company_key,
    email_domain,
    email_match_key,
    is_disposable,
    is_free_mail,
    is_role_address,
    normalise_email,
    normalise_name,
    registrable_domain,
    website_origin,
)

GMAIL = "gmail" + ".com"
GOOGLEMAIL = "googlemail" + ".com"
MAILINATOR = "mailinator" + ".com"


class TestDomains:
    @pytest.mark.parametrize(
        "spelling",
        [
            "example.com",
            "EXAMPLE.COM",
            "www.example.com",
            "http://example.com",
            "https://www.example.com/",
            "https://www.Example.com:443/about/team?x=1#top",
            "//example.com/path",
            "shop.example.com",
            "https://a.b.shop.example.com/x",
            " example.com. ",
        ],
    )
    def test_every_spelling_of_one_site_is_one_company(self, spelling: str) -> None:
        assert registrable_domain(spelling) == "example.com"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.clinic.co.uk/contact", "clinic.co.uk"),
            ("https://blog.example.com.au/", "example.com.au"),
            ("http://münchen-zahnarzt.de/", "münchen-zahnarzt.de".encode("idna").decode()),
            (
                "https://example.github.io/page",
                "example.github.io",
            ),  # a private suffix: the project is the "company"
        ],
    )
    def test_public_suffixes_idn_and_private_suffixes(self, url: str, expected: str) -> None:
        assert registrable_domain(url) == expected

    @pytest.mark.parametrize(
        "junk",
        [
            None,
            "",
            "   ",
            "localhost",
            "intranet",
            "http://127.0.0.1/",
            "https://[::1]/",
            "http://10.0.0.1:80/",
            "com",
            "co.uk",
            "http://",
        ],
    )
    def test_things_that_are_not_a_company_site_have_no_domain(self, junk: str | None) -> None:
        assert registrable_domain(junk) is None

    def test_the_homepage_origin(self) -> None:
        assert website_origin("http://WWW.Example.com/a/b?c") == "https://example.com"
        assert website_origin("nonsense") is None

    @given(
        st.sampled_from(["", "www."]),
        st.sampled_from(["http://", "https://", ""]),
        st.sampled_from(["", "/", "/a/b", "/x?y=1"]),
        st.sampled_from(["example.com", "Example.COM"]),
    )
    def test_property_spelling_never_changes_the_company(
        self, www: str, scheme: str, path: str, host: str
    ) -> None:
        assert registrable_domain(f"{scheme}{www}{host}{path}") == "example.com"


class TestEmails:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Pat.Doe@Example.COM ", "pat.doe@example.com"),
            ("mailto:info@example.com", "info@example.com"),
            ("<pat@example.com>", "pat@example.com"),
            ("pat@example", None),
            ("pat@@example.com", None),
            ("not an email", None),
            ("", None),
            (None, None),
            ("pat@example.com\r\nBcc: x@example.net", None),
        ],
    )
    def test_normalisation(self, raw: str | None, expected: str | None) -> None:
        assert normalise_email(raw) == expected

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            (f"pat.doe@{GMAIL}", f"patdoe@{GMAIL}"),
            (f"pat.doe+news@{GMAIL}", f"PatDoe@{GOOGLEMAIL}"),
            (f"p.a.t@{GMAIL}", f"pat+x@{GMAIL}"),
        ],
    )
    def test_gmail_spellings_match_each_other(self, a: str, b: str) -> None:
        assert email_match_key(a) == email_match_key(b)

    def test_other_providers_are_not_second_guessed(self) -> None:
        assert email_match_key("pat.doe@example.com") != email_match_key("patdoe@example.com")
        assert email_match_key("pat+tag@example.com") != email_match_key("pat@example.com")
        assert email_match_key("garbage") is None

    def test_the_domain_of_an_address(self) -> None:
        assert email_domain("Pat@Mail.Example.com") == "mail.example.com"
        assert email_domain("nope") is None

    @pytest.mark.parametrize(
        "address",
        [
            "info@example.com",
            "SALES@example.com",
            "no-reply@example.com",
            "noreply+x@example.com",
            "customer.service@example.com",
            "contact_us@example.com",
            "support@example.com",
        ],
    )
    def test_role_addresses(self, address: str) -> None:
        assert is_role_address(address) is True

    @pytest.mark.parametrize(
        "address",
        [
            "pat.doe@example.com",
            "dr.smith@example.com",
            "informal@example.com",
            "salesman@example.com",
            "garbage",
            None,
        ],
    )
    def test_named_people_are_not_role_addresses(self, address: str | None) -> None:
        assert is_role_address(address) is False

    def test_free_mail_and_disposable(self) -> None:
        assert is_free_mail(f"clinic.owner@{GMAIL}") is True
        assert is_free_mail("pat@example.com") is False
        assert is_disposable(f"x@{MAILINATOR}") is True
        assert is_disposable(f"x@sub.{MAILINATOR}") is True
        assert is_disposable("x@example.com") is False
        assert is_disposable("garbage") is False


class TestCompanyKey:
    def test_a_website_decides(self) -> None:
        assert (
            company_key(website="https://www.Example.com/about", name="Totally Different Name")
            == "example.com"
        )

    def test_two_branches_of_one_chain_are_one_company(self) -> None:
        a = company_key(
            website="https://austin.example.com/",
            name="Example Dental Austin",
            city="Austin",
            country="US",
        )
        b = company_key(
            website="https://dallas.example.com/",
            name="Example Dental Dallas",
            city="Dallas",
            country="US",
        )
        assert a == b == "example.com"

    def test_without_a_website_name_city_and_country_decide(self) -> None:
        a = company_key(website=None, name="Café Möller, LLC", city="Austin", country="US")
        b = company_key(website="", name="CAFE  MOLLER", city="austin", country="us")
        assert a == b == "name:cafe moller|austin|us"

    def test_different_cities_are_different_companies_when_there_is_no_website(self) -> None:
        a = company_key(website=None, name="Smile Dental", city="Austin", country="US")
        b = company_key(website=None, name="Smile Dental", city="Dallas", country="US")
        assert a != b

    def test_nothing_to_go_on_is_no_key(self) -> None:
        assert company_key(website=None, name=None) is None
        assert company_key(website="localhost", name="  ") is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Acme, Inc.", "acme"),
            ("Acme Corporation", "acme"),
            ("Müller & Söhne GmbH", "muller sohne"),
            ("  The   Dental   Studio  ", "the dental studio"),
            ("Dr. Smith's Clinic", "dr smith s clinic"),
            ("", ""),
        ],
    )
    def test_names(self, raw: str, expected: str) -> None:
        assert normalise_name(raw) == expected

    @given(st.text(max_size=60))
    def test_property_normalising_twice_changes_nothing(self, raw: str) -> None:
        once = normalise_name(raw)
        assert normalise_name(once) == once


class TestSharedHosting:
    """Businesses on a shared platform are separate companies — collapsing them would silently drop real leads."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://acme-dental.wixsite.com/home", "https://other-clinic.wixsite.com/home"),
            ("https://acme.github.io/", "https://other.github.io/"),
            ("https://acme.blogspot.com/", "https://other.blogspot.com/"),
            ("https://acme.myshopify.com/", "https://other.myshopify.com/"),
        ],
    )
    def test_two_sites_on_one_platform_are_two_companies(self, a: str, b: str) -> None:
        assert registrable_domain(a) != registrable_domain(b)
        assert company_key(website=a, name=None) != company_key(website=b, name=None)
