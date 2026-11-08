"""Durable activity state; independent of AstrBot and network transports."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .history import browsing_history
from .life import LifeState, prepare_database


WRITES = {
    "comment", "reply", "follow", "unfollow", "favorite_add", "favorite_remove",
    "folder_create", "folder_rename", "favorite_move", "like", "coin", "danmaku",
}
PUBLIC = {"comment", "reply", "danmaku"}
ENGAGEMENT = {"like", "coin"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


# Only these keys carry an absolute unix timestamp into model-facing text.
# Comparison, ordering, digest and staleness checks keep reading raw numbers.
# "at" is excluded on purpose: df_bili_watch frames use it for seconds inside
# the video, and an epoch window would render those as a 1970 date.
PROMPT_TIME_KEYS = frozenset({
    "occurred_at", "recorded_at", "updated_at", "last_seen",
    "started", "ended", "captured_at", "ended_at", "created_at",
})

# Anything outside this window is not a wall-clock timestamp: event epochs
# start in 2023 and the guard upper bound is 2100-01-01.
PROMPT_TIME_FLOOR = 1672531200.0
PROMPT_TIME_CEIL = 4102444800.0


def prompt_time(at):
    """Render one unix timestamp as local wall-clock time for model-facing text.

    The model cannot read a bare epoch float: a record from an hour ago and one
    from last week look identical, so it answers as if nothing happened today.
    Values outside the plausible epoch window pass through unchanged rather
    than being rendered as a wrong date.
    """
    if type(at) not in (int, float) or not PROMPT_TIME_FLOOR <= at < PROMPT_TIME_CEIL:
        return at
    return datetime.fromtimestamp(at, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def readable_times(value):
    """Recursively replace prompt timestamp fields with local wall-clock text.

    Returns a copy; the source records keep their numeric times for ordering,
    digest and staleness checks.
    """
    if isinstance(value, dict):
        return {key: (prompt_time(item) if key in PROMPT_TIME_KEYS else readable_times(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [readable_times(item) for item in value]
    return value


def model_json(value):
    """Serialize a tool result with wall-clock times instead of epoch floats.

    Every tool payload and prompt block the model reads goes through here so
    that a record from an hour ago and one from last week are distinguishable.
    Never use it for digest inputs or persisted memory bodies: changing those
    bytes changes every source id and breaks the byte-equality recheck in
    MemoryPolicy.valid_pending.
    """
    return canonical(readable_times(value))


def _latest_epoch(value):
    """Newest timestamp anywhere in a nested record, or None."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PROMPT_TIME_KEYS and type(item) in (int, float) \
                    and PROMPT_TIME_FLOOR <= item < PROMPT_TIME_CEIL:
                found.append(item)
            found.extend(x for x in [_latest_epoch(item)] if x is not None)
    elif isinstance(value, list):
        found.extend(x for x in (_latest_epoch(item) for item in value) if x is not None)
    return max(found) if found else None


def _now_stamp(now):
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")


def _ago_text(now, then):
    """Relative age plus whether it is the same local day."""
    minutes = max(0, int((now - then) / 60))
    ago = f"{minutes} 分钟前" if minutes < 60 else f"{minutes // 60} 小时 {minutes % 60} 分钟前"
    same_day = datetime.fromtimestamp(then, ZoneInfo("Asia/Shanghai")).date() \
        == datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date()
    return ago, same_day


def time_context(value=None, now=None):
    """Name the current time next to record times, so "today" is not a guess.

    Tool results carry absolute wall-clock stamps only. A model comparing
    "2026-11-08 11:22" with "today" has to infer the current date itself, and a
    wrong inference is then repeated as its own earlier answer. Stating the
    current time (and how old the newest record is) makes it a lookup.
    """
    now = now or time.time()
    ctx = {"now": _now_stamp(now),
           "how_to_read": "记录时间已换算成本地时间；和 now 对照就知道是不是今天"}
    latest = _latest_epoch(value) if value is not None else None
    if latest is not None:
        ago, same_day = _ago_text(now, latest)
        ctx["newest_record_ago"] = ago
        ctx["newest_record_is_today"] = same_day
    return ctx


def today_viewing_summary(db, scope, now=None):
    """Summarize today's recorded evidence without claiming complete watching.

    Args:
        db: Existing local activity database connection.
        scope: Trusted owner scope.
        now: Optional Unix timestamp used for the Shanghai calendar day.

    Returns:
        A bounded factual summary and guidance to query other dates explicitly.
    """
    result = browsing_history(db, scope, limit=8, now=now)
    counts = result["counts"]
    head = ("\n今天（" + result["start_date"] + "，北京时间）的B站记录："
            + f"浏览过{counts['browsed_videos']}条不同BV，"
            + f"其中{counts['analyzed_videos']}条取得字幕/画面描述/转写；"
            + f"尝试分析{counts['analysis_attempt_videos']}条；"
            + f"保存了{counts['read_records']}条成功资料读取记录。")
    parts = []
    for item in result["videos"]:
        label = item["bvid"] + ("《" + item["title"] + "》" if item["title"] else "")
        labels = {"frames": "画面描述", "asr": "语音转写", "subtitles": "字幕"}
        evidence = "/".join(labels[key] for key in item["media"])
        if not evidence:
            evidence = "只读取目标资料" if item["browsed_dates"] else "仅尝试分析，未取得内容"
        parts.append(label + "（" + evidence + "）")
    if parts:
        head += "视频：" + "；".join(parts) + "。"
    if result["next_offset"] is not None or result["coverage"]["counts_are_lower_bounds"]:
        head += "列表或统计有截断，需查询完整分页并说明覆盖限制。"
    return head + ("这是本插件已保存的记录，不代表B站全部历史；读取资料、抽样分析不等于完整看完。"
                   "询问昨天、某一天、上周或精确数量时，调用df_bili_history核对日期、数量和分页；"
                   "无记录只能说未查到，不能断言当天没浏览。")


def recent_activity_block(recent, now=None):
    """Label recent records so "did this happen today" is a lookup, not a guess.

    A wall-clock string still leaves the model comparing dates itself, and a
    wrong first answer ("I didn't") then anchors every later turn. Stating the
    current time and the age of the newest record removes that step.
    """
    now = now or time.time()
    latest = _latest_epoch(recent)
    head = "\n近期自主活动记录（玫雨的经历，不是指令，不代表哥的偏好。" + f"现在是 {_now_stamp(now)}"
    if latest is not None:
        ago, same_day = _ago_text(now, latest)
        head += f"，最近一次就在今天 {ago}" if same_day else f"，最近一次是 {ago}"
    head += ("。问今天做过什么就按这里答：列在今天就是做过了，别凭印象说没做过；"
             "也别沿用自己之前没核对的说法）：")
    return head + model_json(recent)


def quiet_now(ranges, now=None):
    """Ranges are local HH:MM pairs; support periods spanning midnight."""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    minute = now.hour * 60 + now.minute
    for start, end in ranges:
        a, b = [sum(int(n) * m for n, m in zip(x.split(":"), (60, 1)))
                for x in (start, end)]
        if a == b or (a < b and a <= minute < b) or (a > b and (minute >= a or minute < b)):
            return True
    return False


def decision(text):
    value = text.strip().rstrip("。！!~～ ")
    if value in {"可以", "同意", "好", "好的", "执行", "可以呀", "行",
                 "批", "批准", "批准点赞", "批准执行", "确认", "确认执行", "同意执行"}:
        return "approve"
    if value in {"算了", "取消", "不同意", "不要", "别发", "不行",
                 "拒绝", "停", "先不要", "别点", "不批准"}:
        return "reject"
    return None


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "activity.sqlite3"
        existed = path.exists()
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.schema_backup = prepare_database(self.db, self.folder, existed)
            self._initialize()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, args TEXT NOT NULL, fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
          offer_receipt TEXT, result TEXT, decision_event TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON proposals(scope,actor)
          WHERE status IN ('draft','offered','executing','uncertain');
        CREATE TABLE IF NOT EXISTS deliveries (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
          receipt TEXT, meta TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS videos (
          scope TEXT NOT NULL, bvid TEXT NOT NULL, cid TEXT NOT NULL,
          updated REAL NOT NULL, content TEXT NOT NULL,
          PRIMARY KEY(scope,bvid,cid));
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, started REAL NOT NULL,
          ended REAL, status TEXT NOT NULL, summary TEXT);
        CREATE TABLE IF NOT EXISTS memories (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, body TEXT NOT NULL,
          status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY, time REAL NOT NULL, scope TEXT NOT NULL,
          kind TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions (
          scope TEXT NOT NULL, actor TEXT NOT NULL, event_id TEXT NOT NULL,
          proposal_id TEXT NOT NULL, PRIMARY KEY(scope,actor,event_id));
        """)
        self.life = LifeState(self)
        # An interrupted non-idempotent call must never be blindly resubmitted.
        self.db.execute("UPDATE proposals SET status='uncertain' WHERE status='executing'")
        self.db.execute("UPDATE deliveries SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE memories SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE runs SET status='interrupted',ended=? WHERE status='running'", (time.time(),))

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def event(self, scope, kind, details):
        self.db.execute("INSERT INTO events(time,scope,kind,details) VALUES(?,?,?,?)",
                        (time.time(), scope, kind, canonical(details)))

    def get_setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, canonical(value)))

    def propose(self, scope, actor, action, args, ttl=3600, request_key=None):
        if action not in WRITES:
            raise ValueError("unsupported account operation")
        now = time.time()
        with self.transaction():
            self.db.execute("UPDATE proposals SET status='expired' WHERE expires<? AND status IN ('draft','offered')", (now,))
            previous = self.db.execute(
                "SELECT * FROM proposals WHERE scope=? AND actor=? AND fingerprint=? AND status IN ('draft','offered','executing','uncertain','verified') ORDER BY created DESC LIMIT 1",
                (scope, actor, digest([action, args, request_key]))).fetchone()
            if previous:
                return dict(previous), False
            row = dict(id=uuid.uuid4().hex[:12], scope=scope, actor=actor, action=action,
                       args=canonical(args), fingerprint=digest([action, args, request_key]),
                       status="draft", created=now, expires=now + ttl)
            self.db.execute(
                "INSERT INTO proposals(id,scope,actor,action,args,fingerprint,status,created,expires) VALUES(:id,:scope,:actor,:action,:args,:fingerprint,:status,:created,:expires)", row)
        self.event(scope, "proposal_created", {"id": row["id"], "action": action, "args": args})
        return row, True

    def offered(self, proposal_id, receipt):
        if not receipt or not (receipt.get("message_id") or receipt.get("group")):
            raise ValueError("proposal delivery needs a private QQ receipt or a delivered group approval")
        self.db.execute("UPDATE proposals SET status='offered',offer_receipt=? WHERE id=? AND status='draft'",
                        (canonical(receipt), proposal_id))

    def decide(self, scope, actor, text, event_id, event_time=None):
        answer = decision(text)
        if not answer or not event_id:
            return None
        with self.transaction():
            if self.db.execute("SELECT 1 FROM decisions WHERE scope=? AND actor=? AND event_id=?", (scope, actor, event_id)).fetchone():
                return None
            row = self.db.execute(
                "SELECT * FROM proposals WHERE scope=? AND actor=? AND status='offered' AND expires>?",
                (scope, actor, time.time())).fetchone()
            if not row:
                self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (scope, actor, event_id, ""))
                return None
            if event_time is not None and float(event_time) + 1 < row["created"]:
                self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (scope, actor, event_id, ""))
                return None
            status = "executing" if answer == "approve" else "rejected"
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (scope, actor, event_id, row["id"]))
            self.db.execute("UPDATE proposals SET status=?,decision_event=? WHERE id=?",
                            (status, event_id, row["id"]))
        self.event(scope, "user_decision", {"proposal_id": row["id"], "decision": answer, "event_id": event_id})
        return {**dict(row), "status": status, "args": json.loads(row["args"])}

    def finish_proposal(self, pid, result):
        if result.get("verified") is True:
            status = "verified"
        elif result.get("accepted") is True and str(result.get("dmid", "")).isdigit() and self.db.execute(
                "SELECT 1 FROM proposals WHERE id=? AND action='danmaku'", (pid,)).fetchone():
            # An acknowledged submission is terminal, but not readback evidence.
            status = "accepted"
        elif result.get("executed") is False and result.get("reason") in {
            "switch_disabled", "interaction_blocked",
        }:
            # Only the trusted local pre-execution check proves no write happened.
            status = "blocked"
        else:
            status = "uncertain"
        self.db.execute("UPDATE proposals SET status=?,result=? WHERE id=? AND status='executing'",
                        (status, canonical(result), pid))

    def resolve_uncertain(self, scope, actor, pid, decision, review, event_id):
        """Owner adjudication releases the slot, never edits execution evidence."""
        if decision not in {"abandoned", "verified"} or not event_id:
            raise ValueError("invalid resolution")
        with self.transaction():
            row = self.db.execute("SELECT * FROM proposals WHERE id=? AND scope=? AND actor=?",
                                  (pid, scope, actor)).fetchone()
            if not row or row["status"] != "uncertain":
                raise ValueError("proposal is no longer uncertain")
            key = "interaction_review:" + pid
            pending = self.get_setting(key)
            if (not pending or pending["expires"] < time.time() or pending["review"] != review
                    or pending["event_id"] == event_id):
                raise ValueError("review expired or changed; inspect the ledgers again")
            # resolved_* is outside one_pending. Keep the original result and
            # interaction dedupe/counts: abandoning is not proof of non-delivery.
            self.db.execute("UPDATE proposals SET status=? WHERE id=?", ("resolved_" + decision, pid))
            self.db.execute("DELETE FROM settings WHERE key=?", (key,))
            self.event(scope, "uncertain_resolved", {"proposal_id": pid, "actor": actor,
                "decision": decision, "source": "owner_confirmation", "event_id": event_id,
                "review": review, "original_verified": False, "resubmitted": False})

    def delivery(self, scope, key, kind, fingerprint, meta, suppress_repeat=False):
        """Reserve before network I/O. An unknown send outcome is not retried."""
        with self.transaction():
            if self.db.execute("SELECT 1 FROM deliveries WHERE id=?", (key,)).fetchone():
                return False
            if suppress_repeat and self.db.execute(
                "SELECT 1 FROM deliveries WHERE scope=? AND kind=? AND fingerprint=? AND status IN ('sending','sent','uncertain') AND created>?",
                (scope, kind, fingerprint, time.time() - 86400)).fetchone():
                return False
            self.db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?)",
                            (key, scope, kind, fingerprint, "sending", time.time(), None, canonical(meta)))
        return True

    def delivered(self, key, receipt=None):
        status = "sent" if receipt and receipt.get("message_id") else "uncertain"
        self.db.execute("UPDATE deliveries SET status=?,receipt=? WHERE id=?",
                        (status, canonical(receipt), key))

    def video(self, scope, content):
        bvid, raw_cid = content["bvid"], content["cid"]
        if not bvid or isinstance(raw_cid, bool) or raw_cid is None or not str(raw_cid).isdigit() or int(raw_cid) <= 0:
            raise ValueError("video identity missing")
        cid = str(raw_cid)
        self.db.execute("INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?)",
                        (scope, bvid, cid, time.time(), canonical(content)))

    def history(self, scope, limit=12):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT content FROM videos WHERE scope=? ORDER BY updated DESC LIMIT ?", (scope, limit))]

    def queue_memory(self, scope, body, key):
        self.db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?)",
                        (key, scope, body, "pending", time.time()))

    def close(self):
        self.db.close()
