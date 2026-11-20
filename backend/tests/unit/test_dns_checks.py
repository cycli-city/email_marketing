"""Free, certain rejections (no mail server, null MX) and — just as important — never rejecting on a resolver hiccup."""

from __future__ import annotations

from collections.abc import Mapping

import dns.exception
import dns.resolver
import pytest

from app.integrations.dns_checks import mail_status, usable_for_mail


def lookup(table: Mapping[tuple[str, str], list[str] | Exception]):  # type: ignore[no-untyped-def]
    async def resolve(domain: str, record_type: str) -> list[str]:
        answer = table.get((domain, record_type), boom(dns.resolver.NoAnswer))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return resolve


def boom(kind: type[Exception]) -> Exception:
    return kind()


class TestMailStatus:
    async def test_a_domain_with_a_mail_server(self) -> None:
        assert (
            await mail_status(
                "clinic.example.com",
                lookup({("clinic.example.com", "MX"): ["10 mx1.example.net."]}),
            )
            == "mx"
        )

    async def test_several_servers(self) -> None:
        records = ["10 mx1.example.net.", "20 mx2.example.net."]
        assert (
            await mail_status("clinic.example.com", lookup({("clinic.example.com", "MX"): records}))
            == "mx"
        )

    async def test_no_mx_but_an_address_record_falls_back_to_the_host_itself(self) -> None:
        table: dict[tuple[str, str], list[str] | Exception] = {
            ("clinic.example.com", "MX"): boom(dns.resolver.NoAnswer),
            ("clinic.example.com", "A"): ["93.184.216.34"],
        }
        assert await mail_status("clinic.example.com", lookup(table)) == "implicit_mx"

    async def test_an_ipv6_only_host_counts_too(self) -> None:
        table: dict[tuple[str, str], list[str] | Exception] = {
            ("clinic.example.com", "A"): boom(dns.resolver.NoAnswer),
            ("clinic.example.com", "AAAA"): ["2606:2800:220:1::1"],
        }
        assert await mail_status("clinic.example.com", lookup(table)) == "implicit_mx"

    async def test_a_null_mx_means_the_domain_never_receives_mail(self) -> None:
        assert (
            await mail_status("example.com", lookup({("example.com", "MX"): ["0 ."]})) == "null_mx"
        )

    async def test_a_domain_that_does_not_exist(self) -> None:
        assert (
            await mail_status(
                "no-such-business.example.com",
                lookup({("no-such-business.example.com", "MX"): boom(dns.resolver.NXDOMAIN)}),
            )
            == "no_mail_server"
        )

    async def test_a_domain_with_neither_mx_nor_address(self) -> None:
        assert await mail_status("parked.example.com", lookup({})) == "no_mail_server"

    @pytest.mark.parametrize(
        "error",
        [
            boom(dns.resolver.NoNameservers),
            boom(dns.exception.Timeout),
            boom(dns.resolver.LifetimeTimeout),
        ],
    )
    async def test_a_resolver_problem_is_could_not_tell_never_a_rejection(
        self, error: Exception
    ) -> None:
        assert (
            await mail_status("clinic.example.com", lookup({("clinic.example.com", "MX"): error}))
            == "dns_error"
        )

    async def test_a_resolver_problem_while_falling_back_is_also_could_not_tell(self) -> None:
        table: dict[tuple[str, str], list[str] | Exception] = {
            ("clinic.example.com", "MX"): boom(dns.resolver.NoAnswer),
            ("clinic.example.com", "A"): boom(dns.exception.Timeout),
        }
        assert await mail_status("clinic.example.com", lookup(table)) == "dns_error"

    @pytest.mark.parametrize("junk", ["", "  ", "localhost", "."])
    async def test_something_that_is_not_a_domain_has_no_mail_server(self, junk: str) -> None:
        assert await mail_status(junk, lookup({})) == "no_mail_server"

    async def test_case_and_trailing_dots_do_not_matter(self) -> None:
        assert (
            await mail_status(
                " Clinic.Example.COM. ",
                lookup({("clinic.example.com", "MX"): ["10 mx.example.net."]}),
            )
            == "mx"
        )


class TestUsable:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("mx", True),
            ("implicit_mx", True),
            ("null_mx", False),
            ("no_mail_server", False),
            ("dns_error", None),
        ],
    )
    def test_only_a_conclusive_lookup_decides(self, status: str, expected: bool | None) -> None:
        assert usable_for_mail(status) is expected  # type: ignore[arg-type]
