"""Render the original dark PicStatus layout using only local resources."""

from __future__ import annotations

import asyncio
import base64
import math
import mimetypes
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

import jinja2
from markupsafe import Markup, escape
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    Playwright,
    async_playwright,
)

PLUGIN_PATH = Path(__file__).resolve().parent
TEMPLATE_DIR = PLUGIN_PATH / "templates" / "default"
RES_PATH = TEMPLATE_DIR / "res"
CSS_PATH = RES_PATH / "css"
TEMPLATE_PATH = RES_PATH / "templates"

# A .jinja suffix is not covered by select_autoescape(["html", "xml"]).
ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH)),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _number(value: Any) -> float | None:
    """Convert a finite metric without manufacturing a zero for missing data.

    Args:
        value: A numeric metric or an unavailable value.

    Returns:
        A finite float, or None if the metric is unavailable.
    """
    if value is None or isinstance(value, (bool, jinja2.Undefined)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _fmt_bytes(value: Any, suffix: str = "") -> str:
    """Format a byte count or byte rate using binary unit steps.

    Args:
        value: A byte count, including zero, or None.
        suffix: An optional suffix such as /s.

    Returns:
        A human-readable count, or an explicit unknown label.
    """
    number = _number(value)
    if number is None or number < 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if number < 1024 or unit == "PB":
            return f"{number:.1f}{unit}{suffix}"
        number /= 1024
    return "未知"


def _fmt_seconds(value: Any) -> str:
    """Format elapsed seconds while accepting legacy preformatted durations.

    Args:
        value: Elapsed seconds, None, or a legacy duration string.

    Returns:
        A compact duration or an explicit unknown label.
    """
    if isinstance(value, jinja2.Undefined):
        return "未知"
    if isinstance(value, str):
        return value.strip() or "未知"
    seconds = _number(value)
    if seconds is None or seconds < 0:
        return "未知"
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes or parts:
        parts.append(f"{minutes}分")
    if not parts or (not days and not hours and seconds):
        parts.append(f"{seconds}秒")
    return "".join(parts)


def _percent_to_color(value: Any) -> str:
    """Choose the original PicStatus utilization color.

    Args:
        value: An actual utilization percentage, not its clamped chart value.

    Returns:
        A trusted CSS class name.
    """
    number = _number(value)
    if number is None:
        return "gray"
    if number < 70:
        return "prog-low"
    if number < 90:
        return "prog-medium"
    return "prog-high"


def _clamp_percent(value: Any) -> float:
    """Clamp only chart geometry, preserving the actual displayed metric.

    Args:
        value: A percentage that can exceed 100, such as system load.

    Returns:
        A finite percentage within the inclusive range 0 to 100.
    """
    number = _number(value)
    return min(100.0, max(0.0, number)) if number is not None else 0.0


def _fmt_percent(value: Any, digits: int = 0) -> str:
    """Format an actual percentage, including a genuine zero.

    Args:
        value: A percentage or None.
        digits: Number of decimal places to display.

    Returns:
        The actual percentage or an explicit unknown label.
    """
    number = _number(value)
    return f"{number:.{digits}f}%" if number is not None else "未知"


def _format_cpu_freq(value: Any) -> str:
    """Format current and maximum MHz values without assuming availability.

    Args:
        value: A current/max mapping, a legacy scalar in MHz, or None.

    Returns:
        A frequency label with units and an optional maximum frequency.
    """
    current = (
        _number(value.get("current")) if isinstance(value, Mapping) else _number(value)
    )
    maximum = _number(value.get("max")) if isinstance(value, Mapping) else None
    if current is None or current <= 0:
        return "频率未知"
    label = f"{current / 1000:.2f}GHz" if current >= 1000 else f"{current:.0f}MHz"
    if maximum is not None and maximum > 0:
        maximum_label = (
            f"{maximum / 1000:.2f}GHz" if maximum >= 1000 else f"{maximum:.0f}MHz"
        )
        label += f" / 最高 {maximum_label}"
    return label


def _br(value: Any) -> Markup:
    """Preserve newlines without trusting HTML in host or process strings.

    Args:
        value: Plain text that may contain newlines or HTML metacharacters.

    Returns:
        Escaped text containing only trusted line-break markup.
    """
    return escape(str(value)).replace("\n", Markup("<br />"))


ENV.filters.update(
    number=_number,
    auto_convert_unit=_fmt_bytes,
    duration=_fmt_seconds,
    percent_to_color=_percent_to_color,
    clamp_percent=_clamp_percent,
    percentage=_fmt_percent,
    format_cpu_freq=_format_cpu_freq,
    br=_br,
)


class StatusRenderer:
    """Own a bounded, reusable Chromium renderer for one plugin instance."""

    def __init__(self) -> None:
        """Initialize browser ownership and serialize memory-heavy screenshots."""
        self._browser: Browser | None = None
        self._playwright: Playwright | None = None
        self._startup_lock = asyncio.Lock()
        self._render_semaphore = asyncio.Semaphore(1)
        self._closed = False
        self._timeout_ms = 20_000

    def build_html(self, data: dict[str, Any]) -> str:
        """Build self-contained HTML without browser work or input mutation.

        Local images are embedded so document rendering never needs an HTTP
        request, a running asset server, or an arbitrary asset-loading delay.

        Args:
            data: Collected status metrics and resolved image resources.

        Returns:
            An autoescaped HTML document containing inline CSS and images.
        """
        view = {
            "hostname": "未知主机",
            "system": "系统信息未知",
            "python_version": "未知",
            "astrbot_version": "未知",
            "plugin_version": "未知",
            "time": "时间未知",
            "system_run_time": None,
            "astrbot_run_time": None,
            "cpu_percent": None,
            "cpu_count": None,
            "cpu_count_logical": None,
            "cpu_brand": "型号未知",
            "cpu_freq": None,
            "memory_stat": {},
            "swap_stat": {},
            "load_avg": None,
            "gpu_stat": [],
            "disk_usage": [],
            "disk_io": [],
            "network_io": [],
            "network_connection": [],
            "process_status": [],
            "components": [
                "header",
                "cpu_mem",
                "gpu",
                "disk",
                "network",
                "process",
                "footer",
            ],
            "theme": "dark",
            "additional_css": [],
            **data,
        }
        if isinstance(view["time"], datetime):
            view["time"] = view["time"].strftime("%Y-%m-%d %H:%M:%S")
        view["bots"] = [dict(bot) for bot in data.get("bots") or []]
        view["memory_stat"] = view["memory_stat"] or {}
        view["swap_stat"] = view["swap_stat"] or {}

        resources = [
            (view, "background_url", PLUGIN_PATH / "assets" / "default_bg.webp"),
            (view, "default_avatar", PLUGIN_PATH / "assets" / "default_avatar.webp"),
            *(
                (bot, "avatar", PLUGIN_PATH / "assets" / "default_avatar.webp")
                for bot in view["bots"]
            ),
        ]
        for owner, key, fallback in resources:
            source = str(owner.get(key) or "")
            if source.lower().startswith("data:image/") and "," in source:
                owner[key] = source
                continue
            parsed = urlsplit(source)
            candidate = None
            if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
                candidate = Path(url2pathname(parsed.path))
            elif not parsed.scheme and source:
                local_path = Path(source).expanduser()
                if local_path.is_absolute():
                    candidate = local_path
            if candidate is None or not candidate.is_file():
                candidate = fallback
            try:
                mime = mimetypes.guess_type(candidate.name)[0] or "image/webp"
                image_bytes = candidate.read_bytes() if candidate.is_file() else b""
            except OSError:
                image_bytes = b""
            owner[key] = (
                f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
                if image_bytes
                else ""
            )

        supported = {"header", "cpu_mem", "gpu", "disk", "network", "process", "footer"}
        if not view["components"] or any(
            name not in supported for name in view["components"]
        ):
            raise ValueError("Unsupported or empty status components")
        if view["theme"] not in {"dark", "light"}:
            raise ValueError("Unsupported status theme")
        css_names = ["index.css"]
        if view["theme"] == "dark":
            css_names.append("theme-dark.css")
        for name in view["additional_css"]:
            if name not in {"no-shadow", "no-radius", "no-blur"}:
                raise ValueError("Unsupported additional status CSS")
            css_names.append(f"{name}.css")
        css = "\n".join(
            (CSS_PATH / name).read_text(encoding="utf-8") for name in css_names
        )
        return ENV.get_template("index.html.jinja").render(d=view, css=Markup(css))

    async def _ensure_browser(self) -> Browser:
        """Start Chromium once and replace a disconnected browser safely.

        Returns:
            A connected Chromium browser owned by this renderer.

        Raises:
            RuntimeError: If the renderer has already been closed.
            Error: If Playwright cannot start Chromium.
        """
        async with self._startup_lock:
            if self._closed:
                raise RuntimeError("The status renderer has been closed")
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                with suppress(Error):
                    await self._browser.close()
                self._browser = None
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    timeout=self._timeout_ms,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
            except BaseException:
                playwright, self._playwright = self._playwright, None
                with suppress(Error):
                    await playwright.stop()
                raise
            return self._browser

    async def _render_page(self, html: str, output: Path) -> str:
        """Capture only the original 650 CSS-pixel status element at DPR 2.

        Args:
            html: Self-contained status HTML from build_html.
            output: The destination image path chosen by the caller.

        Returns:
            The image path after a successful screenshot.

        Raises:
            Error: If Chromium fails after one browser-disconnection retry.
            RuntimeError: If the rendered element has invalid dimensions.
        """
        for attempt in range(2):
            browser: Browser | None = None
            context: BrowserContext | None = None
            context_task: asyncio.Task[BrowserContext] | None = None
            page: Page | None = None
            try:
                browser = await self._ensure_browser()
                context_task = asyncio.create_task(
                    browser.new_context(
                        viewport={"width": 650, "height": 1000},
                        device_scale_factor=2,
                        color_scheme="dark",
                        locale="zh-CN",
                        service_workers="block",
                    )
                )
                # A cancelled new_context call can still create a remote context.
                context = await asyncio.shield(context_task)
                # All legitimate resources are already embedded in the HTML.
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                page.set_default_timeout(self._timeout_ms)
                await page.set_content(html, wait_until="domcontentloaded")
                await page.evaluate(
                    """async () => {
                        await document.fonts.ready;
                        await Promise.all(
                            Array.from(document.images, image => image.decode())
                        );
                    }"""
                )
                element = page.locator(".main-background")
                bounds = await element.bounding_box()
                if (
                    bounds is None
                    or abs(bounds["width"] - 650) > 0.5
                    or bounds["height"] <= 0
                ):
                    raise RuntimeError("The status element has invalid dimensions")
                output.parent.mkdir(parents=True, exist_ok=True)
                image_type = (
                    "jpeg" if output.suffix.lower() in (".jpg", ".jpeg") else "png"
                )
                await element.screenshot(
                    path=str(output), type=image_type, animations="disabled"
                )
                return str(output)
            except Error:
                if attempt or browser is None or browser.is_connected():
                    raise
                # A crashed browser is replaced by _ensure_browser on retry.
            finally:
                if context is None and context_task is not None:
                    with suppress(Error, asyncio.TimeoutError):
                        context = await asyncio.wait_for(context_task, timeout=5)
                if page is not None:
                    with suppress(Error, asyncio.TimeoutError):
                        await asyncio.wait_for(page.close(), timeout=5)
                if context is not None:
                    with suppress(Error, asyncio.TimeoutError):
                        await asyncio.wait_for(context.close(), timeout=5)
        raise RuntimeError("The status screenshot could not be produced")

    async def render(self, data: dict[str, Any], output_path: str) -> str:
        """Render status data with bounded concurrency and a total deadline.

        Args:
            data: The collector's status dictionary.
            output_path: The destination image path chosen by the caller.

        Returns:
            The destination image path.

        Raises:
            asyncio.TimeoutError: If browser rendering takes over 30 seconds.
            RuntimeError: If this renderer is closed or the layout is invalid.
            Error: If Chromium or image decoding fails.
        """
        async with self._render_semaphore:
            if self._closed:
                raise RuntimeError("The status renderer has been closed")
            html = self.build_html(data)
            return await asyncio.wait_for(
                self._render_page(html, Path(output_path)), timeout=30
            )

    async def close(self) -> None:
        """Finish an active render and release the owned browser and driver."""
        async with self._render_semaphore, self._startup_lock:
            self._closed = True
            browser, self._browser = self._browser, None
            playwright, self._playwright = self._playwright, None
            try:
                if browser is not None:
                    with suppress(Error, asyncio.TimeoutError):
                        await asyncio.wait_for(browser.close(), timeout=5)
            finally:
                if playwright is not None:
                    with suppress(Error, asyncio.TimeoutError):
                        await asyncio.wait_for(playwright.stop(), timeout=5)


_renderer: StatusRenderer | None = None


def get_renderer() -> StatusRenderer:
    """Get the legacy shared instance, recreating it after explicit closure.

    Returns:
        An open renderer; plugin-owned instances are preferred.
    """
    global _renderer
    if _renderer is None or _renderer._closed:
        _renderer = StatusRenderer()
    return _renderer
