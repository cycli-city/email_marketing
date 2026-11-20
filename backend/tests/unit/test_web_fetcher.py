"""The safe fetcher: connects only to the address it checked, follows redirects by hand, obeys robots.txt, stays bounded."""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.integrations.web.fetcher import (
    FetchError,
    FetchLimits,
    SafeFetcher,
    build_crawler_client,
)

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"
Reply = tuple[int, dict[str, str], bytes]


def html(body: str = "<html><body>hello</body></html>") -> Reply:
    return 200, {"content-type": "text/html; charset=utf-8"}, body.encode()


@dataclass
class Web:
    """A tiny fake internet: sites by Host header, DNS by name; remembers exactly what reached the wire."""

    sites: dict[str, dict[str, Reply]] = field(default_factory=dict)
    dns: dict[str, list[str]] = field(default_factory=dict)
    seen: list[httpx.Request] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    now: float = 1000.0

    async def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return self.dns.get(host, [PUBLIC_A])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host = request.headers["host"].split(":")[0]
        reply = self.sites.get(host, {}).get(request.url.path)
        if reply is None:
            return httpx.Response(404, text="not found")
        status, headers, body = reply
        return httpx.Response(status, headers=headers, content=body)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def fetcher(self, **limits: Any) -> SafeFetcher:
        return SafeFetcher(
            client=build_crawler_client(httpx.MockTransport(self.handle)),
            resolver=self.resolve,
            limits=FetchLimits(**limits),
            sleep=self.sleep,
            monotonic=self.monotonic,
        )

    def pages(self) -> list[httpx.Request]:
        return [r for r in self.seen if r.url.path != "/robots.txt"]


def site(web: Web, host: str = "shop.example.com", /, **paths: Reply) -> Web:
    web.sites[host] = {
        "/" + key.lstrip("_").replace("__", "/"): value for key, value in paths.items()
    }
    web.sites[host].setdefault("/robots.txt", (404, {}, b""))
    web.sites[host].setdefault("/", html())
    return web


@pytest.fixture
def web() -> Web:
    return Web()


# ───────────────────────────── the happy path, and the pinning ─────────────────────────────


class TestFetching:
    async def test_fetches_a_page(self, web: Web) -> None:
        site(web, **{"": html("<p>Welcome to Acme</p>")})
        page = await web.fetcher().fetch("https://shop.example.com/")
        assert page.status == 200
        assert page.content_type == "text/html"
        assert "Welcome to Acme" in page.text
        assert page.truncated is False
        assert page.url == "https://shop.example.com/"

    async def test_it_connects_to_the_address_it_checked_and_keeps_the_name_for_the_host_and_certificate(
        self, web: Web
    ) -> None:
        site(web)
        web.dns["shop.example.com"] = [PUBLIC_B]
        await web.fetcher().fetch("https://shop.example.com/")
        request = web.pages()[0]
        assert request.url.host == PUBLIC_B  # the pinned address, not the name
        assert request.headers["host"] == "shop.example.com"
        assert (
            request.extensions["sni_hostname"] == "shop.example.com"
        )  # certificate is verified against the NAME

    async def test_a_name_is_resolved_once_per_request_so_the_answer_cannot_change_between_check_and_use(
        self, web: Web
    ) -> None:
        site(web)
        await web.fetcher().fetch("https://shop.example.com/")
        assert web.lookups == [
            "shop.example.com",
            "shop.example.com",
        ]  # robots.txt + the page, one lookup each

    async def test_an_ipv6_address_is_pinned_too(self, web: Web) -> None:
        site(web)
        web.dns["shop.example.com"] = ["2606:2800:220:1:248:1893:25c8:1946"]
        await web.fetcher().fetch("https://shop.example.com/")
        assert web.pages()[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"
        assert web.pages()[0].headers["host"] == "shop.example.com"

    async def test_a_non_default_port_is_kept_in_the_host_header(self, web: Web) -> None:
        site(web)
        await web.fetcher().fetch("http://shop.example.com:80/")
        assert web.pages()[0].headers["host"] == "shop.example.com"

    async def test_plain_http_sites_work_and_send_no_sni(self, web: Web) -> None:
        site(web)
        await web.fetcher().fetch("http://shop.example.com/")
        assert "sni_hostname" not in web.pages()[0].extensions

    async def test_the_user_agent_is_generic(self, web: Web) -> None:
        site(web)
        await web.fetcher().fetch("https://shop.example.com/")
        assert web.pages()[0].headers["user-agent"].startswith("aoi-outreach/")

    async def test_no_cookies_are_kept_between_requests(self, web: Web) -> None:
        site(
            web,
            **{
                "": (
                    200,
                    {"content-type": "text/html", "set-cookie": "session=abc; Path=/"},
                    b"<p>x</p>",
                )
            },
        )
        web.sites["shop.example.com"]["/other"] = html()
        fetcher = web.fetcher(default_delay_seconds=0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://shop.example.com/other")
        assert all("cookie" not in r.headers for r in web.seen)

    def test_the_client_never_follows_redirects_or_reads_proxy_settings(self) -> None:
        client = build_crawler_client()
        assert client.follow_redirects is False
        assert client.trust_env is False


# ───────────────────────────── the attacks ─────────────────────────────


class TestSsrf:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:80/",
            "http://localhost/",
            "http://[::1]/",
            "http://2130706433/",
            "file:///etc/passwd",
            "http://user:pw@shop.example.com/",
            "http://shop.example.com:6379/",
        ],
    )
    async def test_a_dangerous_url_is_refused_and_nothing_goes_on_the_wire(
        self, web: Web, url: str
    ) -> None:
        site(web)
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch(url)
        assert raised.value.unsafe is True
        assert web.seen == []
        assert web.lookups == []

    async def test_a_name_that_resolves_to_an_internal_address_is_refused(self, web: Web) -> None:
        site(web)
        web.dns["shop.example.com"] = ["10.0.0.7"]
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch("https://shop.example.com/")
        assert (raised.value.reason, raised.value.unsafe) == (
            "resolves_to_non_public_address",
            True,
        )
        assert web.seen == []

    async def test_dns_rebinding_a_mixed_answer_is_refused(self, web: Web) -> None:
        site(web)
        web.dns["shop.example.com"] = [PUBLIC_A, "127.0.0.1"]
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch("https://shop.example.com/")
        assert raised.value.unsafe is True
        assert web.seen == []

    @pytest.mark.parametrize(
        "location",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://127.0.0.1:80/admin",
            "http://localhost/",
            "http://[::1]/",
            "file:///etc/passwd",
            "gopher://shop.example.com/",
        ],
    )
    async def test_a_redirect_into_an_internal_target_is_refused_and_never_followed(
        self, web: Web, location: str
    ) -> None:
        site(web, **{"": (302, {"location": location}, b"")})
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch("https://shop.example.com/")
        assert raised.value.unsafe is True
        assert [r.url.host for r in web.pages()] == [PUBLIC_A]  # only the first hop ever went out

    async def test_a_redirect_to_a_name_that_resolves_internally_is_refused(self, web: Web) -> None:
        site(web, **{"": (301, {"location": "https://sneaky.example.net/"}, b"")})
        web.dns["sneaky.example.net"] = ["192.168.1.1"]
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch("https://shop.example.com/")
        assert (raised.value.reason, raised.value.unsafe) == (
            "resolves_to_non_public_address",
            True,
        )
        assert len(web.pages()) == 1


# ───────────────────────────── redirects ─────────────────────────────


class TestRedirects:
    async def test_follows_relative_and_absolute_redirects_and_reports_the_trail(
        self, web: Web
    ) -> None:
        site(web, **{"": (301, {"location": "/home"}, b"")})
        web.sites["shop.example.com"]["/home"] = (
            302,
            {"location": "https://www.example.com/welcome"},
            b"",
        )
        site(web, "www.example.com", welcome=html("<p>final</p>"))
        page = await web.fetcher(default_delay_seconds=0).fetch("https://shop.example.com/")
        assert "final" in page.text
        assert page.url == "https://www.example.com/welcome"
        assert page.redirected_from == (
            "https://shop.example.com/home",
            "https://www.example.com/welcome",
        )

    async def test_each_hop_is_resolved_and_pinned_separately(self, web: Web) -> None:
        site(web, **{"": (301, {"location": "https://www.example.com/"}, b"")})
        site(web, "www.example.com")
        web.dns["www.example.com"] = [PUBLIC_B]
        await web.fetcher(default_delay_seconds=0).fetch("https://shop.example.com/")
        assert [r.url.host for r in web.pages()] == [PUBLIC_A, PUBLIC_B]

    async def test_a_redirect_loop_stops(self, web: Web) -> None:
        site(web, **{"": (302, {"location": "/"}, b"")})
        with pytest.raises(FetchError, match="too_many_redirects"):
            await web.fetcher(default_delay_seconds=0, max_redirects=3).fetch(
                "https://shop.example.com/"
            )
        assert len(web.pages()) == 4  # the first request plus three redirects

    async def test_a_redirect_without_a_location_is_an_error(self, web: Web) -> None:
        site(web, **{"": (302, {}, b"")})
        with pytest.raises(FetchError, match="redirect_without_location"):
            await web.fetcher().fetch("https://shop.example.com/")


# ───────────────────────────── robots.txt ─────────────────────────────


def robots(web: Web, text: str, host: str = "shop.example.com") -> None:
    site(web, host)
    web.sites[host]["/robots.txt"] = (200, {"content-type": "text/plain"}, text.encode())


class TestRobots:
    async def test_a_disallowed_path_is_not_fetched(self, web: Web) -> None:
        robots(web, "User-agent: *\nDisallow: /private\n")
        web.sites["shop.example.com"]["/private/team"] = html()
        with pytest.raises(FetchError, match="blocked_by_robots"):
            await web.fetcher().fetch("https://shop.example.com/private/team")
        assert web.pages() == []

    async def test_an_allowed_path_on_the_same_site_is_fetched(self, web: Web) -> None:
        robots(web, "User-agent: *\nDisallow: /private\n")
        page = await web.fetcher().fetch("https://shop.example.com/")
        assert page.status == 200

    async def test_rules_for_our_own_user_agent_win(self, web: Web) -> None:
        robots(web, "User-agent: *\nAllow: /\n\nUser-agent: aoi-outreach\nDisallow: /\n")
        with pytest.raises(FetchError, match="blocked_by_robots"):
            await web.fetcher().fetch("https://shop.example.com/")

    async def test_a_site_that_bans_everyone_is_left_alone(self, web: Web) -> None:
        robots(web, "User-agent: *\nDisallow: /\n")
        with pytest.raises(FetchError, match="blocked_by_robots"):
            await web.fetcher().fetch("https://shop.example.com/")

    async def test_a_missing_robots_file_means_no_restrictions(self, web: Web) -> None:
        site(web)  # robots.txt -> 404
        assert (await web.fetcher().fetch("https://shop.example.com/")).status == 200

    @pytest.mark.parametrize("status", [500, 502, 503, 429])
    async def test_an_unreachable_robots_file_forbids_everything_for_now(
        self, web: Web, status: int
    ) -> None:
        site(web)
        web.sites["shop.example.com"]["/robots.txt"] = (status, {}, b"")
        with pytest.raises(FetchError, match="blocked_by_robots"):
            await web.fetcher().fetch("https://shop.example.com/")
        assert web.pages() == []

    async def test_a_robots_failure_is_retried_after_a_while_not_remembered_forever(
        self, web: Web
    ) -> None:
        site(web)
        web.sites["shop.example.com"]["/robots.txt"] = (503, {}, b"")
        fetcher = web.fetcher(robots_failure_ttl_seconds=600)
        with pytest.raises(FetchError):
            await fetcher.fetch("https://shop.example.com/")
        web.sites["shop.example.com"]["/robots.txt"] = (404, {}, b"")
        web.now += 601
        assert (await fetcher.fetch("https://shop.example.com/")).status == 200

    async def test_robots_is_fetched_once_and_cached(self, web: Web) -> None:
        robots(web, "User-agent: *\nDisallow: /nothing\n")
        web.sites["shop.example.com"]["/a"] = html()
        fetcher = web.fetcher(default_delay_seconds=0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://shop.example.com/a")
        assert len([r for r in web.seen if r.url.path == "/robots.txt"]) == 1

    async def test_robots_itself_cannot_be_used_to_reach_an_internal_address(
        self, web: Web
    ) -> None:
        site(web)
        web.sites["shop.example.com"]["/robots.txt"] = (
            301,
            {"location": "http://169.254.169.254/robots.txt"},
            b"",
        )
        with pytest.raises(FetchError, match="blocked_by_robots"):  # unreadable ⇒ forbidden
            await web.fetcher().fetch("https://shop.example.com/")
        assert all(r.url.host != "169.254.169.254" for r in web.seen)

    async def test_robots_can_be_skipped_only_when_the_caller_says_so(self, web: Web) -> None:
        robots(web, "User-agent: *\nDisallow: /\n")
        page = await web.fetcher().fetch("https://shop.example.com/", respect_robots=False)
        assert page.status == 200


# ───────────────────────────── politeness ─────────────────────────────


class TestPoliteness:
    async def test_requests_to_one_host_are_spaced_out(self, web: Web) -> None:
        site(web)
        web.sites["shop.example.com"]["/a"] = html()
        fetcher = web.fetcher(default_delay_seconds=2.0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://shop.example.com/a")
        assert web.sleeps
        assert web.sleeps[0] == pytest.approx(2.0)

    async def test_different_hosts_do_not_wait_for_each_other(self, web: Web) -> None:
        site(web)
        site(web, "other.example.org")
        fetcher = web.fetcher(default_delay_seconds=5.0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://other.example.org/")
        assert web.sleeps == []

    async def test_a_crawl_delay_in_robots_is_respected_but_capped(self, web: Web) -> None:
        robots(web, "User-agent: *\nCrawl-delay: 4\n")
        web.sites["shop.example.com"]["/a"] = html()
        fetcher = web.fetcher(default_delay_seconds=1.0, max_crawl_delay_seconds=10.0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://shop.example.com/a")
        assert web.sleeps[0] == pytest.approx(4.0)

    async def test_an_absurd_crawl_delay_is_capped(self, web: Web) -> None:
        robots(web, "User-agent: *\nCrawl-delay: 3600\n")
        web.sites["shop.example.com"]["/a"] = html()
        fetcher = web.fetcher(default_delay_seconds=1.0, max_crawl_delay_seconds=10.0)
        await fetcher.fetch("https://shop.example.com/")
        await fetcher.fetch("https://shop.example.com/a")
        assert web.sleeps[0] == pytest.approx(10.0)


# ───────────────────────────── bounds ─────────────────────────────


class TestBounds:
    async def test_a_large_page_is_cut_at_the_cap_and_flagged(self, web: Web) -> None:
        site(web, **{"": html("x" * 5000)})
        page = await web.fetcher(max_bytes=1000).fetch("https://shop.example.com/")
        assert (len(page.text), page.truncated) == (1000, True)

    async def test_a_page_exactly_at_the_cap_is_not_flagged(self, web: Web) -> None:
        site(web, **{"": html("y" * 1000)})
        page = await web.fetcher(max_bytes=1000).fetch("https://shop.example.com/")
        assert (len(page.text), page.truncated) == (1000, False)

    async def test_a_compression_bomb_is_cut_off_at_the_cap_not_expanded(self, web: Web) -> None:
        bomb = gzip.compress(
            b"0" * 200_000_000, compresslevel=9
        )  # ~200 KB on the wire, 200 MB inflated
        assert len(bomb) < 400_000
        site(web, **{"": (200, {"content-type": "text/html", "content-encoding": "gzip"}, bomb)})
        page = await web.fetcher(max_bytes=100_000).fetch("https://shop.example.com/")
        assert (len(page.text), page.truncated) == (100_000, True)

    @pytest.mark.parametrize(
        "content_type", ["application/pdf", "image/png", "application/octet-stream", "video/mp4"]
    )
    async def test_only_text_is_accepted(self, web: Web, content_type: str) -> None:
        site(web, **{"": (200, {"content-type": content_type}, b"%PDF-1.7 ...")})
        with pytest.raises(FetchError, match="unsupported_content_type"):
            await web.fetcher().fetch("https://shop.example.com/")

    @pytest.mark.parametrize(
        "content_type", ["text/html", "text/plain", "application/xhtml+xml", "text/xml"]
    )
    async def test_text_types_are_accepted(self, web: Web, content_type: str) -> None:
        site(web, **{"": (200, {"content-type": content_type}, b"<p>ok</p>")})
        assert (await web.fetcher().fetch("https://shop.example.com/")).text == "<p>ok</p>"

    async def test_a_missing_content_type_is_treated_as_html(self, web: Web) -> None:
        site(web, **{"": (200, {}, b"<p>ok</p>")})
        assert (await web.fetcher().fetch("https://shop.example.com/")).content_type == "text/html"

    async def test_the_declared_charset_is_used_and_garbage_does_not_raise(self, web: Web) -> None:
        site(
            web,
            **{
                "": (
                    200,
                    {"content-type": "text/html; charset=iso-8859-1"},
                    "café".encode("latin-1"),
                )
            },
        )
        assert (await web.fetcher().fetch("https://shop.example.com/")).text == "café"
        site(
            web,
            "odd.example.com",
            **{"": (200, {"content-type": "text/html; charset=nonsense-9"}, b"caf\xc3\xa9")},
        )
        assert (await web.fetcher().fetch("https://odd.example.com/")).text == "café"

    @pytest.mark.parametrize("status", [403, 404, 410, 500, 503])
    async def test_http_errors_are_reported_with_their_status(self, web: Web, status: int) -> None:
        site(web, **{"": (status, {"content-type": "text/html"}, b"nope")})
        with pytest.raises(FetchError) as raised:
            await web.fetcher().fetch("https://shop.example.com/")
        assert (raised.value.reason, raised.value.status) == ("http_error", status)

    async def test_timeouts_and_connection_failures_are_reported_not_raised_raw(
        self, web: Web
    ) -> None:
        site(web)

        def slow(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ReadTimeout("too slow", request=request)

        def refused(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ConnectError("refused", request=request)

        for handler, reason in ((slow, "timeout"), (refused, "network_error")):
            fetcher = SafeFetcher(
                client=build_crawler_client(httpx.MockTransport(handler)),
                resolver=web.resolve,
                limits=FetchLimits(default_delay_seconds=0),
                sleep=web.sleep,
                monotonic=web.monotonic,
            )
            with pytest.raises(FetchError) as raised:
                await fetcher.fetch("https://shop.example.com/")
            assert raised.value.reason == reason
