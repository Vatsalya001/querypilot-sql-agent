"""Rate limiter unit tests (with a fake clock) and its wiring into the chat endpoints."""

import pytest
from starlette.requests import Request

from app.api.rate_limit import DAY_S, RateLimiter, client_ip
from tests.api.conftest import ClientFactory


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_minute_window_blocks_then_recovers() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, per_day=100, clock=clock)

    assert all(limiter.hit("a").allowed for _ in range(3))
    blocked = limiter.hit("a")
    assert (blocked.allowed, blocked.window, blocked.retry_after) == (False, "minute", 60)

    clock.advance(59.5)
    assert limiter.hit("a").retry_after == 1
    clock.advance(0.5)
    assert limiter.hit("a").allowed


def test_window_slides_instead_of_resetting() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, per_day=100, clock=clock)
    for gap in (0, 20, 20):
        clock.advance(gap)
        assert limiter.hit("a").allowed

    clock.advance(10)  # 50 s after the first request
    assert limiter.hit("a").retry_after == 10
    clock.advance(10)  # the first request has left the window
    assert limiter.hit("a").allowed
    assert not limiter.hit("a").allowed


def test_day_window_blocks_with_a_long_retry_after() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=0, per_day=2, clock=clock)
    assert limiter.hit("a").allowed
    clock.advance(3_600)
    assert limiter.hit("a").allowed

    blocked = limiter.hit("a")

    assert (blocked.allowed, blocked.window) == (False, "day")
    assert blocked.retry_after == int(DAY_S - 3_600)


def test_rejected_requests_do_not_extend_the_block() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=1, per_day=100, clock=clock)
    assert limiter.hit("a").allowed
    for _ in range(10):
        clock.advance(5)
        assert not limiter.hit("a").allowed
    clock.advance(10)
    assert limiter.hit("a").allowed


def test_clients_are_limited_independently() -> None:
    limiter = RateLimiter(per_minute=1, per_day=100, clock=FakeClock())
    assert limiter.hit("a").allowed
    assert not limiter.hit("a").allowed
    assert limiter.hit("b").allowed


def test_disabled_limiter_allows_everything() -> None:
    limiter = RateLimiter(per_minute=1, per_day=1, enabled=False, clock=FakeClock())
    assert all(limiter.hit("a").allowed for _ in range(50))


def test_idle_clients_are_forgotten() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=5, per_day=100, clock=clock)
    limiter.hit("idle")
    clock.advance(DAY_S + 600)

    limiter.hit("active")

    assert set(limiter._hits) == {"active"}


def _request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers, "client": client})


def test_client_ip_prefers_the_first_forwarded_hop() -> None:
    request = _request([(b"x-forwarded-for", b" 203.0.113.7 , 10.0.0.1")], ("10.0.0.2", 5000))
    assert client_ip(request) == "203.0.113.7"


@pytest.mark.parametrize(
    ("headers", "client", "expected"),
    [([], ("10.0.0.2", 5000), "10.0.0.2"), ([(b"x-forwarded-for", b"")], None, "unknown")],
)
def test_client_ip_falls_back_to_the_peer(
    headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None, expected: str
) -> None:
    assert client_ip(_request(headers, client)) == expected


def test_chat_endpoints_return_429_with_retry_after(make_client: ClientFactory) -> None:
    client, agent = make_client(rate_limit_enabled=True, rate_limit_per_minute=2, rate_limit_per_day=100)
    assert client.post("/api/chat", json={"message": "first"}).status_code == 200
    assert client.post("/api/chat/stream", json={"message": "second"}).status_code == 200

    for path in ("/api/chat", "/api/chat/stream"):
        limited = client.post(path, json={"message": "third"})

        assert limited.status_code == 429
        assert limited.headers["content-type"] == "application/json"
        body = limited.json()
        assert set(body) == {"detail", "code", "retry_after"}
        assert body["code"] == "rate_limited"
        assert 1 <= body["retry_after"] <= 60
        assert "2 per minute" in body["detail"]
        assert limited.headers["retry-after"] == str(body["retry_after"])

    assert len(agent.requests) == 2
    assert client.get("/api/schema").status_code == 200
    assert client.get("/api/graph").status_code == 200


def test_each_forwarded_client_gets_its_own_budget(make_client: ClientFactory) -> None:
    client, _ = make_client(rate_limit_enabled=True, rate_limit_per_minute=1, rate_limit_per_day=100)

    def post(ip: str) -> int:
        return client.post("/api/chat", json={"message": "hi"}, headers={"X-Forwarded-For": ip}).status_code

    assert post("203.0.113.1") == 200
    assert post("203.0.113.1") == 429
    assert post("203.0.113.2") == 200


def test_rate_limit_can_be_disabled(make_client: ClientFactory) -> None:
    client, _ = make_client(rate_limit_enabled=False, rate_limit_per_minute=1)

    assert all(client.post("/api/chat", json={"message": "hi"}).status_code == 200 for _ in range(5))
