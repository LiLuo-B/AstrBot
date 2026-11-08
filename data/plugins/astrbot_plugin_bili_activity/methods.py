"""Versioned research methods and evidence-backed attempts, never executable code.

A checked result demonstrates a fresh sourced work under another goal. It does
not prove causal effectiveness of the method or semantic truth of its contents.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .workshop import ID, Workshop, _json, _positive, _text


VERSION = 1
STEP_KINDS = {"gather", "verify", "compare", "write", "review"}
TERMINAL = {"success", "failed", "abandoned"}


class MethodLibrary:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.schema_backup = None
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='method_meta'").fetchone()
        if exists:
            row = self.db.execute("SELECT value FROM method_meta WHERE key='version'").fetchone()
            if not row or row[0] != str(VERSION):
                raise RuntimeError("unsupported research method database schema")
        else:
            if self.db.in_transaction:
                raise RuntimeError("initialize methods outside a transaction")
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name IN ('research_methods','method_revisions','method_attempts')").fetchone():
                raise RuntimeError("incomplete research method database schema")
            directory = Path(store.folder) / "schema_backups"
            directory.mkdir(exist_ok=True)
            self.schema_backup = directory / f"methods-v0-{time.time_ns()}-{uuid.uuid4().hex[:8]}.sqlite3"
            with closing(sqlite3.connect(self.schema_backup)) as backup:
                self.db.backup(backup)
            try:
                self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE method_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO method_meta VALUES('version','1');
                CREATE TABLE research_methods(
                  id TEXT PRIMARY KEY,scope TEXT NOT NULL,title TEXT NOT NULL,
                  status TEXT NOT NULL,revision INTEGER NOT NULL,create_key TEXT NOT NULL,
                  created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(scope,create_key));
                CREATE TABLE method_revisions(
                  method_id TEXT NOT NULL,scope TEXT NOT NULL,revision INTEGER NOT NULL,
                  work_id TEXT NOT NULL,work_revision INTEGER NOT NULL,work_hash TEXT NOT NULL,
                  source_goal_id TEXT NOT NULL,title TEXT NOT NULL,applies_to TEXT NOT NULL,
                  steps_json TEXT NOT NULL,limitations TEXT NOT NULL,request_key TEXT NOT NULL,
                  created_at REAL NOT NULL,PRIMARY KEY(method_id,revision),UNIQUE(method_id,request_key));
                CREATE TABLE method_attempts(
                  id TEXT PRIMARY KEY,scope TEXT NOT NULL,method_id TEXT NOT NULL,revision INTEGER NOT NULL,
                  goal_id TEXT NOT NULL,run_id TEXT NOT NULL,started_at REAL NOT NULL,
                  baseline_work_id TEXT,baseline_revision INTEGER NOT NULL,baseline_hash TEXT,
                  baseline_sources_json TEXT NOT NULL,status TEXT NOT NULL,result_work_id TEXT,
                  result_revision INTEGER,result_hash TEXT,summary TEXT NOT NULL,finished_at REAL,
                  UNIQUE(scope,goal_id));
                CREATE INDEX method_scope_updated ON research_methods(scope,updated_at);
                CREATE INDEX method_attempt_revision ON method_attempts(scope,method_id,revision,status);
                CREATE INDEX method_attempt_run ON method_attempts(scope,run_id,status);
                COMMIT;
                """)
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        self.workshop = Workshop(store)

    @staticmethod
    def _steps(steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 6:
            raise ValueError("methods need 1 to 6 declarative steps")
        result = []
        for step in steps:
            if (not isinstance(step, dict) or set(step) != {"kind", "instruction"}
                    or not isinstance(step["kind"], str) or step["kind"] not in STEP_KINDS):
                raise ValueError("steps allow only a research kind and instruction, not executable tool arguments")
            result.append({"kind": step["kind"], "instruction": _text(step["instruction"], "instruction", 600)})
        return result

    def _row(self, scope, method_id):
        if not isinstance(method_id, str) or not ID.fullmatch(method_id):
            return None
        return self.db.execute("SELECT * FROM research_methods WHERE scope=? AND id=?", (scope, method_id)).fetchone()

    def _revision(self, scope, method_id, revision=None):
        row = self._row(scope, method_id)
        if row is None:
            raise ValueError("method not found in this session")
        method = dict(row)
        revision = method["revision"] if revision is None else _positive(revision, "revision", maximum=20)
        row = self.db.execute("SELECT * FROM method_revisions WHERE scope=? AND method_id=? AND revision=?",
                              (scope, method_id, revision)).fetchone()
        if row is None:
            raise ValueError("method revision not found in this session")
        detail = dict(row)
        detail["revision_created_at"] = detail.pop("created_at")
        detail["steps"] = json.loads(detail.pop("steps_json"))
        return {**method, **detail, "head_revision": method["revision"]}

    def _source_work(self, scope, work_id):
        work = self.workshop.get(scope, work_id)
        if not work:
            raise ValueError("source work is unavailable in this session")
        goal = self.store.life.get_goal(scope, work["goal_id"])
        if not goal or goal["status"] != "completed":
            raise ValueError("a method requires a completed source goal")
        if not self.workshop.check(scope, work_id)["passed"]:
            raise ValueError("source work no longer passes fresh checks")
        return work

    def _source(self, method):
        source = self._source_work(method["scope"], method["work_id"])
        if (source["revision"] != method["work_revision"] or source["body_hash"] != method["work_hash"]
                or source["goal_id"] != method["source_goal_id"]):
            raise ValueError("method source revision changed; revise the method explicitly")
        return source

    def _values(self, scope, work_id, title, applies_to, steps, limitations):
        source = self._source_work(scope, work_id)
        return dict(work_id=work_id, work_revision=source["revision"], work_hash=source["body_hash"],
                    source_goal_id=source["goal_id"], title=_text(title, "title", 160),
                    applies_to=_text(applies_to, "applies_to", 1000), steps_json=_json(self._steps(steps)),
                    limitations=_text(limitations, "limitations", 1000))

    def create(self, scope, work_id, title, applies_to, steps, limitations, request_key, run_id=None):
        scope, request_key = _text(scope, "scope", 250), _text(request_key, "request_key", 250)
        with self.store.transaction():
            values = self._values(scope, work_id, title, applies_to, steps, limitations)
            old = self.db.execute("SELECT id FROM research_methods WHERE scope=? AND create_key=?", (scope, request_key)).fetchone()
            if old:
                initial = self.db.execute("SELECT * FROM method_revisions WHERE method_id=? AND revision=1", (old["id"],)).fetchone()
                if any(initial[key] != value for key, value in values.items()):
                    raise ValueError("create request key reused with different method")
                return self.get(scope, old["id"], 1)
            if self.db.execute("SELECT count(*) FROM research_methods WHERE scope=?", (scope,)).fetchone()[0] >= 100:
                raise ValueError("method limit reached")
            method_id, now = uuid.uuid4().hex, time.time()
            self.db.execute("INSERT INTO research_methods VALUES(?,?,?,?,?,?,?,?)",
                            (method_id, scope, values["title"], "active", 1, request_key, now, now))
            self._insert_revision(scope, method_id, 1, values, request_key, now)
        return self.get(scope, method_id)

    def _insert_revision(self, scope, method_id, revision, values, request_key, now):
        self.db.execute("""INSERT INTO method_revisions VALUES(
            :method_id,:scope,:revision,:work_id,:work_revision,:work_hash,:source_goal_id,
            :title,:applies_to,:steps_json,:limitations,:request_key,:created_at)""",
            dict(method_id=method_id, scope=scope, revision=revision, **values, request_key=request_key, created_at=now))

    def revise(self, scope, method_id, work_id, title, applies_to, steps, limitations,
               expected_revision, request_key, run_id=None):
        expected_revision = _positive(expected_revision, "expected_revision", maximum=20)
        request_key = _text(request_key, "request_key", 250)
        with self.store.transaction():
            method = self._revision(scope, method_id)
            if method["status"] != "active":
                raise ValueError("retired method cannot be revised")
            values = self._values(scope, work_id, title, applies_to, steps, limitations)
            old = self.db.execute("SELECT * FROM method_revisions WHERE scope=? AND method_id=? AND request_key=?",
                                  (scope, method_id, request_key)).fetchone()
            if old:
                if any(old[key] != value for key, value in values.items()):
                    raise ValueError("revision request key reused with different method")
                return self.get(scope, method_id, old["revision"])
            if expected_revision != method["revision"]:
                raise ValueError("method revision changed; read before revising")
            previous = self.db.execute("SELECT * FROM method_revisions WHERE method_id=? AND revision=?",
                                       (method_id, method["revision"])).fetchone()
            if all(previous[key] == value for key, value in values.items()):
                return self.get(scope, method_id)
            if method["revision"] >= 20:
                raise ValueError("method revision limit reached")
            revision, now = method["revision"] + 1, time.time()
            self._insert_revision(scope, method_id, revision, values, request_key, now)
            self.db.execute("UPDATE research_methods SET title=?,revision=?,updated_at=? WHERE scope=? AND id=?",
                            (values["title"], revision, now, scope, method_id))
        return self.get(scope, method_id)

    def list(self, scope, limit=8):
        limit = _positive(limit, "limit", maximum=50)
        result = []
        for row in self.db.execute("SELECT id FROM research_methods WHERE scope=? ORDER BY updated_at DESC,id LIMIT ?", (scope, limit)).fetchall():
            method = self.get(scope, row["id"])
            result.append({key: method[key] for key in ("id", "title", "status", "revision", "applies_to", "source_valid",
                           "source_error", "success_count", "recorded_success_count", "label", "usable", "updated_at")})
        return result

    def get(self, scope, method_id, revision=None):
        if self._row(scope, method_id) is None:
            return None
        method = self._revision(scope, method_id, revision)
        source, error = None, None
        try:
            source = self._source(method)
        except ValueError as exc:
            error = str(exc)
        attempts = self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND method_id=? AND revision=? AND status='success'",
                                   (scope, method_id, method["revision"])).fetchall()
        count = 0
        if source:
            for row in attempts:
                try:
                    self._success_work(method, self._attempt(row), source)
                    count += 1
                except ValueError:
                    pass
        return {**method, "source_ids": source["source_ids"] if source else [], "source_valid": source is not None,
                "source_error": error, "usable": source is not None and method["status"] == "active",
                "success_count": count, "recorded_success_count": len(attempts),
                "label": "reused_multiple" if count >= 2 else "reused_once" if count else "candidate",
                "meaning": "checked works under distinct goals, not proof of method effectiveness"}

    @staticmethod
    def _attempt(row):
        if row is None:
            return None
        result = dict(row)
        result["baseline_sources"] = json.loads(result.pop("baseline_sources_json"))
        return result

    def active(self, scope, goal_id):
        return self._attempt(self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND goal_id=? AND status IN ('active','paused')",
                                            (scope, goal_id)).fetchone())

    def get_attempt(self, scope, attempt_id):
        if not isinstance(attempt_id, str) or not ID.fullmatch(attempt_id):
            return None
        return self._attempt(self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND id=?", (scope, attempt_id)).fetchone())

    def _goal_lease(self, scope, goal_id, run_id):
        run_id = _text(run_id, "run_id", 250)
        goal = self.store.life.get_goal(scope, goal_id)
        if not goal or goal["status"] != "running" or goal["run_id"] != run_id:
            raise ValueError("method attempt requires this run's active goal")
        return goal

    def select(self, scope, method_id, goal_id, run_id):
        with self.store.transaction():
            self._goal_lease(scope, goal_id, run_id)
            old = self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND goal_id=?", (scope, goal_id)).fetchone()
            if old and old["method_id"] != method_id:
                raise ValueError("goal is already bound to another method attempt")
            if old and old["status"] in TERMINAL:
                raise ValueError("goal already has a finished method attempt")
            method = self._revision(scope, method_id, old["revision"] if old else None)
            if method["status"] != "active":
                raise ValueError("method has been retired")
            self._source(method)
            if goal_id == method["source_goal_id"]:
                raise ValueError("reuse needs a different goal from the source work")
            if old:
                if old["status"] == "active" and old["run_id"] != run_id:
                    raise ValueError("attempt belongs to another active run")
                self.db.execute("UPDATE method_attempts SET status='active',run_id=? WHERE scope=? AND id=?", (run_id, scope, old["id"]))
                return self.active(scope, goal_id)
            if self.db.execute("SELECT count(*) FROM method_attempts WHERE scope=?", (scope,)).fetchone()[0] >= 1000:
                raise ValueError("method attempt limit reached")
            baseline_row = self.db.execute("SELECT id FROM creative_works WHERE scope=? AND goal_id=?", (scope, goal_id)).fetchone()
            baseline = self.workshop.get(scope, baseline_row["id"]) if baseline_row else None
            attempt_id, now = uuid.uuid4().hex, time.time()
            self.db.execute("INSERT INTO method_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, scope, method_id, method["revision"], goal_id, run_id, now,
                 baseline["id"] if baseline else None, baseline["revision"] if baseline else 0,
                 baseline["body_hash"] if baseline else None, _json(baseline["source_ids"] if baseline else []),
                 "active", None, None, None, "", None))
        return self.active(scope, goal_id)

    def _source_fingerprint(self, scope, episode_id):
        row = self.db.execute("SELECT kind,evidence_json FROM episodes WHERE scope=? AND id=?", (scope, episode_id)).fetchone()
        if not row:
            return None
        proof = json.loads(row["evidence_json"])
        # Re-reading identical evidence with a new episode ID must not count as
        # a new research result merely because captured_at or run_id differs.
        keys = ("operation", "result_excerpt") if row["kind"] == "read" else ("bvid", "cid", "observations", "subtitles")
        return hashlib.sha256(_json({key: proof.get(key) for key in keys}).encode()).hexdigest()

    def _success_work(self, method, attempt, source):
        scope = method["scope"]
        goal = self.store.life.get_goal(scope, attempt["goal_id"])
        if not goal or goal["status"] != "completed" or goal["id"] == method["source_goal_id"]:
            raise ValueError("success needs a different completed goal")
        row = self.db.execute("SELECT id FROM creative_works WHERE scope=? AND goal_id=?", (scope, goal["id"])).fetchone()
        work = self.workshop.get(scope, row["id"]) if row else None
        if (not work or not work["revision"] or work["revision_created_at"] <= attempt["started_at"]
                or work["revision"] <= attempt["baseline_revision"]):
            raise ValueError("success needs a new work revision produced after method selection")
        if work["body_hash"] in {source["body_hash"], attempt["baseline_hash"]}:
            raise ValueError("success cannot reuse an unchanged source or baseline work")
        if attempt["status"] == "success" and (work["id"] != attempt["result_work_id"]
                or work["revision"] != attempt["result_revision"] or work["body_hash"] != attempt["result_hash"]):
            raise ValueError("recorded method result no longer matches the work head")
        if not self.workshop.check(scope, work["id"])["passed"]:
            raise ValueError("result work no longer passes fresh source checks")
        earlier = set(source["source_ids"]) | set(attempt["baseline_sources"])
        fingerprints = {self._source_fingerprint(scope, item) for item in earlier}
        new_sources = [item for item in work["source_ids"] if item not in earlier
                       and self._source_fingerprint(scope, item) not in fingerprints]
        if not new_sources:
            raise ValueError("success needs observed evidence distinct from the source and baseline")
        return work

    def result(self, scope, attempt_id, outcome, summary, run_id=None):
        if outcome not in TERMINAL:
            raise ValueError("method result must be success, failed or abandoned")
        summary = _text(summary, "summary", 1000)
        if not isinstance(attempt_id, str) or not ID.fullmatch(attempt_id):
            raise ValueError("attempt not found in this session")
        with self.store.transaction():
            attempt = self._attempt(self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND id=?", (scope, attempt_id)).fetchone())
            if not attempt:
                raise ValueError("attempt not found in this session")
            if run_id and attempt["run_id"] != run_id:
                raise ValueError("attempt belongs to another run")
            goal = self.store.life.get_goal(scope, attempt["goal_id"])
            if goal and goal["status"] == "running" and (not run_id or goal["run_id"] != run_id):
                raise ValueError("attempt goal belongs to another active run")
            if attempt["status"] in TERMINAL:
                if attempt["status"] != outcome:
                    raise ValueError("a finished attempt is immutable")
                return attempt
            work = None
            if outcome == "success":
                method = self._revision(scope, attempt["method_id"], attempt["revision"])
                if method["status"] != "active":
                    raise ValueError("retired method cannot gain new success evidence")
                work = self._success_work(method, attempt, self._source(method))
            self.db.execute("""UPDATE method_attempts SET status=?,result_work_id=?,result_revision=?,
                result_hash=?,summary=?,finished_at=? WHERE scope=? AND id=?""",
                (outcome, work["id"] if work else None, work["revision"] if work else None,
                 work["body_hash"] if work else None, summary, time.time(), scope, attempt_id))
            result = self._attempt(self.db.execute("SELECT * FROM method_attempts WHERE scope=? AND id=?", (scope, attempt_id)).fetchone())
        return result

    def retire(self, scope, method_id):
        with self.store.transaction():
            method = self._revision(scope, method_id)
            if method["status"] != "retired":
                self.db.execute("UPDATE research_methods SET status='retired',updated_at=? WHERE scope=? AND id=?", (time.time(), scope, method_id))
                self.db.execute("UPDATE method_attempts SET status='paused',summary='method retired' WHERE scope=? AND method_id=? AND status='active'", (scope, method_id))
        return self.get(scope, method_id)

    def pause_run(self, scope, run_id, reason):
        reason = _text(reason, "reason", 1000)
        self.db.execute("UPDATE method_attempts SET status='paused',summary=? WHERE scope=? AND run_id=? AND status='active'",
                        (reason, scope, run_id))

    def recover(self):
        """Called by plugin startup, never by readonly ops or this constructor."""
        cursor = self.db.execute("""UPDATE method_attempts SET status='paused',summary='interrupted run' WHERE status='active'
            AND NOT EXISTS(SELECT 1 FROM goals g WHERE g.scope=method_attempts.scope AND g.id=method_attempts.goal_id
                           AND g.status='running' AND g.run_id=method_attempts.run_id)""")
        return cursor.rowcount
