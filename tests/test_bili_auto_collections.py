"""Regression tests for explicitly authorized automatic Bilibili collections."""

import importlib
import sys
import types
import unittest
from pathlib import Path

PACKAGE_NAME = "bili_collection_policy_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [
    str(
        Path(__file__).resolve().parents[1]
        / "data/plugins/astrbot_plugin_bili_activity"
    )
]
sys.modules[PACKAGE_NAME] = package
policy = importlib.import_module(f"{PACKAGE_NAME}.interactions")

COLLECTION_ACTIONS = policy.WRITES - policy.PUBLIC - policy.ENGAGEMENT


class AutoCollectionPolicyTests(unittest.TestCase):
    """Exercise the real policy without opening state or contacting Bilibili."""

    def setUp(self):
        """Build an explicitly enabled, non-public automatic configuration."""
        self.cfg = policy.DEFAULTS | {
            "collection_management": True,
            "engagement_actions": True,
            "public_interactions": False,
            "interaction_mode": "auto_low_risk",
            "auto_collection_management": True,
        }

    def test_collection_actions_require_separate_explicit_opt_in(self):
        """Preserve legacy approval behavior until the new flag is enabled."""
        for action in COLLECTION_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(policy.auto_allowed(action, {}, self.cfg))
                self.assertFalse(
                    policy.auto_allowed(
                        action, {}, self.cfg | {"auto_collection_management": False}
                    )
                )
                legacy = self.cfg.copy()
                legacy.pop("auto_collection_management")
                self.assertFalse(policy.auto_allowed(action, {}, legacy))

    def test_approval_switch_and_zero_quotas_prevent_automatic_actions(self):
        """Retain the action switch, approval mode, and both global quotas."""
        for action in COLLECTION_ACTIONS:
            for key, value in (
                ("collection_management", False),
                ("interaction_mode", "approval"),
                ("max_actions_per_run", 0),
                ("max_actions_per_day", 0),
            ):
                with self.subTest(action=action, key=key):
                    self.assertFalse(
                        policy.auto_allowed(action, {}, self.cfg | {key: value})
                    )

    def test_public_interactions_and_unknown_actions_remain_blocked(self):
        """Do not grant permission for public or unrecognized operations."""
        for action in policy.PUBLIC | {"delete_video", "unknown"}:
            with self.subTest(action=action):
                self.assertFalse(policy.auto_allowed(action, {}, self.cfg))
        for action in policy.ENGAGEMENT:
            self.assertTrue(policy.auto_allowed(action, {}, self.cfg))

    def test_allowlists_apply_to_video_owners_and_follow_uids(self):
        """Apply the UID restriction to both video and follow targets."""
        cfg = self.cfg | {"allowed_uids": [5]}
        for action, target in (
            ("follow", {"uid": 5}),
            ("unfollow", {"uid": "5"}),
            ("favorite_add", {"owner": {"mid": 5}}),
        ):
            with self.subTest(action=action):
                self.assertTrue(policy.auto_allowed(action, target, cfg))
                self.assertFalse(policy.auto_allowed(action, {"uid": 6}, cfg))
                self.assertFalse(policy.auto_allowed(action, {}, cfg))
        cfg |= {"allowed_keywords": ["tutorial"]}
        self.assertTrue(
            policy.auto_allowed(
                "favorite_add", {"owner": {"mid": 5}, "title": "Tutorial"}, cfg
            )
        )
        self.assertFalse(policy.auto_allowed("follow", {"uid": 5}, cfg))

    def test_new_flag_must_be_boolean_and_old_configs_still_validate(self):
        """Reject ambiguous flag values without breaking legacy configs."""
        policy.validate_config(self.cfg)
        legacy = self.cfg.copy()
        legacy.pop("auto_collection_management")
        policy.validate_config(legacy)
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.validate_config(self.cfg | {"auto_collection_management": value})


if __name__ == "__main__":
    unittest.main()
