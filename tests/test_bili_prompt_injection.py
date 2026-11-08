"""Keep Bilibili history tool-driven instead of injecting raw logs every turn."""

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class HistoryInjectionTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the deployed request hook without starting AstrBot or services."""

    async def test_regular_chat_and_background_requests_do_not_load_history(self):
        """Retain safety and lookup guidance without scanning or appending episodes."""
        path = (
            Path(__file__).resolve().parents[1]
            / "data/plugins/astrbot_plugin_bili_activity/main.py"
        )
        tree = ast.parse(path.read_text())
        plugin = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "BiliActivity"
        )
        method = next(
            node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "guard_phone"
        )
        method.decorator_list = []
        module = types.ModuleType("bili_prompt_hook_test")
        module.AstrMessageEvent = object
        module.trim_tool_image_history = lambda *args, **kwargs: 0
        module.SelfExpression = Mock(
            side_effect=AssertionError("disabled expression must not run")
        )
        module.group_access = lambda event, cfg, owner=False: False
        module.MemoryPolicy = Mock(
            side_effect=AssertionError("no per-message episode scan")
        )
        with patch.dict(sys.modules, {module.__name__: module}):
            exec(
                compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"),
                vars(module),
            )
            for run_id in (None, "background-run"):
                with self.subTest(run_id=run_id):
                    cfg = {
                        "owner_scope": "qq:FriendMessage:123",
                        "comment_learning_enabled": False,
                        "life_enabled": False,
                        "owner_id": "123",
                    }
                    controller = types.SimpleNamespace(
                        cfg=lambda: cfg,
                        store=Mock(
                            side_effect=AssertionError("history store must not run")
                        ),
                        is_reply_run=lambda event: False,
                        shared_scope=lambda event: event.unified_msg_origin,
                    )
                    event = types.SimpleNamespace(
                        unified_msg_origin=cfg["owner_scope"],
                        get_extra=lambda key: run_id if key == "df_run_id" else None,
                    )
                    req = types.SimpleNamespace(
                        contexts=[], system_prompt="existing persona", func_tool=None
                    )
                    await module.guard_phone(controller, event, req)
                    self.assertTrue(req.system_prompt.startswith("existing persona"))
                    self.assertIn("df_bili_propose", req.system_prompt)
                    self.assertIn("df_bili_history", req.system_prompt)
                    self.assertIn("df_recall", req.system_prompt)
                    self.assertNotIn("今天（", req.system_prompt)
                    self.assertNotIn("近期自主活动记录", req.system_prompt)
                    self.assertNotIn("读过就是看过", req.system_prompt)
                    self.assertLess(len(req.system_prompt), 600)
            module.MemoryPolicy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
