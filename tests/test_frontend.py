import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def web(tmp_path, monkeypatch):
    """A client serving a fake frontend build. No database: these routes never touch it."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "robots.txt").write_text("User-agent: *")
    (tmp_path / "secret.txt").write_text("outside the build")
    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    return TestClient(main.app)  # not entered as a context, so startup (bucket setup) is skipped


def test_page_urls_get_the_app_so_the_router_can_pick_the_page(web):
    for path in ["/", "/trips/abc", "/some/deep/page", "/trips/abc/"]:
        res = web.get(path)
        assert res.status_code == 200
        assert res.text == "<div id=root></div>"
        assert res.headers["cache-control"] == "no-cache"
    assert web.head("/trips/abc").status_code == 200


def test_unreadable_paths_get_the_app_not_a_server_error(web):
    for path in ["/%00", "/a%00b", "/" + "x" * 5000]:
        assert web.get(path).status_code == 200


def test_built_assets_are_served_and_cached_forever(web):
    res = web.get("/assets/index-abc123.js")
    assert res.text == "console.log(1)"
    assert "immutable" in res.headers["cache-control"]
    assert web.get("/robots.txt").text == "User-agent: *"


def test_unknown_api_paths_are_json_404s_not_the_app(web):
    for path in ["/api", "/api/nope", "/api/photos/not-a-uuid/extra"]:
        res = web.get(path)
        assert res.status_code == 404
        assert res.headers["content-type"] == "application/json"


def test_api_routing_behaves_as_if_the_app_were_not_there(web):
    # The router's own answers come first: a trailing slash redirects, a wrong method is 405.
    res = web.get("/api/photos/", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"].endswith("/api/photos")
    assert web.post("/api/imports/", json={}, follow_redirects=False).status_code == 307
    assert web.delete("/api/timeline").status_code == 405
    assert web.post("/trips/abc").status_code == 404  # only GET and HEAD get the app


def test_requests_cannot_escape_the_build_folder(web):
    for path in ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"]:
        assert "outside the build" not in web.get(path).text


def test_missing_build_says_how_to_fix_it(web, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "nothing-here")
    res = web.get("/")
    assert res.status_code == 404
    assert "npm run build" in res.json()["detail"]
