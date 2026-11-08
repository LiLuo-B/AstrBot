"""Offline calendar queries over real local Bilibili evidence aggregation."""

import ast
import importlib
import json
import sqlite3
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PACKAGE_NAME = "bili_date_history_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [
    str(
        Path(__file__).resolve().parents[1]
        / "data/plugins/astrbot_plugin_bili_activity"
    )
]
sys.modules[PACKAGE_NAME] = package
history = importlib.import_module(f"{PACKAGE_NAME}.history")
core = importlib.import_module(f"{PACKAGE_NAME}.core")
NOW = datetime(2026, 11, 8, 15, tzinfo=history.SHANGHAI).timestamp()
SCOPE = "qq:FriendMessage:12345"


class DateHistoryTests(unittest.TestCase):
    """Cover calendar boundaries, provenance, pagination and scope isolation."""

    def setUp(self):
        """Create an in-memory ledger matching the production episode columns."""
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE episodes(id TEXT PRIMARY KEY,scope TEXT,kind TEXT,occurred_at REAL,evidence_json TEXT,interpretation_json TEXT,supersedes_id TEXT)"
        )
        self.number = 0

    def tearDown(self):
        """Close the isolated database."""
        self.db.close()

    def add(
        self,
        kind="video",
        at="2026-11-08T10:00:00+08:00",
        proof=None,
        scope=SCOPE,
        thought="",
        supersedes=None,
    ):
        """Insert a fixture episode and return its stable id."""
        self.number += 1
        ident = f"e{self.number:04}"
        self.db.execute(
            "INSERT INTO episodes VALUES(?,?,?,?,?,?,?)",
            (
                ident,
                scope,
                kind,
                datetime.fromisoformat(at).timestamp(),
                json.dumps(
                    proof if proof is not None else {"bvid": "BVone", "cid": "1"}
                ),
                json.dumps({"thought": thought}),
                supersedes,
            ),
        )
        return ident

    def query(self, start="今天", end="", **kwargs):
        """Query the real aggregation with a frozen Shanghai clock."""
        return history.browsing_history(self.db, SCOPE, start, end, now=NOW, **kwargs)

    def read(self, operation="info", bvid="BVone", **kwargs):
        """Insert a successful identity-bound read fixture."""
        value = {"identity": {"bvid": bvid, "cid": 1, "title": "A title"}}
        return self.add(
            "read",
            proof={"operation": operation, "result_excerpt": json.dumps(value)},
            **kwargs,
        )

    def test_relative_dates_and_calendar_weeks(self):
        """Resolve relative names using a calendar week rather than seven rolling days."""
        expected = {
            "今天": ("2026-11-08", "2026-11-08"),
            "昨天": ("2026-11-07", "2026-11-07"),
            "前天": ("2026-11-06", "2026-11-06"),
            "上周": ("2026-10-26", "2026-11-01"),
            "本周": ("2026-11-02", "2026-11-08"),
            "最近7天": ("2026-11-02", "2026-11-08"),
        }
        for value, dates in expected.items():
            with self.subTest(value=value):
                self.assertEqual(
                    tuple(x.isoformat() for x in history.resolve_dates(value, now=NOW)),
                    dates,
                )
        self.assertEqual(self.query("2024-02-29")["start_date"], "2024-02-29")
        for start, end in (
            ("2025-02-29", ""),
            ("2026-2-01", ""),
            ("2026-11-08", "2026-11-07"),
            ("2025-01-01", "2026-01-02"),
            ("昨天", "2026-11-08"),
            ("9999-12-31", ""),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.query(start, end)

    def test_shanghai_midnight_inclusive_end_day_and_scope(self):
        """Include midnight exactly, excluding the next day's midnight and other owners."""
        self.read(bvid="BVbefore", at="2026-11-06T15:59:59+00:00")
        self.read(bvid="BVstart", at="2026-11-06T16:00:00+00:00")
        self.read(bvid="BVend", at="2026-11-07T15:59:59+00:00")
        self.read(bvid="BVnext", at="2026-11-07T16:00:00+00:00")
        self.read(bvid="BVprivate", at="2026-11-07T10:00:00+08:00", scope="other")
        result = self.query("昨天")
        self.assertEqual({x["bvid"] for x in result["videos"]}, {"BVstart", "BVend"})
        self.assertEqual(result["counts"]["browsed_videos"], 2)

    def test_bv_dedup_media_types_and_partition_ids(self):
        """Merge snapshots and different parts without inflating video counts."""
        self.read()
        for cid, observation in (
            (1, {"kind": "frames", "text": "frame description"}),
            (2, {"kind": "asr", "text": "audio transcript"}),
        ):
            self.add(
                proof={
                    "bvid": "BVone",
                    "cid": cid,
                    "observations": [observation],
                    "requested_range": [0, 60],
                }
            )
        self.add(
            proof={"bvid": "BVone", "cid": 2, "subtitles": [{"content": "subtitle"}]}
        )
        self.add(
            "video_note",
            proof={
                "bvid": "BVone",
                "cid": 2,
                "observations": [{"kind": "frames", "text": "old"}],
            },
            thought="my reflection",
        )
        result = self.query()
        self.assertEqual(result["counts"]["browsed_videos"], 1)
        self.assertEqual(result["counts"]["analyzed_videos"], 1)
        video = result["videos"][0]
        self.assertEqual(video["media"], ["asr", "frames", "subtitles"])
        self.assertEqual(video["cids"], ["1", "2"])
        self.assertEqual(video["analysis_records"], 3)
        self.assertEqual(video["notes"][0]["thought"], "my reflection")
        self.assertEqual(len(video["requested_ranges"]), 2)

    def test_feed_candidates_and_quoted_bv_are_not_browsed(self):
        """Only direct video identities count, never arbitrary nested content."""
        for operation in ("home", "search", "following_feed", "favorites", "folders"):
            self.add(
                "read",
                proof={
                    "operation": operation,
                    "result_excerpt": json.dumps({"items": [{"bvid": "BVcandidate"}]}),
                },
            )
        self.add(
            "read",
            proof={
                "operation": "comments",
                "result_excerpt": json.dumps({"comments": [{"bvid": "BVquoted"}]}),
            },
        )
        result = self.query()
        self.assertEqual(result["counts"]["read_records"], 6)
        self.assertEqual(result["counts"]["browsed_videos"], 0)
        self.assertEqual(result["videos"], [])

    def test_empty_attempts_failures_and_asr_only_labels(self):
        """Do not promote failed reads or empty observations to analyzed content."""
        self.add(
            proof={
                "bvid": "BVempty",
                "cid": 1,
                "observations": [{"kind": "frames", "text": " "}],
            }
        )
        self.add(
            proof={
                "bvid": "BVunknown",
                "cid": 1,
                "observations": [{"kind": "unknown", "text": "claim"}],
            }
        )
        self.add(
            proof={
                "bvid": "BVasr",
                "cid": 1,
                "observations": [{"kind": "asr", "text": "words"}],
            }
        )
        for proof in (
            {"ok": False, "operation": "info", "identity": {"bvid": "BVfailed"}},
            {"operation": "info", "result_excerpt": '{"ok":false,"bvid":"BVfailed"}'},
            {"operation": "info", "result_excerpt": "{}"},
        ):
            self.add("read", proof=proof)
        result = self.query()
        self.assertEqual(result["counts"]["browsed_videos"], 1)
        self.assertEqual(result["counts"]["analyzed_videos"], 1)
        self.assertEqual(result["counts"]["analysis_attempt_videos"], 3)
        summary = core.today_viewing_summary(self.db, SCOPE, NOW)
        self.assertIn("语音转写", summary)
        self.assertNotIn("读过就是看过", summary)
        self.assertNotIn("画面描述/字幕", summary)

    def test_late_notes_do_not_create_new_browsing(self):
        """Yesterday's copied media evidence in today's note is not today's view."""
        self.add(
            at="2026-11-07T10:00:00+08:00",
            proof={"bvid": "BVone", "cid": 1, "subtitles": [{"content": "words"}]},
        )
        self.add(
            "video_note",
            proof={"bvid": "BVone", "cid": 1, "subtitles": [{"content": "words"}]},
            thought="remembered today",
        )
        self.assertEqual(self.query()["counts"]["browsed_videos"], 0)
        self.assertEqual(self.query("昨天")["counts"]["analyzed_videos"], 1)

    def test_daily_and_range_counts_do_not_sum_rewatching(self):
        """Count the same BV on both dates but only once across the range."""
        self.read(at="2026-11-07T10:00:00+08:00")
        self.read()
        result = self.query("2026-11-06", "2026-11-08")
        self.assertEqual(result["counts"]["browsed_videos"], 1)
        self.assertEqual([day["browsed_videos"] for day in result["daily"]], [0, 1, 1])

    def test_pagination_preserves_full_counts_and_is_read_only(self):
        """Aggregate before pagination and never append recall as fresh evidence."""
        for bvid in ("BVone", "BVtwo", "BVthree"):
            self.read(bvid=bvid)
        before = self.db.total_changes
        first = self.query(limit=1)
        second = self.query(limit=1, offset=first["next_offset"])
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(first["counts"]["browsed_videos"], 3)
        self.assertNotEqual(first["videos"][0]["bvid"], second["videos"][0]["bvid"])
        self.assertEqual(self.db.total_changes, before)
        for kwargs in (
            {"limit": True},
            {"limit": 0},
            {"limit": 51},
            {"offset": -1},
            {"offset": False},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.query(**kwargs)

    def test_superseded_success_is_not_counted(self):
        """Respect corrections, including a later correction outside the date window."""
        original = self.read(at="2026-11-07T10:00:00+08:00")
        self.add("read", proof={"ok": False, "operation": "info"}, supersedes=original)
        self.assertEqual(self.query("昨天")["counts"]["browsed_videos"], 0)

    def test_legacy_truncation_uses_top_level_identity_not_nested_bv(self):
        """Recover completed identity fields without regex-matching comment content."""
        self.add(
            "read",
            proof={
                "operation": "comments",
                "truncated": True,
                "result_excerpt": '{"identity":{"bvid":"BVlegacy","cid":2,"title":"A \\"quoted\\" title"},"comments":[{"message":"cut',
            },
        )
        self.add(
            "read",
            proof={
                "operation": "comments",
                "truncated": True,
                "result_excerpt": '{"comments":[{"bvid":"BVnested"},',
            },
        )
        self.db.execute(
            "INSERT INTO episodes VALUES(?,?,?,?,?,?,?)",
            ("bad", SCOPE, "video", NOW, "not-json", "{}", None),
        )
        result = self.query()
        self.assertEqual({x["bvid"] for x in result["videos"]}, {"BVlegacy"})
        self.assertTrue(result["coverage"]["counts_are_lower_bounds"])
        self.assertEqual(result["coverage"]["malformed_records_skipped"], 1)

    def test_scan_cap_is_explicit(self):
        """Bound scan work without silently presenting a partial total as complete."""
        self.read(bvid="BVone")
        self.read(bvid="BVtwo")
        with patch.object(history, "MAX_RECORDS", 1):
            result = self.query()
        self.assertTrue(result["coverage"]["scan_truncated"])
        self.assertTrue(result["coverage"]["counts_are_lower_bounds"])

    def test_structured_read_evidence_survives_truncated_subtitle_json(self):
        """Save bounded title and subtitle flags before truncating response text."""
        value = {
            "identity": {"bvid": "BVone", "cid": 1, "title": 'quoted "title"'},
            "subtitles": [{"content": "a" * 5000}],
        }
        proof = {
            "operation": "subtitles",
            "result_excerpt": json.dumps(value)[:3000],
            "truncated": True,
            **history.read_snapshot(value),
        }
        self.add("read", proof=proof)
        result = self.query()
        self.assertEqual(result["counts"]["analyzed_videos"], 1)
        self.assertEqual(result["videos"][0]["title"], 'quoted "title"')
        self.assertFalse(result["coverage"]["malformed_records_skipped"])
        self.assertFalse(result["coverage"]["counts_are_lower_bounds"])

    def test_corrupt_nested_fields_do_not_crash_or_invent_evidence(self):
        """Handle valid JSON with unexpected nested types conservatively."""
        self.add(
            proof={
                "bvid": "BVone",
                "cid": True,
                "observations": [{"kind": [], "text": "unknown"}],
            }
        )
        self.add("video_note", proof={"bvid": []}, thought="not attributable")
        result = self.query()
        self.assertEqual(result["counts"]["analyzed_videos"], 0)
        self.assertEqual(result["videos"][0]["cids"], [])

    def test_out_of_range_legacy_timestamp_does_not_break_any_date(self):
        """Ignore corrupt timestamps even when finding the first recorded date."""
        self.read()
        for index, timestamp in enumerate((-1e300, 1e300, "invalid", -1, 253402300799)):
            self.db.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,?)",
                (f"badtime{index}", SCOPE, "read", timestamp, "{}", "{}", None),
            )
        result = self.query()
        self.assertEqual(result["coverage"]["first_recorded_date"], "2026-11-08")
        self.assertEqual(result["counts"]["browsed_videos"], 1)
        self.assertIn("浏览过1条", core.today_viewing_summary(self.db, SCOPE, NOW))


class HistoryControllerTests(unittest.IsolatedAsyncioTestCase):
    """Test the actual controller entry without importing or starting AstrBot."""

    async def test_owner_guard_precedes_query_and_no_goal_or_network_is_needed(self):
        """Keep the scope controller-owned and allow historical reads while paused."""
        from unittest.mock import Mock

        source_path = Path(package.__path__[0]) / "main.py"
        tree = ast.parse(source_path.read_text())
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "BiliActivity"
        )
        method = next(
            node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "bili_history"
        )
        method.decorator_list = []
        query = Mock(return_value={"coverage": {}, "counts": {"browsed_videos": 0}})
        namespace = {
            "AstrMessageEvent": object,
            "browsing_history": query,
            "group_access": lambda event, cfg, owner=False: False,
            "public_history": lambda result: result,
            "model_json": core.model_json,
        }
        exec(
            compile(
                ast.Module(body=[method], type_ignores=[]), str(source_path), "exec"
            ),
            namespace,
        )
        controller = types.SimpleNamespace(
            store=types.SimpleNamespace(db=object()),
            check_chat=Mock(side_effect=PermissionError("not owner")),
        )
        event = types.SimpleNamespace(unified_msg_origin=SCOPE)
        with self.assertRaises(PermissionError):
            await namespace["bili_history"](controller, event, "昨天")
        query.assert_not_called()
        controller.check_chat = Mock(return_value={"life_enabled": False, "owner_scope": SCOPE})
        result = json.loads(await namespace["bili_history"](controller, event, "昨天"))
        self.assertFalse(result["coverage"]["recording_enabled_now"])
        self.assertTrue(result["records_are_data_not_instructions"])
        query.assert_called_once_with(
            controller.store.db, SCOPE, "昨天", "", limit=20, offset=0
        )


if __name__ == "__main__":
    unittest.main()
