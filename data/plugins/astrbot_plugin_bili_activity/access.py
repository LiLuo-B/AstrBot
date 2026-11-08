"""Group access boundaries for public activity history and owner account actions."""

from __future__ import annotations

import re


def group_access(event, cfg, *, owner=False):
    """Check trusted group identity without accepting model-provided scopes.

    Args:
        event: Platform event carrying the real sender and adapter identity.
        cfg: Current controller configuration.
        owner: Require explicit account-action permission and the configured owner.

    Returns:
        Whether this foreground group event may use the requested capability.
    """
    scope = str(event.unified_msg_origin)
    target = str(cfg.get("owner_scope", ""))
    if event.get_extra("df_run_id") or not re.fullmatch(r"[^:]+:GroupMessage:[0-9]+", scope):
        return False
    if not re.fullmatch(r"[^:]+:FriendMessage:[0-9]+", target):
        return False
    if scope.split(":", 1)[0] != target.split(":", 1)[0]:
        return False
    if owner:
        return (cfg.get("group_owner_actions_enabled") is True
                and str(event.get_sender_id()) == str(cfg.get("owner_id"))
                and target.rsplit(":", 1)[-1] == str(cfg.get("owner_id")))
    return cfg.get("group_history_enabled") is True


def public_history(result):
    """Expose viewing statistics and public video metadata, never private notes.

    Args:
        result: Owner-ledger history returned by the trusted local query.

    Returns:
        A new, whitelisted group-safe history response without episode identifiers,
        reflections, goals, private preferences or arbitrary evidence bodies.
    """
    fields = ("timezone", "start_date", "end_date", "counts", "daily", "video_entries",
              "next_offset", "coverage", "counting_rules", "limitations")
    response = {key: result[key] for key in fields if key in result}
    video_fields = ("bvid", "title", "cids", "dates", "browsed_dates", "analyzed_dates",
                    "media", "first_seen", "last_seen")
    response["videos"] = [{key: video[key] for key in video_fields if key in video}
                          for video in result.get("videos", [])]
    response["visibility"] = "public_browsing_history_only"
    return response
