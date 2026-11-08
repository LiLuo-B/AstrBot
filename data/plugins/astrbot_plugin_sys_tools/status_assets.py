"""Bounded image loading with fresh backgrounds and cached platform avatars."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import math
import random
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image

log = logging.getLogger("astrbot")
ASSETS = Path(__file__).parent / "assets"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_data_uri(data: bytes) -> str:
    """Validate and encode image bytes, not an extension or HTTP content type.

    Args:
        data: Downloaded or local image bytes.

    Returns:
        A browser-ready data URI containing a decodable JPEG, PNG, GIF or WebP.

    Raises:
        ValueError: The bytes are invalid, unsupported or exceed the size bound.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is empty or exceeds 10 MiB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            mime = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "GIF": "image/gif",
                "WEBP": "image/webp",
            }.get(image.format)
            if mime is None:
                raise ValueError("Unsupported image bytes")
            image.verify()
        # Verification alone does not decode JPEG pixel data or reject truncation.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid image bytes") from exc
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class StatusAssets:
    """Resolve trusted background/avatar assets before launching the browser."""

    def __init__(
        self,
        cache_dir: Path,
        source: str = "loli",
        local_path: str = "",
        *,
        url: str = "",
        proxy: str | None = None,
        timeout: float = 5.0,
        default_avatar: str = "",
        retry_limit: int = 1,
    ):
        """Configure sources without retaining network clients or background tasks.

        Args:
            cache_dir: Directory for the last successfully downloaded image bytes.
            source: Background provider: ``loli``, ``url`` or ``local``.
            local_path: Image file or directory, used only by ``local``.
            url: Custom random-image endpoint used only by ``url``.
            proxy: Explicit proxy for background downloads only; avatars are direct.
            timeout: Total per-download deadline in seconds, including redirects.
            default_avatar: Trusted avatar URL used when the platform has no avatar.
            retry_limit: Background attempts sharing one total download deadline.

        Raises:
            ValueError: The background source or timeout is unsupported.
        """
        if source not in {"loli", "url", "local"}:
            raise ValueError(f"Unsupported background source: {source!r}")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Asset timeout must be finite and positive")
        self.cache_dir = Path(cache_dir)
        self.source = source
        self.local_path = local_path.strip()
        self.url = url.strip()
        self.proxy = proxy
        self.timeout = timeout
        self.default_avatar = default_avatar.strip()
        self.retry_limit = max(1, min(5, int(retry_limit)))
        self._last_local: Path | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._memory: dict[str, tuple[float, str]] = {}

    async def load(self, kind: str, url: str = "") -> str:
        """Fetch a fresh background or reuse a one-hour cached platform avatar.

        Args:
            kind: Either ``background`` or ``avatar``.
            url: Trusted platform avatar URL, never a user-provided argument.

        Returns:
            Validated data URI; failures use last successful or packaged bytes.
            Background failures are retried on the next load, without a cooldown.

        Raises:
            ValueError: The asset kind is unsupported.
        """
        if kind not in {"background", "avatar"}:
            raise ValueError(f"Unsupported asset kind: {kind!r}")
        background = kind == "background"
        fallback = ASSETS / ("default_bg.webp" if background else "default_avatar.webp")
        if background and self.source == "local":
            try:
                if not self.local_path:
                    return image_data_uri(await asyncio.to_thread(fallback.read_bytes))
                path = Path(self.local_path).expanduser()
                candidates = (
                    [item for item in path.iterdir() if item.is_file()]
                    if path.is_dir()
                    else [path]
                )
                random.shuffle(candidates)
                # Keep the previous image as a last resort if all others are invalid.
                candidates.sort(key=lambda item: item == self._last_local)
                for candidate in candidates:
                    try:
                        raw = await asyncio.to_thread(candidate.read_bytes)
                        result = await asyncio.to_thread(image_data_uri, raw)
                    except (OSError, ValueError):
                        continue
                    self._last_local = candidate
                    return result
                raise ValueError("Local path contains no readable supported image")
            except (OSError, ValueError) as exc:
                log.warning(
                    "[sys_tools] Local background unavailable; using packaged image: %s",
                    exc,
                )
            return image_data_uri(await asyncio.to_thread(fallback.read_bytes))
        if background:
            url = (
                "https://www.loliapi.com/acg/pe/" if self.source == "loli" else self.url
            )
        else:
            url = url or self.default_avatar
        if not url.startswith(("https://", "http://")):
            if background:
                log.warning(
                    "[sys_tools] Background URL is missing or invalid; using packaged image"
                )
            return image_data_uri(await asyncio.to_thread(fallback.read_bytes))
        key = f"{kind}-{hashlib.sha256(url.encode()).hexdigest()[:20]}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._memory.get(key)
            if not background and cached and time.monotonic() - cached[0] < 3600:
                return cached[1]
            cache_file = self.cache_dir / f"{key}.img"
            stale = cached[1] if cached else None
            if stale is None:
                try:
                    disk_bytes = await asyncio.to_thread(cache_file.read_bytes)
                    stale = await asyncio.to_thread(image_data_uri, disk_bytes)
                    age = time.time() - cache_file.stat().st_mtime
                    if not background and age < 3600:
                        self._memory[key] = (time.monotonic() - max(0, age), stale)
                        return stale
                except (OSError, ValueError):
                    pass
            try:

                async def download() -> tuple[bytes, str]:
                    # Retry dead random redirects within one overall deadline.
                    attempts = self.retry_limit if background else 1
                    for attempt in range(attempts):
                        request_url = url
                        if background:
                            parts = urlsplit(url)
                            query = parts.query + ("&" if parts.query else "")
                            request_url = urlunsplit(
                                parts._replace(
                                    query=f"{query}_sys_tools_nonce={uuid.uuid4().hex}"
                                )
                            )
                        try:
                            chunks = []
                            total = 0
                            async with httpx.AsyncClient(
                                timeout=self.timeout,
                                follow_redirects=True,
                                max_redirects=10,
                                trust_env=False,
                                proxy=self.proxy if background else None,
                            ) as client:
                                async with client.stream(
                                    "GET", request_url
                                ) as response:
                                    response.raise_for_status()
                                    async for part in response.aiter_bytes():
                                        total += len(part)
                                        if total > MAX_IMAGE_BYTES:
                                            raise ValueError(
                                                "Asset response exceeds 10 MiB"
                                            )
                                        chunks.append(part)
                            raw = b"".join(chunks)
                            encoded = await asyncio.to_thread(image_data_uri, raw)
                            return raw, encoded
                        except (httpx.HTTPError, ValueError):
                            if attempt + 1 == attempts:
                                raise
                    raise RuntimeError("No asset download attempt was configured")

                raw, result = await asyncio.wait_for(download(), timeout=self.timeout)
                self._memory[key] = (time.monotonic(), result)
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(cache_file.write_bytes, raw)
                except OSError as exc:
                    log.debug("[sys_tools] Could not cache image: %s", exc)
                log.debug("[sys_tools] Downloaded fresh %s image", kind)
                return result
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
                log.warning(
                    "[sys_tools] %s fetch failed; using %s image; retry on next load: %s: %s",
                    kind,
                    "last successful cached" if stale else "packaged",
                    type(exc).__name__,
                    exc,
                )
                return stale or image_data_uri(
                    await asyncio.to_thread(fallback.read_bytes)
                )
