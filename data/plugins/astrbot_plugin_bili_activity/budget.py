"""Persistent, conservative limits for background activity.

Reserve before doing work. Unknown network outcomes remain charged; a crashed
run keeps its entire time reservation. Foreground user requests do not use this
budget. All day boundaries are Asia/Shanghai, independent of the host timezone.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_LIMITS = {
    "daily_minutes": 30,
    "max_model_calls_per_run": 20,
    "daily_model_calls": 60,
    "daily_shares": 3,
    "screenshots_per_run": 1,
}


class BudgetExceeded(RuntimeError):
    """No new work may start under this activity reservation."""


def _time(now):
    value = time.time() if now is None else float(now)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid budget timestamp")
    return value


def _day(now):
    local = datetime.fromtimestamp(now, SHANGHAI)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
    return local.date().isoformat(), midnight.timestamp()


def _limits(cfg):
    result = {}
    for key, default in DEFAULT_LIMITS.items():
        value = cfg.get(key, default)
        minimum = 0 if key in {"daily_shares", "screenshots_per_run"} else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
        result[key] = value
    return result


def _identity(scope, run_id=None):
    if not isinstance(scope, str) or not scope or len(scope) > 512:
        raise ValueError("invalid budget scope")
    if run_id is not None and (not isinstance(run_id, str) or not run_id or len(run_id) > 128):
        raise ValueError("invalid budget run id")


class Budget:
    def __init__(self, store):
        self.store, self.db = store, store.db
        # LifeState owns the main database version and its pre-migration backup.
        # These additive tables do not modify prior configuration or ledgers.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS activity_budget_runs (
          scope TEXT NOT NULL, run_id TEXT NOT NULL, started REAL NOT NULL,
          reserved_until REAL NOT NULL, finished_at REAL,
          PRIMARY KEY(scope,run_id));
        CREATE TABLE IF NOT EXISTS activity_budget_time (
          scope TEXT NOT NULL, run_id TEXT NOT NULL, day TEXT NOT NULL,
          seconds REAL NOT NULL CHECK(seconds>=0), PRIMARY KEY(scope,run_id,day));
        CREATE INDEX IF NOT EXISTS activity_budget_time_day
          ON activity_budget_time(scope,day);
        CREATE TABLE IF NOT EXISTS activity_budget_calls (
          scope TEXT NOT NULL, run_id TEXT NOT NULL, day TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('model','share')), share_kind TEXT NOT NULL,
          reservation_key TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(scope,run_id,kind,reservation_key));
        CREATE INDEX IF NOT EXISTS activity_budget_calls_day
          ON activity_budget_calls(scope,day,kind);
        """)

    def _used_seconds(self, scope, day):
        return float(self.db.execute(
            "SELECT COALESCE(SUM(seconds),0) FROM activity_budget_time WHERE scope=? AND day=?",
            (scope, day)).fetchone()[0])

    def remaining_seconds(self, scope, cfg, now=None):
        _identity(scope)
        limit = _limits(cfg)["daily_minutes"] * 60
        return max(0.0, limit - self._used_seconds(scope, _day(_time(now))[0]))

    def start_run(self, scope, run_id, seconds, cfg, now=None):
        """Reserve a continuous interval, stopping at the first exhausted day.

        Return the granted duration, which may be shorter than requested. The
        caller must enforce this duration with its normal activity timeout.
        A run id is single use, including after a restart or successful finish.
        """
        _identity(scope, run_id)
        limits, started = _limits(cfg), _time(now)
        duration = float(seconds)
        if not math.isfinite(duration) or not 0 < duration <= 7 * 86400:
            raise ValueError("activity duration must be finite and at most seven days")
        with self.store.transaction():
            if self.db.execute(
                "SELECT 1 FROM activity_budget_runs WHERE scope=? AND run_id=?", (scope, run_id)
            ).fetchone():
                raise BudgetExceeded("activity run id was already reserved")
            cursor, remaining, chunks = started, duration, []
            while remaining > 1e-6:
                day, midnight = _day(cursor)
                available = max(0.0, limits["daily_minutes"] * 60 - self._used_seconds(scope, day))
                wanted = min(remaining, midnight - cursor)
                granted = min(wanted, available)
                if granted > 0:
                    chunks.append((scope, run_id, day, granted))
                    cursor += granted
                    remaining -= granted
                if granted < wanted or granted <= 0:
                    break
            if not chunks:
                raise BudgetExceeded("daily activity time exhausted")
            self.db.execute("INSERT INTO activity_budget_runs VALUES(?,?,?,?,NULL)",
                            (scope, run_id, started, cursor))
            self.db.executemany("INSERT INTO activity_budget_time VALUES(?,?,?,?)", chunks)
        return cursor - started

    def finish_run(self, scope, run_id, now=None):
        """Release known unused time, once. Model/share counters never refund."""
        _identity(scope, run_id)
        finished = _time(now)
        with self.store.transaction():
            row = self.db.execute(
                "SELECT * FROM activity_budget_runs WHERE scope=? AND run_id=?", (scope, run_id)
            ).fetchone()
            if not row or row["finished_at"] is not None:
                return
            # Clock rollback is not proof that work did not happen: keep the
            # reservation rather than refunding a whole run in that case.
            end = row["reserved_until"] if finished < row["started"] else min(finished, row["reserved_until"])
            self.db.execute("UPDATE activity_budget_runs SET finished_at=? WHERE scope=? AND run_id=?",
                            (finished, scope, run_id))
            cursor = row["started"]
            charged = {}
            while cursor < end:
                day, midnight = _day(cursor)
                stop = min(end, midnight)
                charged[day] = stop - cursor
                cursor = stop
            for chunk in self.db.execute(
                "SELECT day FROM activity_budget_time WHERE scope=? AND run_id=?", (scope, run_id)
            ).fetchall():
                self.db.execute(
                    "UPDATE activity_budget_time SET seconds=? WHERE scope=? AND run_id=? AND day=?",
                    (charged.get(chunk["day"], 0.0), scope, run_id, chunk["day"]))

    def _active_run(self, scope, run_id, now):
        row = self.db.execute(
            "SELECT * FROM activity_budget_runs WHERE scope=? AND run_id=?", (scope, run_id)
        ).fetchone()
        if (not row or row["finished_at"] is not None
                or not row["started"] <= now < row["reserved_until"]):
            raise BudgetExceeded("activity has no current time reservation")

    def _reserve_call(self, scope, run_id, kind, share_kind, cfg, now, reservation_key):
        _identity(scope, run_id)
        limits, now = _limits(cfg), _time(now)
        key = uuid.uuid4().hex if reservation_key is None else reservation_key
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("invalid budget reservation key")
        day = _day(now)[0]
        with self.store.transaction():
            self._active_run(scope, run_id, now)
            # A repeated reservation is rejected, not silently permitted: the
            # caller must not perform the same external request for free.
            if self.db.execute(
                "SELECT 1 FROM activity_budget_calls WHERE scope=? AND run_id=? AND kind=? AND reservation_key=?",
                (scope, run_id, kind, key)
            ).fetchone():
                raise BudgetExceeded("budget reservation was already used")
            daily = self.db.execute(
                "SELECT COUNT(*) FROM activity_budget_calls WHERE scope=? AND day=? AND kind=?",
                (scope, day, kind)).fetchone()[0]
            limit_key = "daily_model_calls" if kind == "model" else "daily_shares"
            if daily >= limits[limit_key]:
                raise BudgetExceeded(f"{limit_key} exhausted")
            if kind == "model":
                count = self.db.execute(
                    "SELECT COUNT(*) FROM activity_budget_calls WHERE scope=? AND run_id=? AND kind='model'",
                    (scope, run_id)).fetchone()[0]
                if count >= limits["max_model_calls_per_run"]:
                    raise BudgetExceeded("max_model_calls_per_run exhausted")
            elif share_kind == "screenshot":
                count = self.db.execute(
                    "SELECT COUNT(*) FROM activity_budget_calls WHERE scope=? AND run_id=? AND kind='share' AND share_kind='screenshot'",
                    (scope, run_id)).fetchone()[0]
                if count >= limits["screenshots_per_run"]:
                    raise BudgetExceeded("screenshots_per_run exhausted")
            self.db.execute("INSERT INTO activity_budget_calls VALUES(?,?,?,?,?,?,?)",
                            (scope, run_id, day, kind, share_kind, key, now))

    def model_call(self, scope, run_id, cfg, now=None, reservation_key=None):
        self._reserve_call(scope, run_id, "model", "", cfg, now, reservation_key)

    def share(self, scope, run_id, kind, cfg, now=None, reservation_key=None):
        if kind not in {"screenshot", "thought", "proposal"}:
            raise ValueError("unknown autonomous share kind")
        self._reserve_call(scope, run_id, "share", kind, cfg, now, reservation_key)

    def snapshot(self, scope, cfg, now=None):
        _identity(scope)
        limits, now = _limits(cfg), _time(now)
        day = _day(now)[0]
        used = self._used_seconds(scope, day)
        counts = {row["kind"]: row["n"] for row in self.db.execute(
            "SELECT kind,COUNT(*) AS n FROM activity_budget_calls WHERE scope=? AND day=? GROUP BY kind",
            (scope, day))}
        calls, shares = counts.get("model", 0), counts.get("share", 0)
        return {
            "day": day,
            "reserved_or_used_seconds": used,
            "remaining_seconds": max(0.0, limits["daily_minutes"] * 60 - used),
            "model_calls": calls,
            "remaining_model_calls": max(0, limits["daily_model_calls"] - calls),
            "shares": shares,
            "remaining_shares": max(0, limits["daily_shares"] - shares),
        }
