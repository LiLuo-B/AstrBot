"""Render the live plugin through a WebChat tool request without changing routing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx


def run() -> None:
    """Verify image SSE delivery using an explicitly selected test provider."""
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    dashboard = config["dashboard"]

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    body = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 600}
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(dashboard["jwt_secret"].encode(), body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    destination = Path("data/plugin_data/astrbot_plugin_sys_tools/previews")
    destination.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": f"Bearer {body}.{signature}"},
        timeout=120,
        trust_env=False,
    ) as client:
        response = client.get("/api/v1/chat/sessions/new?platform_id=webchat")
        response.raise_for_status()
        session = response.json()["data"]["session_id"]
        payload = {
            "session_id": session,
            "platform_id": "webchat",
            "selected_provider": "newapi/gemini-3.8-flash",
            "message": "状态",
        }
        images = []
        errors = []
        types = []
        with client.stream("POST", "/api/chat/send", json=payload) as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type")
                types.append(kind)
                value = str(event.get("data") or "")
                if kind == "image":
                    images.append(event)
                if "LLM 响应错误" in value:
                    errors.append(value[:200])
        result = {"session": session, "images": images, "errors": errors, "event_types": types}
        (destination / "live-delivery.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print("Image events:", len(images), "errors:", errors, "event types:", sorted(set(types)))
        assert not errors and len(images) == 1, result
        print("Image event:", images[0])


if __name__ == "__main__":
    run()
