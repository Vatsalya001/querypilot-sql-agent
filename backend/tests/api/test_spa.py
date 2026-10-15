"""Tests for serving the built single-page app next to the API."""

from pathlib import Path

import pytest

from tests.api.conftest import ClientFactory


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    """A minimal Vite-style build plus a file outside it that must never be served."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>QueryPilot</title><div id='root'></div>")
    (root / "assets" / "index-abc123.js").write_text("console.log('querypilot')")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return root


def test_spa_serves_index_for_client_routes(make_client: ClientFactory, ui_dir: Path) -> None:
    client, _ = make_client(static_dir=str(ui_dir))

    for path in ("/", "/history/8f6a1c2e", "/settings"):
        response = client.get(path)

        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        assert "<title>QueryPilot</title>" in response.text
        assert response.headers["cache-control"] == "no-cache"
    assert client.head("/").status_code == 200


def test_spa_serves_assets_and_root_files(make_client: ClientFactory, ui_dir: Path) -> None:
    client, _ = make_client(static_dir=str(ui_dir))

    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert asset.text == "console.log('querypilot')"

    favicon = client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")

    missing = client.get("/assets/missing-123.js")
    assert missing.status_code == 404
    assert "QueryPilot" not in missing.text


def test_api_paths_keep_json_errors_and_win_over_the_catch_all(
    make_client: ClientFactory, ui_dir: Path
) -> None:
    client, _ = make_client(static_dir=str(ui_dir))

    for path in ("/api", "/api/unknown", "/api/unknown/deeper"):
        response = client.get(path)

        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found", "code": "not_found"}
    assert client.get("/api/schema").status_code == 200
    assert client.get("/api/docs").status_code == 200


@pytest.mark.parametrize("path", ["/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"])
def test_files_outside_the_static_dir_are_never_served(
    make_client: ClientFactory, ui_dir: Path, path: str
) -> None:
    client, _ = make_client(static_dir=str(ui_dir))

    response = client.get(path)

    assert "TOP SECRET" not in response.text


def test_static_dir_without_index_html_is_ignored(make_client: ClientFactory, tmp_path: Path) -> None:
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    client, _ = make_client(static_dir=str(empty))

    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["docs"] == "/api/docs"


@pytest.mark.parametrize("path", ["/%00", "/history/abc%00def", "/" + "a" * 300, "/x/" + "b" * 300])
def test_unusual_client_routes_get_the_app_instead_of_a_500(
    make_client: ClientFactory, ui_dir: Path, path: str
) -> None:
    # Regression: a NUL byte (ValueError) or an over-long segment (OSError ENAMETOOLONG) in the path escaped
    # the file lookup, so the catch-all answered 500 and logged a traceback for every such request.
    client, _ = make_client(static_dir=str(ui_dir), raise_server_exceptions=False)

    response = client.get(path)

    assert response.status_code == 200, response.text
    assert "<title>QueryPilot</title>" in response.text


def test_wrong_method_on_an_api_route_is_405_with_or_without_the_ui(make_client: ClientFactory, ui_dir: Path) -> None:
    # Regression: with the UI mounted, the GET catch-all answered "GET /api/chat" with a 404, as if the endpoint
    # did not exist; without the UI the same request was a 405.
    for options in ({"static_dir": str(ui_dir)}, {}):
        client, _ = make_client(**options)

        response = client.get("/api/chat")

        assert response.status_code == 405, options
        assert response.json()["code"] == "method_not_allowed"
        assert "POST" in response.headers["allow"]
    client, _ = make_client(static_dir=str(ui_dir))
    assert client.get("/api/nope").status_code == 404
