"""In-memory sliding-window rate limiter keyed by client IP (per minute and per day).

It protects the free LLM quota of the public demo. State lives in process memory, so limits reset on
restart and are per instance, which is the intended trade-off for a single free-tier container. The
number of tracked clients is bounded, so rotating addresses cannot grow memory without limit.
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.api.errors import APIError

MINUTE_S = 60.0
DAY_S = 86_400.0
MAX_TRACKED_CLIENTS = 10_000
"""Clients remembered at once; the least recently seen are forgotten first (about 1 KB each)."""
_SWEEP_INTERVAL_S = 300.0
_EDGE_CLIENT_HEADERS = ("cf-connecting-ip", "true-client-ip")
"""Single-address headers set by Cloudflare, which fronts Render; Cloudflare rejects or overwrites
client-supplied values, unlike X-Forwarded-For, whose left-most entry is whatever the client sent."""


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of one rate-limit check."""

    allowed: bool
    retry_after: int = 0
    """Whole seconds until the next request would be accepted (0 when allowed)."""
    window: str | None = None
    """``"minute"`` or ``"day"`` when the request was blocked."""


class RateLimiter:
    """Thread-safe sliding-window limiter.

    Each key keeps the timestamps of its accepted requests from the last 24 hours. A request is accepted
    when fewer than ``per_minute`` of them fall in the last minute and fewer than ``per_day`` in the last
    day. Rejected requests are not recorded, so a blocked client recovers as soon as the window slides.
    A non-positive limit disables that window.
    """

    def __init__(
        self,
        per_minute: int,
        per_day: int,
        *,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self.enabled = enabled
        self.per_minute = per_minute
        self.per_day = per_day
        self.max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._last_sweep = clock()

    def tracked_clients(self) -> int:
        """Number of clients currently remembered."""
        with self._lock:
            return len(self._hits)

    def hit(self, key: str) -> RateLimitResult:
        """Record a request for ``key`` if it is within the limits and report the decision."""
        if not self.enabled:
            return RateLimitResult(allowed=True)
        now = self._clock()
        with self._lock:
            self._sweep_stale_keys(now)
            hits = self._hits.setdefault(key, deque())
            self._hits.move_to_end(key)
            while len(self._hits) > self.max_clients:
                self._hits.popitem(last=False)  # forget the least recently seen client
            while hits and hits[0] <= now - DAY_S:
                hits.popleft()
            for window, length_s, limit in (
                ("minute", MINUTE_S, self.per_minute),
                ("day", DAY_S, self.per_day),
            ):
                if limit > 0 and _count_since(hits, now - length_s) >= limit:
                    # The request becomes possible once the oldest of the last `limit` hits leaves the window.
                    retry_after = max(1, math.ceil(hits[-limit] + length_s - now))
                    return RateLimitResult(allowed=False, retry_after=retry_after, window=window)
            hits.append(now)
            return RateLimitResult(allowed=True)

    def _sweep_stale_keys(self, now: float) -> None:
        """Forget clients idle for a whole day so memory stays bounded (runs every few minutes)."""
        if now - self._last_sweep < _SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - DAY_S]
        for key in stale:
            del self._hits[key]


def _count_since(hits: deque[float], start: float) -> int:
    """Count timestamps newer than ``start`` in an ascending deque (walks from the newest end)."""
    count = 0
    for timestamp in reversed(hits):
        if timestamp <= start:
            break
        count += 1
    return count


def _client_key(value: str) -> str | None:
    """Normalized rate-limit key for an address string, or ``None`` if it is not an IP address.

    IPv6 clients are keyed by their /64 prefix, because one subscriber usually controls a whole /64.
    """
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return str(address)


def client_ip(request: Request) -> str:
    """Return the rate-limit key of the client.

    In order: the address Cloudflare reports (``CF-Connecting-IP``/``True-Client-IP``; Render is behind
    Cloudflare), the first ``X-Forwarded-For`` hop, else the socket peer address. Header values that are
    not IP addresses are ignored.
    """
    for header in _EDGE_CLIENT_HEADERS:
        key = _client_key(request.headers.get(header, ""))
        if key:
            return key
    key = _client_key(request.headers.get("x-forwarded-for", "").split(",")[0])
    if key:
        return key
    return request.client.host if request.client else "unknown"


def _limit_message(result: RateLimitResult, limiter: RateLimiter) -> str:
    if result.window == "day":
        return (
            f"Daily limit of {limiter.per_day} requests reached. "
            f"Please try again in about {math.ceil(result.retry_after / 3600)} hour(s)."
        )
    return (
        f"Too many requests: the limit is {limiter.per_minute} per minute. "
        f"Please try again in {result.retry_after} seconds."
    )


async def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency: raise a 429 ``APIError`` when the caller exceeded its rate limit."""
    limiter: RateLimiter = request.app.state.rate_limiter
    result = limiter.hit(client_ip(request))
    if not result.allowed:
        raise APIError(429, "rate_limited", _limit_message(result, limiter), retry_after=result.retry_after)
