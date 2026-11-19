from __future__ import annotations

import pytest

from app.core.net import UNKNOWN_IP, resolve_client_ip, resolve_scheme


class TestClientIp:
    def test_without_trusted_proxy_the_peer_is_used_and_headers_are_ignored(self) -> None:
        assert resolve_client_ip("203.0.113.5", "1.1.1.1, 2.2.2.2", 0) == "203.0.113.5"

    def test_one_proxy_takes_the_rightmost_entry(self) -> None:
        assert resolve_client_ip("10.0.0.1", "198.51.100.7", 1) == "198.51.100.7"

    def test_forged_left_entries_are_ignored(self) -> None:
        # The attacker prepends fake IPs; our proxy appended the real one last.
        forged = "9.9.9.9, 8.8.8.8, 198.51.100.7"
        assert resolve_client_ip("10.0.0.1", forged, 1) == "198.51.100.7"

    def test_two_proxies_take_the_second_from_the_right(self) -> None:
        assert resolve_client_ip("10.0.0.1", "6.6.6.6, 198.51.100.7, 10.1.1.1", 2) == "198.51.100.7"

    def test_header_shorter_than_expected_falls_back_to_the_unforgeable_peer(self) -> None:
        # e.g. a request that bypassed the proxy and carries an attacker-supplied header
        assert resolve_client_ip("203.0.113.5", "1.2.3.4", 2) == "203.0.113.5"

    @pytest.mark.parametrize("garbage", ["not-an-ip", "999.1.1.1", "<script>", "1.2.3.4:80"])
    def test_malformed_entries_fall_back_to_the_peer(self, garbage: str) -> None:
        assert resolve_client_ip("203.0.113.5", garbage, 1) == "203.0.113.5"

    def test_ipv6_is_normalised(self) -> None:
        assert resolve_client_ip("10.0.0.1", "2001:DB8:0:0:0:0:0:1", 1) == "2001:db8::1"

    def test_missing_everything_is_unknown(self) -> None:
        assert resolve_client_ip(None, None, 1) == UNKNOWN_IP

    def test_empty_header_with_proxy_uses_peer(self) -> None:
        assert resolve_client_ip("203.0.113.5", "", 1) == "203.0.113.5"


class TestScheme:
    def test_forwarded_proto_is_only_believed_with_a_trusted_proxy(self) -> None:
        assert resolve_scheme("http", "https", 0) == "http"
        assert resolve_scheme("http", "https", 1) == "https"

    def test_last_forwarded_proto_wins(self) -> None:
        assert resolve_scheme("http", "https, http", 1) == "http"

    @pytest.mark.parametrize("bad", ["ftp", "javascript", "", "HTTPS://evil"])
    def test_garbage_forwarded_proto_is_ignored(self, bad: str) -> None:
        assert resolve_scheme("http", bad, 1) == "http"
