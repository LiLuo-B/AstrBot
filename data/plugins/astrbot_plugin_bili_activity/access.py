"""Shared activity scopes; only account writes require the owner's private chat."""

from __future__ import annotations

import re


def group_access(event, cfg):
    """Return whether a real group event belongs to the configured adapter."""
    scope = str(event.unified_msg_origin)
    target = str(cfg.get("owner_scope", ""))
    return (not event.get_extra("df_run_id")
            and re.fullmatch(r"[^:]+:GroupMessage:[0-9]+", scope) is not None
            and re.fullmatch(r"[^:]+:FriendMessage:[0-9]+", target) is not None
            and scope.split(":", 1)[0] == target.split(":", 1)[0]
            and cfg.get("group_history_enabled") is True)
