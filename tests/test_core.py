from __future__ import annotations

import tempfile
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_glimmer_portraits.profile_engine import (
    build_summary_prompt,
    is_valid_model_result,
    normalize_enabled_fields,
    number_messages,
    parse_model_result,
    render_profile,
)
from astrbot_plugin_glimmer_portraits.profile_store import ProfileStore


class ProfileEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = number_messages(
            [
                {
                    "id": 1,
                    "sender_id": "12345678",
                    "sender_name": "小明",
                    "text": "最近在玩原神",
                    "mentioned_user_ids": ("22334455",),
                    "created_at": 1700000000,
                },
                {
                    "id": 2,
                    "sender_id": "22334455",
                    "sender_name": "小红",
                    "text": "明哥又带你打本？",
                    "reply_user_id": "12345678",
                    "created_at": 1700000060,
                },
            ]
        )

    def test_sensitive_fields_are_disabled_by_default(self) -> None:
        fields = normalize_enabled_fields(["兴趣爱好", "性别", "健康状况", "沟通风格"])
        self.assertEqual(fields, ["interests", "communication_style"])

    def test_prompt_keeps_mixed_group_conversation_and_old_state(self) -> None:
        prompt = build_summary_prompt(
            self.messages,
            {
                "12345678": {
                    "display_name": "小明",
                    "aliases": [],
                    "facts": [
                        {
                            "field": "interests",
                            "value": "摄影",
                            "confidence": 0.8,
                            "status": "active",
                        }
                    ],
                    "observations": [],
                    "relations": [],
                }
            },
            ["interests", "aliases", "relations"],
        )
        self.assertIn("L1 小明(uid=12345678)", prompt)
        self.assertIn("L2 小红(uid=22334455)", prompt)
        self.assertIn("摄影", prompt)
        self.assertIn("不要按某个用户单独截取", prompt)

    def test_parser_rejects_unknown_user_and_unknown_evidence(self) -> None:
        raw = """
        {
          "facts": [
            {"user_id":"12345678","field":"interests","action":"add","value":"原神","confidence":0.9,"source":"explicit","evidence_line_ids":[1]},
            {"user_id":"99999999","field":"interests","action":"add","value":"不存在","confidence":0.9,"source":"explicit","evidence_line_ids":[1]},
            {"user_id":"12345678","field":"interests","action":"add","value":"伪造","confidence":0.9,"source":"explicit","evidence_line_ids":[99]}
          ],
          "aliases": [
            {"target_user_id":"12345678","alias":"明哥","called_by_user_id":"22334455","confidence":0.9,"evidence_line_ids":[2]}
          ]
        }
        """
        result = parse_model_result(
            raw, self.messages, ["interests", "aliases"], min_confidence=0.5
        )
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["facts"][0]["value"], "原神")
        self.assertEqual(len(result["aliases"]), 1)
        self.assertTrue(is_valid_model_result(raw))

    def test_parser_requires_evidence_and_confidence(self) -> None:
        raw = {
            "facts": [
                {
                    "user_id": "12345678",
                    "field": "interests",
                    "action": "add",
                    "value": "可能喜欢",
                    "confidence": 0.2,
                    "evidence_line_ids": [1],
                },
                {
                    "user_id": "12345678",
                    "field": "interests",
                    "action": "add",
                    "value": "没有证据",
                    "confidence": 0.9,
                    "evidence_line_ids": [],
                },
            ]
        }
        result = parse_model_result(
            str(raw).replace("'", '"'), self.messages, ["interests"]
        )
        self.assertEqual(result["facts"], [])

    def test_replace_with_multiple_values_keeps_all_new_values(self) -> None:
        raw = """
        {
          "facts": [
            {"user_id":"12345678","field":"software_preferences","action":"replace","value":["Linux","Neovim"],"confidence":0.9,"source":"explicit","evidence_line_ids":[1]}
          ]
        }
        """
        result = parse_model_result(
            raw, self.messages, ["software_preferences"], min_confidence=0.5
        )
        self.assertEqual(
            [item["action"] for item in result["facts"]], ["replace", "add"]
        )


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.temp_dir.name) / "profiles.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_pending_messages_old_profile_and_atomic_consume(self) -> None:
        inserted, pending = self.store.add_message(
            scope="qq:100",
            platform="qq",
            group_id="100",
            message_key="m1",
            sender_id="12345678",
            sender_name="小明",
            text="最近在玩原神",
            created_at=1700000000,
        )
        self.assertTrue(inserted)
        self.assertEqual(pending, 1)
        self.assertFalse(
            self.store.add_message(
                scope="qq:100",
                platform="qq",
                group_id="100",
                message_key="m1",
                sender_id="12345678",
                sender_name="小明",
                text="最近在玩原神",
                created_at=1700000000,
            )[0]
        )
        messages = self.store.get_pending("qq:100", 10)
        result = parse_model_result(
            '{"facts":[{"user_id":"12345678","field":"interests","action":"add","value":"原神","confidence":0.9,"source":"explicit","evidence_line_ids":[1]}]}',
            number_messages(messages),
            ["interests"],
            min_confidence=0.5,
        )
        counts = self.store.apply_result_and_consume(
            "qq:100",
            [messages[0]["id"]],
            result,
            max_facts_per_user=12,
            max_aliases_per_user=8,
            max_observations_per_user=5,
            max_relations_per_user=12,
        )
        self.assertEqual(counts["facts"], 1)
        self.assertEqual(self.store.pending_count("qq:100"), 0)
        profile = self.store.get_profile("qq:100", "12345678")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile["facts"][0]["value"], "原神")
        self.assertIn("小明", [item["alias"] for item in profile["aliases"]])

    def test_overview_and_user_reset_are_group_scoped(self) -> None:
        now = int(time.time())
        for scope, group_id in (("qq:100", "100"), ("qq:200", "200")):
            self.store.add_message(
                scope=scope,
                platform="qq",
                group_id=group_id,
                message_key=scope,
                sender_id="12345678",
                sender_name="小明",
                text="聊天内容",
                created_at=now,
            )
        result = {
            "facts": [
                {
                    "user_id": "12345678",
                    "field": "interests",
                    "action": "add",
                    "value": "摄影",
                    "confidence": 0.9,
                    "source": "explicit",
                    "evidence": "L1 小明：聊天内容",
                }
            ],
            "aliases": [],
            "relations": [],
            "observations": [],
        }
        pending = self.store.get_pending("qq:100", 10)
        self.store.apply_result_and_consume("qq:100", [pending[0]["id"]], result)
        self.assertEqual(len(self.store.get_overview("qq:100")), 1)
        counts = self.store.reset_user("qq:100", "12345678", "admin")
        self.assertGreaterEqual(counts["affected_rows"], 1)
        self.assertIsNone(self.store.get_profile("qq:100", "12345678"))
        self.assertIsNotNone(self.store.get_profile("qq:200", "12345678"))
        self.assertEqual(self.store.state("qq:100").get("scope"), "qq:100")

    def test_profile_renderer_is_bounded_and_contains_relationship(self) -> None:
        text = render_profile(
            {
                "user_id": "12345678",
                "display_name": "小明",
                "last_updated": 1700000000,
                "aliases": [
                    {"alias": "明哥", "source": "observed", "status": "active"}
                ],
                "facts": [
                    {
                        "field": "interests",
                        "value": "摄影",
                        "confidence": 0.9,
                        "status": "active",
                        "last_seen": 1700000000,
                    }
                ],
                "observations": [],
                "relations": [
                    {
                        "from_user_id": "12345678",
                        "to_user_id": "22334455",
                        "relation_type": "游戏搭档",
                        "value": "经常一起讨论副本",
                        "confidence": 0.8,
                        "status": "active",
                        "last_seen": 1700000000,
                    }
                ],
            },
            max_chars=500,
            injection=True,
        )
        self.assertLessEqual(len(text), 500)
        self.assertIn("摄影", text)
        self.assertIn("明哥", text)


if __name__ == "__main__":
    unittest.main()
