"""Summary import service wrappers."""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List

from astrbot.api import logger
from ...core.utils.summary_importer import SummaryImporter
from ...providers.astrbot_provider_bridge import AstrBotLLMClient
from ..context import AppContext
from ..llm_client import LLMClient
from ..settings import resolve_openapi_endpoint_config


class SummaryService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        provider_bridge = getattr(self.ctx, "provider_bridge", None)
        if provider_bridge is not None and getattr(provider_bridge, "enabled", False):
            retries = int(self.ctx.get_config("embedding.retry.max_attempts", 3) or 3)
            self.llm_client = AstrBotLLMClient(provider_bridge=provider_bridge, max_retries=max(1, retries))
            logger.info("summary service uses AstrBot native chat provider")
        else:
            endpoint_cfg = resolve_openapi_endpoint_config(self.ctx.config, section="embedding")
            self.llm_client = LLMClient(
                base_url=str(endpoint_cfg.get("base_url", "")),
                api_key=str(endpoint_cfg.get("api_key", "")),
                model=str(
                    endpoint_cfg.get("chat_model", "")
                    or endpoint_cfg.get("model", "")
                    or "gpt-4o-mini"
                ),
                timeout_seconds=float(endpoint_cfg.get("timeout_seconds", 60) or 60),
                max_retries=int(endpoint_cfg.get("max_retries", 3) or 3),
            )
            logger.warning("summary service fallback to OpenAI-compatible client (provider bridge unavailable)")
        self.importer = SummaryImporter(
            vector_store=self.ctx.vector_store,
            graph_store=self.ctx.graph_store,
            metadata_store=self.ctx.metadata_store,
            embedding_manager=self.ctx.embedding_manager,
            plugin_config=self.ctx.config,
            llm_client=self.llm_client,
            astrbot_context=self._resolve_astrbot_context(),
        )

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.ctx.get_config(key, default)

    @staticmethod
    async def _invoke_maybe_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
        value = fn(*args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _normalize_source_mode(raw: Any) -> str:
        mode = str(raw or "hybrid").strip().lower()
        if mode not in {"transcript", "astrbot", "hybrid"}:
            return "hybrid"
        return mode

    @staticmethod
    def _content_to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            pieces: List[str] = []
            for item in value:
                text = SummaryService._content_to_text(item)
                if text:
                    pieces.append(text)
            return " ".join(pieces).strip()
        if isinstance(value, dict):
            if "text" in value:
                return SummaryService._content_to_text(value.get("text"))
            if "content" in value:
                return SummaryService._content_to_text(value.get("content"))
            if "parts" in value:
                return SummaryService._content_to_text(value.get("parts"))
        return str(value).strip()

    @classmethod
    def _normalize_message(cls, item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        role = str(item.get("role", "") or item.get("type", "") or item.get("sender", "") or "user").strip().lower()
        if role not in {"user", "assistant", "system", "tool"}:
            if "assistant" in role:
                role = "assistant"
            elif "system" in role:
                role = "system"
            elif "tool" in role:
                role = "tool"
            else:
                role = "user"

        content = cls._content_to_text(item.get("content"))
        if not content:
            for key in ("text", "message", "value"):
                content = cls._content_to_text(item.get(key))
                if content:
                    break
        if not content:
            return {}

        return {
            "role": role,
            "content": content,
            "timestamp": item.get("timestamp") or item.get("time") or item.get("ts"),
            "metadata": item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {},
        }

    @classmethod
    def _normalize_messages(cls, raw_messages: Any, context_length: int) -> List[Dict[str, Any]]:
        if isinstance(raw_messages, str):
            text = raw_messages.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                return [{"role": "user", "content": text, "timestamp": None, "metadata": {}}]
            return cls._normalize_messages(parsed, context_length)

        if isinstance(raw_messages, dict):
            for key in ("history", "messages", "items"):
                if key in raw_messages:
                    return cls._normalize_messages(raw_messages.get(key), context_length)
            normalized = cls._normalize_message(raw_messages)
            return [normalized] if normalized else []

        if not isinstance(raw_messages, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for item in raw_messages:
            row = cls._normalize_message(item)
            if row:
                normalized.append(row)
        if context_length > 0:
            return normalized[-context_length:]
        return normalized

    def _resolve_astrbot_context(self) -> Any:
        ctx_obj = getattr(self.ctx, "astrbot_context", None)
        if ctx_obj is not None:
            return ctx_obj
        bridge = getattr(self.ctx, "provider_bridge", None)
        if bridge is None:
            return None
        return getattr(bridge, "_context", None)

    def _resolve_umo_candidates(self, session_id: str) -> List[str]:
        candidates: List[str] = []
        transcript_session = self.ctx.metadata_store.get_transcript_session(session_id)
        if transcript_session and isinstance(transcript_session.get("metadata"), dict):
            metadata = transcript_session.get("metadata") or {}
            umo = str(metadata.get("unified_msg_origin", "") or "").strip()
            if umo:
                candidates.append(umo)

        sid = str(session_id or "").strip()
        if sid:
            candidates.append(sid)

        deduped: List[str] = []
        seen = set()
        for item in candidates:
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    async def _fetch_astrbot_messages(self, *, session_id: str, context_length: int) -> List[Dict[str, Any]]:
        astrbot_ctx = self._resolve_astrbot_context()
        if astrbot_ctx is None:
            return []

        conversation_manager = getattr(astrbot_ctx, "conversation_manager", None)
        if conversation_manager is None:
            return []

        get_curr_cid = getattr(conversation_manager, "get_curr_conversation_id", None)
        get_conversation = getattr(conversation_manager, "get_conversation", None)
        if not callable(get_curr_cid) or not callable(get_conversation):
            return []

        for umo in self._resolve_umo_candidates(session_id):
            try:
                conversation_id = await self._invoke_maybe_async(get_curr_cid, umo)
                if not conversation_id:
                    continue
                try:
                    conversation = await self._invoke_maybe_async(get_conversation, umo, conversation_id, False)
                except TypeError:
                    conversation = await self._invoke_maybe_async(get_conversation, umo, conversation_id)
                history = getattr(conversation, "history", None) if conversation is not None else None
                messages = self._normalize_messages(history, context_length)
                if messages:
                    logger.info(
                        "summary source selected: astrbot history, session=%s umo=%s messages=%s",
                        session_id,
                        umo,
                        len(messages),
                    )
                    return messages
            except Exception:
                logger.debug("load astrbot conversation failed: session=%s umo=%s", session_id, umo, exc_info=True)
                continue
        return []

    async def _resolve_summary_messages(
        self,
        *,
        session_id: str,
        incoming_messages: List[Dict[str, Any]],
        context_length: int,
    ) -> List[Dict[str, Any]]:
        normalized = self._normalize_messages(incoming_messages, context_length)
        if normalized:
            return normalized[-max(1, context_length) :]

        mode = self._normalize_source_mode(self._cfg("summarization.source_mode", "hybrid"))
        if mode in {"astrbot", "hybrid"}:
            astrbot_messages = await self._fetch_astrbot_messages(
                session_id=session_id,
                context_length=max(1, context_length),
            )
            if astrbot_messages:
                return astrbot_messages

        if mode in {"transcript", "hybrid"}:
            transcript_messages = self.ctx.metadata_store.get_transcript_messages(
                session_id=session_id,
                limit=max(1, context_length),
            )
            if transcript_messages:
                logger.info(
                    "summary source selected: transcript history, session=%s messages=%s",
                    session_id,
                    len(transcript_messages),
                )
            return transcript_messages

        return []

    async def import_from_transcript(
        self,
        *,
        session_id: str,
        messages: List[Dict[str, Any]],
        source: str = "",
        context_length: int = 50,
        persist_messages: bool = False,
    ) -> Dict[str, Any]:
        resolved_context_length = max(1, int(context_length))
        source_mode = self._normalize_source_mode(self._cfg("summarization.source_mode", "hybrid"))
        summary_messages = await self._resolve_summary_messages(
            session_id=session_id,
            incoming_messages=messages,
            context_length=resolved_context_length,
        )
        if source_mode == "astrbot" and not summary_messages:
            return {"success": False, "message": "未找到可用的 AstrBot 会话历史"}
        ok, msg = await self.importer.import_from_transcript(
            session_id=session_id,
            messages=summary_messages,
            source=source,
            context_length=resolved_context_length,
            persist_messages=persist_messages,
        )
        return {"success": ok, "message": msg}
