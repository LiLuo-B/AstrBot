"""Focused plugin integration and direct-delivery regression tests."""

from __future__ import annotations

import asyncio
import importlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

PLUGIN = importlib.import_module("data.plugins.astrbot_plugin_sys_tools.main")
ASSETS = importlib.import_module("data.plugins.astrbot_plugin_sys_tools.status_assets")


class FakeEvent:
    def __init__(self):
        self.extras = {}
        self.paths = []

    def get_platform_id(self):
        return "preview"

    def get_platform_name(self):
        return "qq_official"

    def is_admin(self):
        return False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def track_temporary_local_file(self, path):
        self.paths.append(path)

    def image_result(self, path):
        assert isinstance(path, str)
        assert Path(path).is_file()
        return ("image", path)

    def plain_result(self, text):
        return ("plain", text)


class FakeRenderer:
    def __init__(self):
        self.close = AsyncMock()

    async def render(self, data, output):
        image = Image.new("RGB", (20, 20), color="black")
        image.save(output)
        return output


def make_plugin():
    context = SimpleNamespace(get_platform_inst=lambda _: None)
    return PLUGIN.SysToolsPlugin(context, {"status": {"background_source": "local"}})


def test_image_magic_uses_bytes_not_extension():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="JPEG")
    assert ASSETS.image_data_uri(buffer.getvalue()).startswith("data:image/jpeg;")
    with pytest.raises(ValueError):
        ASSETS.image_data_uri(b"<html>not an image</html>")


def test_reading_legacy_duration_never_mutates_input():
    renderer = importlib.import_module(
        "data.plugins.astrbot_plugin_sys_tools.status_renderer"
    )
    data = {"system_run_time": "25天", "hostname": "<script>evil</script>"}
    html = renderer.StatusRenderer().build_html(data)
    assert data == {"system_run_time": "25天", "hostname": "<script>evil</script>"}
    assert "<script>evil</script>" not in html
    assert "25天" in html


def test_disabled_and_admin_only_stop_without_collecting():
    async def run():
        plugin = make_plugin()
        plugin.build_status_data = AsyncMock(
            side_effect=AssertionError("Must not collect")
        )
        event = FakeEvent()
        plugin.status_enabled = False
        assert [part async for part in plugin.query_server_status(event)][0][
            0
        ] == "plain"
        plugin.status_enabled = True
        plugin.only_admin = True
        assert (
            "管理员" in [part async for part in plugin.query_server_status(event)][0][1]
        )
        plugin.build_status_data.assert_not_called()

    asyncio.run(run())


def test_status_returns_image_and_distinct_paths(monkeypatch, tmp_path):
    async def run():
        plugin = make_plugin()
        plugin._renderer = FakeRenderer()
        plugin.build_status_data = AsyncMock(return_value={"hostname": "host"})
        monkeypatch.setattr(PLUGIN, "get_astrbot_temp_path", lambda: str(tmp_path))
        events = [FakeEvent(), FakeEvent()]
        outputs = await asyncio.gather(
            *(collect_result(plugin, event) for event in events)
        )
        paths = [parts[0][1] for parts in outputs]
        assert len(set(paths)) == 2
        assert all(parts[0][0] == "image" and len(parts) == 1 for parts in outputs)
        assert all(event.paths for event in events)
        await plugin.terminate()

    asyncio.run(run())


async def collect_result(plugin, event):
    return [part async for part in plugin.query_server_status(event)]


def test_missing_platform_status_is_not_fabricated():
    plugin = make_plugin()
    card = plugin._bot_card(FakeEvent())
    assert card["connected"] is None
    assert card["bot_connected"] is None
    assert "插件加载以来" in card["stats_scope"]


def test_observer_does_not_wake_ignored_messages():
    async def run():
        plugin = make_plugin()
        event = FakeEvent()
        await plugin.observe_incoming(event, None)
        await plugin.observe_incoming(event, None)
        assert plugin._counts["preview"]["received"] == 1
        assert not hasattr(event, "is_wake")

    asyncio.run(run())


def test_components_reorder_hide_and_safe_styles():
    renderer = importlib.import_module("data.plugins.astrbot_plugin_sys_tools.status_renderer")
    instance = renderer.StatusRenderer()
    html = instance.build_html({
        "components": ["footer", "cpu_mem"],
        "theme": "light",
        "additional_css": ["no-shadow", "no-radius", "no-blur"],
    })
    assert html.index('<footer class="footer">') < html.index('<div class="card cpu-mem-usage')
    assert '<div class="card network-info' not in html
    assert '<div class="card header' not in html
    for data in ({"components": ["unknown"]}, {"additional_css": ["../../evil"]}):
        with pytest.raises(ValueError):
            instance.build_html(data)


def test_expanded_config_reaches_collector_without_row_truncation(monkeypatch):
    async def run():
        context = SimpleNamespace(get_platform_inst=lambda _: None)
        plugin = PLUGIN.SysToolsPlugin(context, {"status": {
            "background_source": "local", "components": ["network", "disk"],
            "ignore_nets": [], "sort_nets": False, "proc_cpu_max_100p": True,
            "test_sites": [], "sort_sites": False,
        }})
        seen = {}
        rows = [{"name": f"br-test-{number}", "sent": 0, "recv": 0} for number in range(5)]
        def collect(**kwargs):
            seen.update(kwargs)
            return {"network_io": rows}
        monkeypatch.setattr(PLUGIN, "collect_status", collect)
        plugin._assets.load = AsyncMock(return_value="data:image/png;base64,test")
        data = await plugin.build_status_data(FakeEvent())
        assert data["network_io"] == rows
        assert seen["top_n"] == 0 and seen["show_gpu"] is False
        assert seen["display_options"]["ignore_nets"] == []
        assert seen["display_options"]["proc_cpu_max_100p"] is True
        assert data["components"] == ["network", "disk"]
    asyncio.run(run())


def test_sites_sort_matches_upstream_and_disabled_preserves_order(monkeypatch):
    async def run():
        plugin = make_plugin()
        monkeypatch.setattr(PLUGIN, "collect_status", lambda **kwargs: {"network_io": []})
        probes = [
            {"name": "slow", "delay": 50, "error": None},
            {"name": "failed", "delay": 100, "error": "failed", "reason": "ConnectError"},
            {"name": "fast", "delay": 10, "error": None},
        ]
        monkeypatch.setattr(PLUGIN, "check_connectivity", AsyncMock(side_effect=lambda *a, **k: [dict(x) for x in probes]))
        plugin._assets.load = AsyncMock(return_value="data:image/png;base64,test")
        data = await plugin.build_status_data(FakeEvent())
        assert [item["name"] for item in data["network_connection"]] == ["failed", "fast", "slow"]
        plugin.sort_sites = False
        data = await plugin.build_status_data(FakeEvent())
        assert [item["name"] for item in data["network_connection"]] == ["slow", "failed", "fast"]
    asyncio.run(run())


def test_jpeg_format_uses_jpeg_output(monkeypatch, tmp_path):
    async def run():
        plugin = make_plugin()
        plugin.image_format = "jpeg"
        plugin._renderer = FakeRenderer()
        plugin.build_status_data = AsyncMock(return_value={"hostname": "test"})
        monkeypatch.setattr(PLUGIN, "get_astrbot_temp_path", lambda: str(tmp_path))
        result = await collect_result(plugin, FakeEvent())
        assert result[0][1].endswith(".jpg")
        assert Image.open(result[0][1]).format == "JPEG"
        await plugin.terminate()
    asyncio.run(run())
