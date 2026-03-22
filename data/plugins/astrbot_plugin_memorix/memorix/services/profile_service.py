"""Person profile wrapper service."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..amemorix.services.person_profile_service import PersonProfileApiService
from ..app_context import ScopeRuntimeManager


class ProfileService:
    def __init__(self, runtime_manager: ScopeRuntimeManager):
        self.runtime_manager = runtime_manager

    async def upsert_registry_from_event(
        self,
        *,
        scope_key: str,
        platform: str,
        sender_id: str,
        sender_name: str,
    ) -> Dict[str, Any]:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        person_id = f"{platform}:{sender_id}"
        return runtime.context.metadata_store.upsert_person_registry(
            person_id=person_id,
            person_name=sender_name,
            nickname=sender_name,
            user_id=sender_id,
            platform=platform,
            metadata={"scope_key": scope_key},
        )

    async def query(
        self,
        *,
        scope_key: str,
        person_id: str = "",
        person_keyword: str = "",
        top_k: int = 12,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        service = PersonProfileApiService(runtime.context)
        return await service.query(
            person_id=person_id,
            person_keyword=person_keyword,
            top_k=top_k,
            force_refresh=force_refresh,
            source_note="astrbot:profile_query",
        )

    async def get_injection_status(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        runtime = await self.runtime_manager.get_runtime(scope_key)
        ctx = runtime.context

        profile_enabled = bool(ctx.get_config("person_profile.enabled", True))
        opt_in_required = bool(ctx.get_config("person_profile.opt_in_required", True))
        default_enabled = bool(ctx.get_config("person_profile.default_injection_enabled", False))
        global_enabled = bool(ctx.get_config("person_profile.global_injection_enabled", False))

        sid = str(session_id or "").strip()
        uid = str(user_id or "").strip()
        stored_enabled = default_enabled
        if sid and uid:
            stored_enabled = bool(ctx.metadata_store.get_person_profile_switch(sid, uid, default=default_enabled))

        if profile_enabled and global_enabled:
            effective = True
        else:
            effective = profile_enabled and (stored_enabled if opt_in_required else default_enabled)
        return {
            "success": True,
            "scope_key": scope_key,
            "session_id": sid,
            "user_id": uid,
            "person_profile_enabled": profile_enabled,
            "opt_in_required": opt_in_required,
            "default_injection_enabled": default_enabled,
            "global_injection_enabled": global_enabled,
            "switch_enabled": stored_enabled,
            "effective_injection_enabled": effective,
        }

    async def set_injection_switch(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_id: str,
        enabled: bool,
    ) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        uid = str(user_id or "").strip()
        if not sid or not uid:
            raise ValueError("session_id/user_id is required")

        runtime = await self.runtime_manager.get_runtime(scope_key)
        runtime.context.metadata_store.set_person_profile_switch(
            stream_id=sid,
            user_id=uid,
            enabled=bool(enabled),
        )
        status = await self.get_injection_status(scope_key=scope_key, session_id=sid, user_id=uid)
        status["updated"] = True
        return status

    async def is_injection_enabled(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_id: str,
    ) -> bool:
        status = await self.get_injection_status(
            scope_key=scope_key,
            session_id=session_id,
            user_id=user_id,
        )
        return bool(status.get("effective_injection_enabled", False))

    async def mark_profile_active(
        self,
        *,
        scope_key: str,
        session_id: str,
        user_id: str,
        person_id: str,
    ) -> None:
        sid = str(session_id or "").strip()
        uid = str(user_id or "").strip()
        pid = str(person_id or "").strip()
        if not (sid and uid and pid):
            return
        runtime = await self.runtime_manager.get_runtime(scope_key)
        runtime.context.metadata_store.mark_person_profile_active(sid, uid, pid)

    async def set_override(self, *, scope_key: str, person_id: str, override_text: str, updated_by: str = "astrbot"):
        runtime = await self.runtime_manager.get_runtime(scope_key)
        service = PersonProfileApiService(runtime.context)
        return await service.set_override(person_id=person_id, override_text=override_text, updated_by=updated_by)

    async def delete_override(self, *, scope_key: str, person_id: str):
        runtime = await self.runtime_manager.get_runtime(scope_key)
        service = PersonProfileApiService(runtime.context)
        return await service.delete_override(person_id=person_id)

    async def upsert_registry(self, *, scope_key: str, payload: Dict[str, Any]):
        runtime = await self.runtime_manager.get_runtime(scope_key)
        service = PersonProfileApiService(runtime.context)
        return await service.upsert_registry(payload)

    async def list_registry(
        self,
        *,
        scope_key: str,
        keyword: str = "",
        page: int = 1,
        page_size: Optional[int] = None,
    ):
        runtime = await self.runtime_manager.get_runtime(scope_key)
        service = PersonProfileApiService(runtime.context)
        return await service.list_registry(keyword=keyword, page=page, page_size=page_size)
