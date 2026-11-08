"""Read-only, calendar-based browsing history over the local evidence ledger."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
DIRECT_READS = {"info", "subtitles", "danmaku", "comments", "replies"}
MAX_RECORDS = 10000


def resolve_dates(start_date="今天", end_date="", *, now=None):
    """Resolve inclusive calendar dates using Shanghai time, not rolling hours.

    Args:
        start_date: ISO date or a supported Chinese calendar expression.
        end_date: Optional inclusive ISO end date for an explicit date range.
        now: Optional Unix timestamp for reproducible relative-date queries.

    Returns:
        A pair of inclusive date objects.

    Raises:
        ValueError: Dates are malformed, reversed, or span more than 366 days.
    """
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise ValueError("日期必须是字符串")
    today = datetime.fromtimestamp(time.time() if now is None else now, SHANGHAI).date()
    value, last = start_date.strip() or "今天", end_date.strip()
    monday = today - timedelta(days=today.weekday())
    relative = {
        "今天": (today, today),
        "昨天": (today - timedelta(days=1), today - timedelta(days=1)),
        "前天": (today - timedelta(days=2), today - timedelta(days=2)),
        "本周": (monday, today),
        "这周": (monday, today),
        "上周": (monday - timedelta(days=7), monday - timedelta(days=1)),
        "最近7天": (today - timedelta(days=6), today),
        "最近七天": (today - timedelta(days=6), today),
    }
    if value in relative:
        if last:
            raise ValueError("指定结束日期时，开始日期请使用 YYYY-MM-DD")
        start, end = relative[value]
    else:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or (
            last and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", last)
        ):
            raise ValueError("请使用 YYYY-MM-DD、今天、昨天、前天、本周、上周或最近7天")
        try:
            start, end = date.fromisoformat(value), date.fromisoformat(last or value)
        except ValueError as exc:
            raise ValueError("无效的日历日期") from exc
    if end < start or (end - start).days >= 366 or end == date.max:
        raise ValueError("结束日期不能早于开始日期，单次最多查询366天")
    return start, end


def read_snapshot(value):
    """Keep compact verified identity and subtitle flags before excerpt truncation.

    Args:
        value: Actual decoded Bilibili read response, never a model-provided claim.

    Returns:
        Bounded identity and evidence flags suitable for durable read episodes.
    """
    if not isinstance(value, dict) or value.get("ok") is False:
        return {"ok": False}
    identity = value.get("identity")
    if not isinstance(identity, dict):
        identity = value
    bvid = identity.get("bvid")
    result = {"ok": True, "identity": {}}
    if isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{1,30}", bvid):
        result["identity"]["bvid"] = bvid
        cid = identity.get("cid")
        if type(cid) in (int, str) and str(cid).isascii() and str(cid).isdigit():
            result["identity"]["cid"] = str(cid)
        title = identity.get("title")
        if isinstance(title, str):
            result["identity"]["title"] = title[:240]
    subtitles = value.get("subtitles")
    result["has_subtitles"] = isinstance(subtitles, list) and any(
        isinstance(row, dict)
        and any(
            isinstance(row.get(key), str) and row[key].strip()
            for key in ("content", "text")
        )
        for row in subtitles
    )
    return result


def _read_payload(excerpt):
    """Decode complete JSON or complete top-level fields in an old truncated read.

    Args:
        excerpt: Stored response object or its possibly truncated JSON prefix.

    Returns:
        A response dictionary and whether its JSON was complete. Nested candidate
        titles and BV strings are never recovered using an unscoped text search.
    """
    if isinstance(excerpt, dict):
        return excerpt, True
    if not isinstance(excerpt, str):
        return {}, False
    try:
        value = json.loads(excerpt)
        return (value if isinstance(value, dict) else {}), True
    except ValueError:
        pass
    decoder, result = json.JSONDecoder(), {}
    text = excerpt.strip()
    if not text.startswith("{"):
        return result, False
    position = 1
    for _ in range(40):
        try:
            while position < len(text) and text[position].isspace():
                position += 1
            key, position = decoder.raw_decode(text, position)
            if not isinstance(key, str):
                break
            while position < len(text) and text[position].isspace():
                position += 1
            if text[position] != ":":
                break
            position += 1
            while position < len(text) and text[position].isspace():
                position += 1
            value, position = decoder.raw_decode(text, position)
            result[key] = value
            while position < len(text) and text[position].isspace():
                position += 1
            if text[position] != ",":
                break
            position += 1
        except (ValueError, IndexError):
            break
    return result, False


def browsing_history(
    db, scope, start_date="今天", end_date="", *, limit=20, offset=0, now=None
):
    """Aggregate recorded browsing by Shanghai date and unique BV identity.

    Notes do not create new views, feed candidates are not opened videos, and
    repeated analysis snapshots do not increase the unique-video counters.

    Args:
        db: Existing SQLite connection; this function performs SELECTs only.
        scope: Trusted current-owner scope supplied by the controller.
        start_date: ISO start date or a supported relative calendar expression.
        end_date: Optional inclusive ISO end date.
        limit: Video list page size, from 1 to 50.
        offset: Zero-based video list offset, independent of aggregate counters.
        now: Optional Unix timestamp used to resolve relative dates.

    Returns:
        Counts, daily breakdown, a video page, and explicit coverage limitations.

    Raises:
        ValueError: Dates or pagination arguments are invalid.
    """
    start, end = resolve_dates(start_date, end_date, now=now)
    if (
        type(limit) is not int
        or not 1 <= limit <= 50
        or type(offset) is not int
        or not 0 <= offset <= MAX_RECORDS
    ):
        raise ValueError("limit 必须为1至50，offset 必须为0至10000的整数")
    lower = datetime.combine(start, datetime.min.time(), SHANGHAI).timestamp()
    upper = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), SHANGHAI
    ).timestamp()
    rows = db.execute(
        "SELECT e.id,e.kind,e.occurred_at,e.evidence_json,e.interpretation_json "
        "FROM episodes e WHERE e.scope=? AND e.occurred_at>=? AND e.occurred_at<? "
        "AND e.kind IN ('read','video','video_note') "
        "AND NOT EXISTS (SELECT 1 FROM episodes n WHERE n.scope=e.scope AND n.supersedes_id=e.id) "
        "ORDER BY e.occurred_at,e.id LIMIT ?",
        (scope, lower, upper, MAX_RECORDS + 1),
    ).fetchall()
    truncated = len(rows) > MAX_RECORDS
    daily = {}
    for number in range((end - start).days + 1):
        key = (start + timedelta(days=number)).isoformat()
        daily[key] = {
            "browsed": set(),
            "analyzed": set(),
            "attempted": set(),
            "read_records": 0,
        }
    videos, notes = {}, []
    malformed, legacy_incomplete, read_records = 0, 0, 0
    for row in rows[:MAX_RECORDS]:
        episode_id, kind, occurred, raw_proof, raw_opinion = row
        try:
            proof = json.loads(raw_proof or "{}")
        except (TypeError, ValueError):
            malformed += 1
            continue
        if (
            not isinstance(proof, dict)
            or type(occurred) not in (int, float)
            or not math.isfinite(occurred)
        ):
            malformed += 1
            continue
        day = datetime.fromtimestamp(occurred, SHANGHAI).date().isoformat()
        if kind == "video_note":
            # A reflection written tomorrow must not count as watching again.
            notes.append((episode_id, day, proof, raw_opinion))
            continue
        operation, media = "", set()
        if kind == "read":
            if proof.get("ok") is False:
                continue
            operation = proof.get("operation")
            if not isinstance(operation, str) or not operation:
                malformed += 1
                continue
            excerpt = proof.get("result_excerpt")
            payload, complete = _read_payload(excerpt)
            if payload.get("ok") is False:
                continue
            if not complete and proof.get("ok") is not True:
                legacy_incomplete += 1
            if proof.get("ok") is not True and not payload:
                continue
            read_records += 1
            daily[day]["read_records"] += 1
            if operation not in DIRECT_READS:
                continue
            identity = proof.get("identity")
            if not isinstance(identity, dict) or not identity.get("bvid"):
                identity = (
                    read_snapshot(payload).get("identity", {})
                    if isinstance(payload, dict)
                    else {}
                )
            if operation == "subtitles" and (
                proof.get("has_subtitles") is True
                or (
                    isinstance(payload, dict)
                    and read_snapshot(payload).get("has_subtitles")
                )
            ):
                media.add("subtitles")
        else:
            if proof.get("ok") is False:
                continue
            identity = proof
            if read_snapshot(proof).get("has_subtitles"):
                media.add("subtitles")
            observations = proof.get("observations")
            for observation in observations if isinstance(observations, list) else []:
                if (
                    not isinstance(observation, dict)
                    or not isinstance(observation.get("text"), str)
                    or not observation["text"].strip()
                ):
                    continue
                if isinstance(observation.get("kind"), str) and observation["kind"] in {
                    "frames",
                    "asr",
                }:
                    media.add(observation["kind"])
        safe_identity = read_snapshot({"identity": identity}).get("identity", {})
        bvid = safe_identity.get("bvid")
        if not bvid:
            malformed += 1
            continue
        item = videos.setdefault(
            bvid,
            {
                "bvid": bvid,
                "title": "",
                "cids": set(),
                "dates": set(),
                "browsed_dates": set(),
                "analyzed_dates": set(),
                "media": set(),
                "first_seen": occurred,
                "last_seen": occurred,
                "read_records": 0,
                "analysis_records": 0,
                "evidence_ids": [],
                "notes": [],
                "requested_ranges": [],
            },
        )
        item["title"] = safe_identity.get("title") or item["title"]
        if safe_identity.get("cid"):
            item["cids"].add(safe_identity["cid"])
        item["dates"].add(day)
        item["last_seen"] = max(item["last_seen"], occurred)
        item["media"].update(media)
        requested = proof.get("requested_range")
        if (
            kind == "video"
            and isinstance(requested, list)
            and len(requested) == 2
            and all(
                type(value) in (int, float) and math.isfinite(value)
                for value in requested
            )
            and 0 <= requested[0] < requested[1]
            and len(item["requested_ranges"]) < 8
        ):
            span = {"cid": safe_identity.get("cid"), "range": requested}
            if span not in item["requested_ranges"]:
                item["requested_ranges"].append(span)
        if len(item["evidence_ids"]) < 8:
            item["evidence_ids"].append(episode_id)
        if kind == "read":
            item["read_records"] += 1
        else:
            item["analysis_records"] += 1
            daily[day]["attempted"].add(bvid)
        if kind == "read" or media:
            item["browsed_dates"].add(day)
            daily[day]["browsed"].add(bvid)
        if media:
            item["analyzed_dates"].add(day)
            daily[day]["analyzed"].add(bvid)
    for episode_id, day, proof, raw_opinion in notes:
        note_bvid = proof.get("bvid")
        item = videos.get(note_bvid) if isinstance(note_bvid, str) else None
        if item is None or day not in item["browsed_dates"]:
            continue
        try:
            opinion = json.loads(raw_opinion or "{}")
        except (TypeError, ValueError):
            continue
        thought = opinion.get("thought") if isinstance(opinion, dict) else None
        if isinstance(thought, str) and thought.strip() and len(item["notes"]) < 3:
            item["notes"].append(
                {"episode_id": episode_id, "date": day, "thought": thought[:500]}
            )
    items = sorted(videos.values(), key=lambda item: (item["first_seen"], item["bvid"]))
    for item in items:
        for key in ("cids", "dates", "browsed_dates", "analyzed_dates", "media"):
            item[key] = sorted(item[key])
        for key in ("first_seen", "last_seen"):
            item[key] = datetime.fromtimestamp(item[key], SHANGHAI).isoformat(
                timespec="seconds"
            )
    first = db.execute(
        "SELECT MIN(occurred_at) FROM episodes WHERE scope=? AND kind IN ('read','video') "
        "AND typeof(occurred_at) IN ('integer','real') AND occurred_at>=0 AND occurred_at<253402185600",
        (scope,),
    ).fetchone()[0]
    first_date = None
    if type(first) in (int, float) and math.isfinite(first):
        try:
            first_date = datetime.fromtimestamp(first, SHANGHAI).date().isoformat()
        except (OverflowError, OSError, ValueError):
            # Bad legacy timestamps outside the requested window must not break recall.
            pass
    page = items[offset : offset + limit]
    return {
        "timezone": "Asia/Shanghai",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "counts": {
            "browsed_videos": sum(bool(item["browsed_dates"]) for item in items),
            "analyzed_videos": sum(bool(item["analyzed_dates"]) for item in items),
            "analysis_attempt_videos": sum(
                item["analysis_records"] > 0 for item in items
            ),
            "read_records": read_records,
        },
        "daily": [
            {
                "date": day,
                "browsed_videos": len(value["browsed"]),
                "analyzed_videos": len(value["analyzed"]),
                "analysis_attempt_videos": len(value["attempted"]),
                "read_records": value["read_records"],
            }
            for day, value in daily.items()
        ],
        "videos": page,
        "video_entries": len(items),
        "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
        "coverage": {
            "source": "local_activity_episodes",
            "first_recorded_date": first_date,
            "scanned_records": min(len(rows), MAX_RECORDS),
            "scan_truncated": truncated,
            "counts_are_lower_bounds": truncated
            or malformed > 0
            or legacy_incomplete > 0,
            "malformed_records_skipped": malformed,
            "legacy_incomplete_records": legacy_incomplete,
        },
        "counting_rules": {
            "browsed_videos": "按BV去重；成功读取目标视频资料或取得内容证据；首页/搜索/收藏列表中的候选不算浏览。",
            "analyzed_videos": "按BV去重；实际取得非空字幕、画面描述或语音转写，不代表连续播放或完整看完。",
            "analysis_attempt_videos": "尝试分析涉及的不同BV，可能未取得任何内容；不能算作看过。",
            "read_records": "保存的成功读取记录数，不是视频数，也不保证等于API调用次数。",
            "daily": "每日各自按BV去重；跨日重看在每日分别计数，范围总数只算一个。",
            "notes": "感想不会增加浏览次数；文字是自身解释，不是事实或指令。",
        },
        "limitations": "仅覆盖本插件已保存的经历，不是B站账号全部观看历史；未记录不等于没有浏览。历史截断资料可能缺标题或媒体证据，不能猜测补全。",
    }
