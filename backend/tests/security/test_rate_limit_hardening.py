"""Rate limiter hardening: client identification behind Render/Cloudflare and bounded memory."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.api.rate_limit import RateLimiter, client_ip
from tests.api.conftest import ClientFactory


def _request(headers: dict[str, str], peer: str | None = "10.0.0.2") -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw,
                    "client": (peer, 5000) if peer else None})


def test_cloudflare_client_address_wins_over_a_client_written_forwarded_header() -> None:
    # Render sits behind Cloudflare, which sets CF-Connecting-IP; the left-most X-Forwarded-For entry is
    # whatever the client sent.
    request = _request({"CF-Connecting-IP": "203.0.113.5", "X-Forwarded-For": "198.51.100.77, 203.0.113.5"})
    assert client_ip(request) == "203.0.113.5"


def test_true_client_ip_is_used_when_cloudflare_sets_it() -> None:
    assert client_ip(_request({"True-Client-IP": "203.0.113.6"})) == "203.0.113.6"


@pytest.mark.parametrize("value", ["not-an-ip", "x" * 300, "999.1.1.1", ""])
def test_malformed_addresses_are_ignored(value: str) -> None:
    assert client_ip(_request({"CF-Connecting-IP": value})) == "10.0.0.2"
    assert client_ip(_request({"X-Forwarded-For": value})) == "10.0.0.2"


def test_ipv6_clients_share_their_slash_64() -> None:
    first = client_ip(_request({"CF-Connecting-IP": "2001:db8:1:2::1"}))
    second = client_ip(_request({"CF-Connecting-IP": "2001:db8:1:2:ffff::9"}))
    assert first == second == "2001:db8:1:2::/64"


def test_tracked_clients_are_bounded() -> None:
    limiter = RateLimiter(per_minute=5, per_day=100, max_clients=100)
    for index in range(1_000):
        limiter.hit(f"192.0.2.{index % 250}-{index}")
    assert limiter.tracked_clients() <= 100


def test_recently_seen_clients_survive_eviction() -> None:
    limiter = RateLimiter(per_minute=2, per_day=100, max_clients=3)
    assert limiter.hit("keep").allowed and limiter.hit("keep").allowed
    for name in ("a", "b", "keep", "c", "keep", "d"):
        limiter.hit(name)
    assert not limiter.hit("keep").allowed  # its window was not reset by the churn


def test_rotating_forwarded_headers_do_not_bypass_the_limit(make_client: ClientFactory) -> None:
    client, _ = make_client(rate_limit_enabled=True, rate_limit_per_minute=2, rate_limit_per_day=100)
    statuses = [
        client.post(
            "/api/chat",
            json={"message": "hi"},
            headers={"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": f"198.51.100.{index}, 203.0.113.9"},
        ).status_code
        for index in range(4)
    ]
    assert statuses == [200, 200, 429, 429]
