"""Regression tests for fresh backgrounds, bounded downloads and avatar caching."""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import logging
import os
import time
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image

ASSETS = importlib.import_module("data.plugins.astrbot_plugin_sys_tools.status_assets")


@pytest.fixture(autouse=True)
def capture_asset_logs(monkeypatch):
    """Use an isolated logger because AstrBot disables its logger propagation."""
    monkeypatch.setattr(ASSETS, "log", logging.getLogger("status_assets_test"))


@pytest.fixture
def pictures():
    """Return distinct real encoded images rather than signature-only placeholders."""
    results = []
    for color, encoding in (("red", "PNG"), ("blue", "JPEG"), ("green", "WEBP")):
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), color).save(buffer, format=encoding)
        results.append(buffer.getvalue())
    return results


@pytest.fixture
def mock_http(monkeypatch):
    """Inject an HTTPX transport while preserving the real request/stream lifecycle."""
    original_client = httpx.AsyncClient
    options = []

    def install(handler):
        def client(**kwargs):
            options.append(kwargs.copy())
            # Capture proxy routing, but use MockTransport rather than a real proxy.
            kwargs.pop("proxy", None)
            return original_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(ASSETS.httpx, "AsyncClient", client)
        return options

    return install


def test_remote_background_is_fresh_but_avatar_is_cached(tmp_path, pictures, mock_http):
    requests = []
    backgrounds = iter(pictures[:2])

    def handle(request):
        requests.append(request)
        raw = pictures[2] if request.url.host == "avatar.test" else next(backgrounds)
        # A misleading content type must not affect the data URI encoding.
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    options = mock_http(handle)
    local = tmp_path / "ignored.png"
    local.write_bytes(pictures[2])
    assets = ASSETS.StatusAssets(
        tmp_path / "cache", "loli", str(local), proxy="http://proxy.test:7890"
    )

    async def run():
        first = await assets.load("background")
        second = await assets.load("background")
        avatar = await assets.load("avatar", "https://avatar.test/bot")
        assert await assets.load("avatar", "https://avatar.test/bot") == avatar
        assert first == ASSETS.image_data_uri(pictures[0])
        assert second == ASSETS.image_data_uri(pictures[1])
        assert first != second

    asyncio.run(run())
    assert len(requests) == 3
    assert all(
        str(item.url).startswith("https://www.loliapi.com/acg/pe/?")
        for item in requests[:2]
    )
    assert (
        requests[0].url.params["_sys_tools_nonce"]
        != requests[1].url.params["_sys_tools_nonce"]
    )
    assert requests[2].url.query == b""
    assert [item["proxy"] for item in options] == [
        "http://proxy.test:7890",
        "http://proxy.test:7890",
        None,
    ]
    assert all(item["trust_env"] is False for item in options)
    assert all(item["follow_redirects"] is True for item in options)
    assert all(item["max_redirects"] == 10 for item in options)
    assert all(item["timeout"] == 5.0 for item in options)


def test_custom_url_preserves_query_and_follows_redirect(tmp_path, pictures, mock_http):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/random":
            return httpx.Response(302, headers={"location": "/actual-image"})
        return httpx.Response(200, content=pictures[0])

    mock_http(handle)
    assets = ASSETS.StatusAssets(
        tmp_path,
        "url",
        url="https://custom.test/random?token=a%2Bb&tag=1&tag=2#fragment",
    )
    assert asyncio.run(assets.load("background")) == ASSETS.image_data_uri(pictures[0])
    assert len(requests) == 2
    assert requests[0].url.query.startswith(b"token=a%2Bb&tag=1&tag=2&")
    assert parse_qs(requests[0].url.query.decode())["tag"] == ["1", "2"]
    assert requests[1].url.path == "/actual-image"


def test_success_failure_then_success_retries_without_background_ttl(
    tmp_path, pictures, mock_http, caplog
):
    outcomes = iter([pictures[0], None, pictures[1]])
    calls = []

    def handle(request):
        calls.append(request)
        raw = next(outcomes)
        if raw is None:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=raw)

    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path)

    async def run():
        first = await assets.load("background")
        assert await assets.load("background") == first
        assert await assets.load("background") == ASSETS.image_data_uri(pictures[1])

    asyncio.run(run())
    assert len(calls) == 3
    assert "last successful cached image; retry on next load" in caplog.text
    cached = list(tmp_path.glob("background-*.img"))
    assert len(cached) == 1
    assert cached[0].read_bytes() == pictures[1]


def test_packaged_failure_is_not_cached_as_success(
    tmp_path, pictures, mock_http, caplog
):
    calls = []

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(503)
            if len(calls) == 1
            else httpx.Response(200, content=pictures[0])
        )

    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path)

    async def run():
        assert await assets.load("background") == ASSETS.image_data_uri(
            (ASSETS.ASSETS / "default_bg.webp").read_bytes()
        )
        assert not assets._memory
        assert not list(tmp_path.glob("*.img"))
        assert await assets.load("background") == ASSETS.image_data_uri(pictures[0])

    asyncio.run(run())
    assert len(calls) == 2
    assert "using packaged image; retry on next load" in caplog.text


def test_disk_background_only_fallback_never_one_hour_reuse(
    tmp_path, pictures, mock_http
):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200, content=pictures[0] if len(calls) == 1 else pictures[1]
        )

    mock_http(handle)
    assert asyncio.run(
        ASSETS.StatusAssets(tmp_path).load("background")
    ) == ASSETS.image_data_uri(pictures[0])
    restarted = ASSETS.StatusAssets(tmp_path)
    assert asyncio.run(restarted.load("background")) == ASSETS.image_data_uri(
        pictures[0]
    )
    assert asyncio.run(restarted.load("background")) == ASSETS.image_data_uri(
        pictures[1]
    )
    assert len(calls) == 3


def test_avatar_memory_and_disk_expire_after_one_hour(tmp_path, pictures, mock_http):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=pictures[min(len(calls) - 1, 1)])

    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path, default_avatar="https://avatar.test/default")
    first = asyncio.run(assets.load("avatar"))
    assert asyncio.run(assets.load("avatar")) == first
    assert (
        asyncio.run(
            ASSETS.StatusAssets(
                tmp_path, default_avatar="https://avatar.test/default"
            ).load("avatar")
        )
        == first
    )
    assert len(calls) == 1
    for key, (_, result) in assets._memory.items():
        assets._memory[key] = (time.monotonic() - 3601, result)
    cache_file = next(tmp_path.glob("avatar-*.img"))
    old = time.time() - 3601
    os.utime(cache_file, (old, old))
    assert asyncio.run(assets.load("avatar")) == ASSETS.image_data_uri(pictures[1])
    assert len(calls) == 2


def test_local_file_and_directory_avoid_repeat(tmp_path, pictures, mock_http):
    def handle(request):
        pytest.fail(f"Local image unexpectedly made a network request: {request.url}")

    mock_http(handle)
    local = tmp_path / "local"
    local.mkdir()
    # Extensions intentionally lie; bytes determine image validity and MIME.
    first = local / "first.txt"
    second = local / "second.bin"
    first.write_bytes(pictures[0])
    second.write_bytes(pictures[1])
    (local / "bad.png").write_bytes(b"not an image")
    (local / "directory.jpg").mkdir()
    file_assets = ASSETS.StatusAssets(tmp_path / "cache", "local", str(first))
    assert asyncio.run(file_assets.load("background")) == ASSETS.image_data_uri(
        pictures[0]
    )
    assets = ASSETS.StatusAssets(tmp_path / "cache", "local", str(local))

    async def run():
        results = [await assets.load("background") for _ in range(6)]
        assert set(results) == {ASSETS.image_data_uri(raw) for raw in pictures[:2]}
        assert all(left != right for left, right in zip(results, results[1:]))
        second.unlink()
        assert await assets.load("background") == ASSETS.image_data_uri(pictures[0])
        assert await assets.load("background") == ASSETS.image_data_uri(pictures[0])

    asyncio.run(run())


@pytest.mark.parametrize("path", ["", "missing", "empty", "bad"])
def test_local_missing_empty_invalid_uses_packaged(tmp_path, path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "bad").write_bytes(b"GIF89a invalid")
    assets = ASSETS.StatusAssets(
        tmp_path / "cache", "local", str(tmp_path / path) if path else ""
    )
    assert asyncio.run(assets.load("background")) == ASSETS.image_data_uri(
        (ASSETS.ASSETS / "default_bg.webp").read_bytes()
    )


@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"<html>no</html>",
        b"\xff\xd8\xffgarbage",
        b"\x89PNG\r\n\x1a\ntruncated",
        b"GIF89agarbage",
        b"RIFF0000WEBPgarbage",
    ],
)
def test_invalid_or_truncated_bytes_rejected(invalid):
    with pytest.raises(ValueError):
        ASSETS.image_data_uri(invalid)


def test_data_uri_matches_actual_encoding(pictures):
    for raw, mime in zip(pictures, ("png", "jpeg", "webp")):
        result = ASSETS.image_data_uri(raw)
        prefix, encoded = result.split(",", 1)
        assert prefix == f"data:image/{mime};base64"
        assert base64.b64decode(encoded) == raw
    with pytest.raises(ValueError, match="10 MiB"):
        ASSETS.image_data_uri(b"x" * (ASSETS.MAX_IMAGE_BYTES + 1))


@pytest.mark.parametrize("mode", ["html", "too_large", "redirect_loop", "timeout"])
def test_remote_invalid_bounded_and_timeout_fall_back(
    tmp_path, mode, mock_http, caplog
):
    async def handle(request):
        if mode == "timeout":
            await asyncio.sleep(1)
        if mode == "redirect_loop":
            return httpx.Response(302, headers={"location": "/again"})
        return httpx.Response(
            200,
            content=b"x" * (ASSETS.MAX_IMAGE_BYTES + 1)
            if mode == "too_large"
            else b"<html>not an image</html>",
        )

    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path, timeout=0.01 if mode == "timeout" else 5)
    assert asyncio.run(assets.load("background")) == ASSETS.image_data_uri(
        (ASSETS.ASSETS / "default_bg.webp").read_bytes()
    )
    assert not assets._memory
    assert not list(tmp_path.glob("*.img"))
    assert "retry on next load" in caplog.text


def test_unsupported_source_kind_and_timeout_are_explicit(tmp_path):
    with pytest.raises(ValueError, match="Unsupported background source"):
        ASSETS.StatusAssets(tmp_path, "typo")
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="timeout"):
            ASSETS.StatusAssets(tmp_path, timeout=timeout)
    with pytest.raises(ValueError, match="Unsupported asset kind"):
        asyncio.run(ASSETS.StatusAssets(tmp_path).load("typo"))


def test_random_background_retries_dead_redirects_with_new_nonce(tmp_path, pictures, mock_http):
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(404)
        return httpx.Response(200, content=pictures[0])
    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path, retry_limit=3)
    assert asyncio.run(assets.load("background")) == ASSETS.image_data_uri(pictures[0])
    assert len(requests) == 2
    assert requests[0].url.params['_sys_tools_nonce'] != requests[1].url.params['_sys_tools_nonce']


def test_background_retries_share_one_total_timeout(tmp_path, mock_http):
    requests = []
    async def handle(request):
        requests.append(request)
        await asyncio.sleep(0.1)
        return httpx.Response(404)
    mock_http(handle)
    assets = ASSETS.StatusAssets(tmp_path, timeout=0.01, retry_limit=3)
    started = time.monotonic()
    asyncio.run(assets.load("background"))
    assert time.monotonic() - started < 0.2
    assert len(requests) == 1
