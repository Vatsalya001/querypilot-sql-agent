"""Tests for the request body size cap (413 before an oversized body is buffered or parsed)."""

import json
from collections.abc import Iterator

from app.api.body_limit import MAX_BODY_BYTES
from tests.api.conftest import DEV_ORIGIN, ClientFactory

JSON_HEADERS = {"content-type": "application/json"}


def _chunks(payload: bytes, size: int = 256 * 1024) -> Iterator[bytes]:
    """Yield ``payload`` in pieces; httpx then sends it chunked, without a Content-Length header."""
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


def _oversized_body() -> bytes:
    return json.dumps({"message": "x" * (MAX_BODY_BYTES + 1)}).encode()


def test_declared_oversized_body_is_refused_before_reading(make_client: ClientFactory) -> None:
    client, agent = make_client()

    response = client.post(
        "/api/chat", content=_oversized_body(), headers={**JSON_HEADERS, "Origin": DEV_ORIGIN}
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": f"Request body is too large (limit: {MAX_BODY_BYTES:,} bytes).",
        "code": "payload_too_large",
    }
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    assert agent.requests == []


def test_chunked_oversized_body_is_cut_off_while_streaming(make_client: ClientFactory) -> None:
    client, agent = make_client()

    for path in ("/api/chat", "/api/chat/stream"):
        response = client.post(path, content=_chunks(_oversized_body()), headers=JSON_HEADERS)

        assert response.status_code == 413, path
        assert response.json()["code"] == "payload_too_large"
    assert agent.requests == []


def test_normal_chunked_bodies_still_work(make_client: ClientFactory) -> None:
    client, agent = make_client()
    body = json.dumps({"message": "Show all customers", "thread_id": "t-chunked"}).encode()

    response = client.post("/api/chat", content=_chunks(body, size=8), headers=JSON_HEADERS)

    assert response.status_code == 200
    assert agent.requests[0].message == "Show all customers"
