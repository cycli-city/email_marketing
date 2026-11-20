"""The SSRF guard: every way we know of to make a server fetch something internal is refused."""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.integrations.web.ssrf import (
    MAX_URL_LENGTH,
    UnsafeUrlError,
    is_public_ip,
    resolve_public,
    system_resolver,
    validate_url,
)

PRIVATE_AND_SPECIAL = [
    "127.0.0.1",
    "127.1.2.3",
    "0.0.0.0",  # noqa: S104 - this is a test of refusing it
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.0.1",
    "192.168.255.255",
    "169.254.169.254",  # cloud metadata
    "169.254.0.1",
    "100.64.0.1",  # carrier-grade NAT
    "100.127.255.255",
    "192.0.0.1",
    "192.0.2.1",  # documentation range
    "198.18.0.1",  # benchmarking
    "198.51.100.7",
    "203.0.113.9",
    "224.0.0.1",  # multicast
    "239.255.255.250",
    "240.0.0.1",  # reserved
    "255.255.255.255",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd12:3456:789a::1",
    "ff02::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.1.2.3",
    "::ffff:169.254.169.254",
    "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
    "2002:0a00:0001::",  # 6to4 wrapping 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 wrapping 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "2001:db8::1",  # documentation
]

PUBLIC = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "2a00:1450:4001:81b::200e",
    "::ffff:8.8.8.8",
]


class TestAddresses:
    @pytest.mark.parametrize("address", PRIVATE_AND_SPECIAL)
    def test_internal_and_special_addresses_are_not_public(self, address: str) -> None:
        assert is_public_ip(address) is False

    @pytest.mark.parametrize("address", PUBLIC)
    def test_ordinary_internet_addresses_are_public(self, address: str) -> None:
        assert is_public_ip(address) is True

    @pytest.mark.parametrize(
        "junk", ["", "not-an-ip", "127.0.0.1.5", "1.2.3", "fe80::1%eth0", "999.1.1.1"]
    )
    def test_anything_that_is_not_an_address_is_not_public(self, junk: str) -> None:
        assert is_public_ip(junk) is False

    @given(
        st.ip_addresses(v=4, network="10.0.0.0/8")
        | st.ip_addresses(v=4, network="192.168.0.0/16")
        | st.ip_addresses(v=4, network="127.0.0.0/8")
    )
    def test_property_a_private_ipv4_is_never_public_however_it_is_wrapped(
        self, v4: ipaddress.IPv4Address
    ) -> None:
        mapped = f"::ffff:{v4}"
        nat64 = str(ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) + int(v4)))
        for candidate in (str(v4), mapped, nat64):
            assert is_public_ip(candidate) is False


class TestUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.org/",
            "file:///etc/passwd",
            "gopher://example.org/",
            "javascript:alert(1)",
            "data:text/html,hi",
            "//example.org/path",  # no scheme
            "example.org",  # no scheme
        ],
    )
    def test_only_http_and_https(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError):
            validate_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://127.0.0.1:8000/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1]/",
            "http://10.0.0.5/",
            "https://192.168.1.1/",
            "http://0.0.0.0/",
            "http://[2002:7f00:1::]/",
        ],
    )
    def test_internal_ip_literals_are_refused_before_any_lookup(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError) as raised:
            validate_url(url)
        assert raised.value.reason in {"address_not_public", "port_not_allowed", "malformed_url"}

    @pytest.mark.parametrize(
        "url",
        [
            "http://2130706433/",  # 127.0.0.1 as one decimal number
            "http://0x7f000001/",
            "http://0x7f.0.0.1/",
            "http://017700000001/",  # octal
            "http://127.1/",  # short form
            "http://0177.0.0.1/",
            "http://1.1/",
        ],
    )
    def test_alternative_spellings_of_an_ip_are_refused_outright(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError) as raised:
            validate_url(url)
        assert raised.value.reason == "numeric_hostname"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/",
            "http://LOCALHOST./",
            "http://foo.localhost/",
            "http://printer.local/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://db.internal/",
            "http://intranet/",  # single label
            "http://router.lan/",
            "http://x.home.arpa/",
            "http://hidden.onion/",
        ],
    )
    def test_reserved_and_single_label_hostnames_are_refused(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError):
            validate_url(url)

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("http://user:pass@example.org/", "credentials_in_url"),
            ("http://example.org@evil.example.net/", "credentials_in_url"),
            ("https://example.org:8443/", "port_not_allowed"),
            ("http://example.org:22/", "port_not_allowed"),
            ("http://example.org:5432/", "port_not_allowed"),
            ("http://example.org:abc/", "malformed_url"),
            ("http://exa mple.org/", "illegal_characters"),
            ("http://example.org/\r\nHost: evil", "illegal_characters"),
            ("http://example.org\\@evil.net/", "illegal_characters"),
            ("", "empty_url"),
            ("   ", "empty_url"),
            ("http://" + "a" * 300 + ".org/", "bad_hostname"),
            ("http://exa..mple.org/", "bad_hostname"),
            ("http://exam_ple.org/", "bad_hostname"),
            ("https://example.org/" + "x" * MAX_URL_LENGTH, "url_too_long"),
        ],
    )
    def test_other_ways_to_smuggle_a_target(self, url: str, reason: str) -> None:
        with pytest.raises(UnsafeUrlError) as raised:
            validate_url(url)
        assert raised.value.reason == reason

    def test_ordinary_urls_pass_and_are_normalised(self) -> None:
        target = validate_url("  HTTPS://WWW.Example.COM./About?x=1#team  ")
        assert target.url == "https://www.example.com/About?x=1"
        assert (target.scheme, target.host, target.port, target.is_ip_literal) == (
            "https",
            "www.example.com",
            443,
            False,
        )
        assert target.origin == "https://www.example.com"

    def test_internationalised_names_become_punycode(self) -> None:
        assert validate_url("https://münchen.example.com/").host == "xn--mnchen-3ya.example.com"

    def test_a_public_ip_literal_is_allowed(self) -> None:
        target = validate_url("http://93.184.216.34:80/x")
        assert target.is_ip_literal is True
        assert target.host == "93.184.216.34"

    def test_the_origin_shows_a_non_default_port(self) -> None:
        assert validate_url("http://example.com:80/").origin == "http://example.com"
        assert validate_url("https://example.com:443/").origin == "https://example.com"


class TestResolution:
    @staticmethod
    def resolver(*answers: str):  # type: ignore[no-untyped-def]
        async def resolve(host: str, port: int) -> list[str]:
            return list(answers)

        return resolve

    async def test_a_name_resolving_to_public_addresses_is_fine(self) -> None:
        target = validate_url("https://shop.example.com/")
        assert await resolve_public(
            target, self.resolver("93.184.216.34", "2606:2800:220:1::1")
        ) == [
            "93.184.216.34",
            "2606:2800:220:1::1",
        ]

    @pytest.mark.parametrize("bad", PRIVATE_AND_SPECIAL)
    async def test_a_name_resolving_to_an_internal_address_is_refused(self, bad: str) -> None:
        target = validate_url("https://innocent-looking.example.com/")
        with pytest.raises(UnsafeUrlError) as raised:
            await resolve_public(target, self.resolver(bad))
        assert raised.value.reason == "resolves_to_non_public_address"

    async def test_a_mixed_answer_is_a_rebinding_attempt_and_is_refused(self) -> None:
        target = validate_url("https://rebind.example.com/")
        with pytest.raises(UnsafeUrlError, match="non_public"):
            await resolve_public(target, self.resolver("93.184.216.34", "127.0.0.1"))

    async def test_an_empty_answer_is_refused(self) -> None:
        with pytest.raises(UnsafeUrlError, match="dns_failure"):
            await resolve_public(validate_url("https://nowhere.example.com/"), self.resolver())

    async def test_an_ip_literal_is_not_looked_up(self) -> None:
        async def boom(host: str, port: int) -> list[str]:
            raise AssertionError("must not resolve an IP literal")

        assert await resolve_public(validate_url("http://93.184.216.34/"), boom) == [
            "93.184.216.34"
        ]

    async def test_the_system_resolver_reports_an_unknown_name_as_a_refusal(self) -> None:
        with pytest.raises(UnsafeUrlError, match="dns_failure"):
            await system_resolver("this-name-does-not-exist.invalid", 443)
