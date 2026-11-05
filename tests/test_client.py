import httpx
import pytest

from mm.data.client import DeribitClient, DeribitError


def _client(cfg, clock, handler):
    return DeribitClient(
        "https://example.test/api/v2",
        cfg.data.http,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def _ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": result})


def test_get_returns_result_and_builds_url(cfg, clock):
    seen = []

    def handler(req):
        seen.append(req.url)
        return _ok({"x": 1})

    c = _client(cfg, clock, handler)
    assert c.get("public/foo", {"a": 1}) == {"x": 1}
    assert seen[0].path == "/api/v2/public/foo"
    assert seen[0].params["a"] == "1"


def test_booleans_sent_lowercase(cfg, clock):
    seen = []

    def handler(req):
        seen.append(req.url)
        return _ok([])

    _client(cfg, clock, handler).get("public/get_instruments", {"expired": True})
    assert seen[0].params["expired"] == "true"


def test_retries_on_429_then_succeeds(cfg, clock):
    calls = iter([httpx.Response(429), httpx.Response(503), _ok({"ok": True})])
    c = _client(cfg, clock, lambda req: next(calls))
    assert c.get("public/foo", {}) == {"ok": True}
    base = cfg.data.http.backoff_base_s
    assert base in clock.sleeps and 2 * base in clock.sleeps


def test_retries_on_rpc_rate_limit_error(cfg, clock):
    err = httpx.Response(
        400, json={"jsonrpc": "2.0", "error": {"code": 10028, "message": "too_many_requests"}}
    )
    calls = iter([err, _ok(1)])
    assert _client(cfg, clock, lambda req: next(calls)).get("public/foo", {}) == 1


def test_gives_up_after_max_retries(cfg, clock):
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        return httpx.Response(500)

    with pytest.raises(DeribitError):
        _client(cfg, clock, handler).get("public/foo", {})
    assert n["calls"] == cfg.data.http.max_retries + 1


def test_non_retryable_api_error_raises_immediately(cfg, clock):
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        return httpx.Response(
            400, json={"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params"}}
        )

    with pytest.raises(DeribitError, match="Invalid params"):
        _client(cfg, clock, handler).get("public/foo", {})
    assert n["calls"] == 1


def test_rate_limit_spaces_requests(cfg, clock):
    c = _client(cfg, clock, lambda req: _ok(1))
    for _ in range(3):
        c.get("public/foo", {})
    gap = 1.0 / cfg.data.http.max_requests_per_s
    assert clock.sleeps == pytest.approx([gap, gap])
