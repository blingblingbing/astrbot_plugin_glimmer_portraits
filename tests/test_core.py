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

    def test_summary_prompt_requires_compact_values(self) -> None:
        prompt = build_summary_prompt(self.messages, {}, ["interests"])
        self.assertIn("每个 value 只写一条短结论或短语", prompt)
        self.assertIn("硬上限 40 个字符", prompt)
        self.assertIn("不要复述证据", prompt)

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
        self.assertEqual(result["aliases"], [])
        self.assertEqual(len(result["alias_candidates"]), 1)
        self.assertEqual(
            result["alias_candidates"][0]["attribution_strength"], "anchored"
        )
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

    def test_communication_style_requires_repeated_evidence_and_replace(self) -> None:
        messages = number_messages(
            [
                {
                    "id": 1,
                    "sender_id": "12345678",
                    "sender_name": "小明",
                    "text": "先简单说一句",
                },
                {
                    "id": 2,
                    "sender_id": "22334455",
                    "sender_name": "小红",
                    "text": "我来回应",
                },
                {
                    "id": 3,
                    "sender_id": "12345678",
                    "sender_name": "小明",
                    "text": "再接一个玩笑",
                },
            ]
        )
        raw = """
        {
          "observations": [
            {"user_id":"12345678","field":"communication_style","action":"replace","value":"在本群表现为回复简洁，善于承接玩笑","confidence":0.85,"evidence_line_ids":[1,3]},
            {"user_id":"22334455","field":"communication_style","action":"replace","value":"在本群表现为直接回应问题","confidence":0.9,"evidence_line_ids":[2]}
          ]
        }
        """
        result = parse_model_result(
            raw, messages, ["communication_style"], min_confidence=0.5
        )
        self.assertEqual(len(result["observations"]), 1)
        self.assertEqual(result["observations"][0]["action"], "replace")
        self.assertEqual(result["observations"][0]["user_id"], "12345678")

    def test_relations_reject_alias_type_and_keep_one_per_pair_type(self) -> None:
        raw = """
        {
          "relations": [
            {"from_user_id":"22334455","to_user_id":"12345678","relation_type":"称呼","action":"replace","value":"称呼对方为明哥","confidence":0.95,"evidence_line_ids":[2]},
            {"from_user_id":"22334455","to_user_id":"12345678","relation_type":"游戏搭档","action":"replace","value":"偶尔一起游戏","confidence":0.8,"evidence_line_ids":[1,2]},
            {"from_user_id":"22334455","to_user_id":"12345678","relation_type":"游戏搭档","action":"replace","value":"经常一起游戏并互相协作","confidence":0.9,"evidence_line_ids":[1,2]}
          ]
        }
        """
        result = parse_model_result(
            raw, self.messages, ["relations"], min_confidence=0.5
        )
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(result["relations"][0]["relation_type"], "游戏搭档")
        self.assertEqual(result["relations"][0]["value"], "经常一起游戏并互相协作")

    def test_unanchored_alias_without_response_stays_weak_candidate(self) -> None:
        messages = number_messages(
            [
                {"id": 1, "sender_id": "12345678", "sender_name": "目标", "text": "先前发言"},
                {
                    "id": 2,
                    "sender_id": "22334455",
                    "sender_name": "称呼者",
                    "text": "星师傅下班狠狠被折磨",
                },
            ]
        )
        raw = """
        {"aliases":[{
          "target_user_id":"12345678","alias":"星师傅",
          "called_by_user_id":"22334455","attribution_type":"ambiguous",
          "confidence":0.9,"call_line_ids":[2],"target_response_line_ids":[]
        }]}
        """
        result = parse_model_result(raw, messages, ["aliases"], min_confidence=0.5)
        self.assertEqual(result["aliases"], [])
        self.assertEqual(len(result["alias_candidates"]), 1)
        self.assertEqual(
            result["alias_candidates"][0]["attribution_strength"], "weak"
        )

    def test_contextual_alias_uses_nearby_target_response(self) -> None:
        messages = number_messages(
            [
                {
                    "id": 1,
                    "sender_id": "22334455",
                    "sender_name": "称呼者",
                    "text": "小饼来塔",
                },
                {"id": 2, "sender_id": "12345678", "sender_name": "目标", "text": "来了"},
            ]
        )
        raw = """
        {"aliases":[
          {
            "target_user_id":"12345678","alias":"小饼",
            "called_by_user_id":"22334455","attribution_type":"context_response",
            "confidence":0.85,"call_line_ids":[1],"target_response_line_ids":[2]
          },
          {
            "target_user_id":"12345678","alias":"小饼",
            "called_by_user_id":"22334455","attribution_type":"context_response",
            "confidence":0.8,"call_line_ids":[1],"target_response_line_ids":[2]
          }
        ]}
        """
        result = parse_model_result(raw, messages, ["aliases"], min_confidence=0.5)
        self.assertEqual(result["aliases"], [])
        self.assertEqual(len(result["alias_candidates"]), 1)
        self.assertEqual(
            result["alias_candidates"][0]["attribution_strength"], "context"
        )

    def test_at_does_not_prove_alias_belongs_to_mentioned_user(self) -> None:
        messages = number_messages(
            [
                {
                    "id": 1,
                    "sender_id": "22334455",
                    "sender_name": "称呼者",
                    "text": "星老师太吵了 @示例用户",
                    "mentioned_user_ids": ("12345678",),
                }
            ]
        )
        raw = """
        {"aliases":[{
          "target_user_id":"12345678","alias":"星老师",
          "called_by_user_id":"22334455","attribution_type":"explicit_at",
          "confidence":0.9,"call_line_ids":[1]
        }]}
        """
        result = parse_model_result(raw, messages, ["aliases"], min_confidence=0.5)
        self.assertEqual(result["aliases"], [])
        self.assertEqual(len(result["alias_candidates"]), 1)
        self.assertEqual(
            result["alias_candidates"][0]["attribution_strength"], "anchored"
        )

    def test_explicit_naming_of_mentioned_user_is_strong(self) -> None:
        messages = number_messages(
            [
                {
                    "id": 1,
                    "sender_id": "22334455",
                    "sender_name": "称呼者",
                    "text": "以后叫你小饼",
                    "mentioned_user_ids": ("12345678",),
                }
            ]
        )
        raw = """
        {"aliases":[{
          "target_user_id":"12345678","alias":"小饼",
          "called_by_user_id":"22334455","attribution_type":"explicit_at",
          "confidence":0.9,"call_line_ids":[1]
        }]}
        """
        result = parse_model_result(raw, messages, ["aliases"], min_confidence=0.5)
        self.assertEqual(len(result["aliases"]), 1)
        self.assertEqual(result["aliases"][0]["attribution_strength"], "strong")

    def test_existing_unique_alias_can_be_reinforced_without_explicit_anchor(self) -> None:
        messages = number_messages(
            [
                {"id": 1, "sender_id": "12345678", "sender_name": "目标", "text": "先前发言"},
                {"id": 2, "sender_id": "22334455", "sender_name": "称呼者", "text": "小饼来吗"},
            ]
        )
        raw = """
        {"aliases":[{
          "target_user_id":"12345678","alias":"小饼",
          "called_by_user_id":"22334455","confidence":0.85,"call_line_ids":[2]
        }]}
        """
        result = parse_model_result(
            raw,
            messages,
            ["aliases"],
            min_confidence=0.5,
            existing_alias_targets={"小饼": {"12345678"}},
        )
        self.assertEqual(len(result["aliases"]), 1)
        self.assertEqual(result["aliases"][0]["attribution_strength"], "existing")


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

    def test_injection_renderer_compacts_long_values_and_preserves_wrapper(self) -> None:
        text = render_profile(
            {
                "user_id": "3110322077",
                "aliases": [
                    {"alias": "217姐姐", "source": "observed", "status": "active"},
                    {"alias": "老登", "source": "observed", "status": "active"},
                ],
                "facts": [
                    {
                        "field": "recent_context",
                        "value": "近期学业与实验安排紧凑，过去一周每日睡眠不足7小时，自述曾在西藏期间均摊至不到6小时，态度乐观表示‘死不了’。",
                        "confidence": 0.95,
                        "status": "active",
                        "last_seen": 1700000000,
                    },
                    {
                        "field": "occupation_or_study",
                        "value": "本科就读于物理相关专业，目前处于论文开题及写作准备阶段。",
                        "confidence": 0.9,
                        "status": "active",
                        "last_seen": 1700000000,
                    },
                    {
                        "field": "coarse_location",
                        "value": "生活/活动于北京地区（自述经历晚高峰通勤）",
                        "confidence": 0.85,
                        "status": "active",
                        "last_seen": 1700000000,
                    },
                ],
                "observations": [
                    {
                        "field": "communication_style",
                        "value": "在本群表现为交流直率且信息密度较高，日常以简短玩闹或反问接话。",
                        "confidence": 0.9,
                        "status": "active",
                        "last_seen": 1700000000,
                    }
                ],
                "relations": [],
            },
            max_chars=500,
            injection=True,
        )
        self.assertLessEqual(len(text), 500)
        self.assertTrue(text.startswith("<group_profile>"))
        self.assertTrue(text.endswith("</group_profile>"))
        self.assertIn("睡眠不足", text)
        self.assertIn("沟通：", text)
        self.assertNotIn("高置信度", text)
        self.assertTrue(all(len(line) <= 72 for line in text.splitlines()))

    def test_communication_style_replaces_previous_active_style(self) -> None:
        values = ("在本群表现为回复简洁", "在本群表现为回复简洁且善于接梗")
        for index, value in enumerate(values, 1):
            result = {
                "facts": [],
                "aliases": [],
                "relations": [],
                "observations": [
                    {
                        "user_id": "12345678",
                        "field": "communication_style",
                        "action": "replace",
                        "value": value,
                        "confidence": 0.8 + index / 100,
                        "evidence": "测试证据",
                    }
                ],
            }
            self.store.apply_result_and_consume("qq:100", [], result)

        rows = self.store.connection.execute(
            "SELECT value, status FROM observations WHERE scope=? AND user_id=? ORDER BY id",
            ("qq:100", "12345678"),
        ).fetchall()
        self.assertEqual(sum(row["status"] == "active" for row in rows), 1)
        self.assertEqual(
            next(row["value"] for row in rows if row["status"] == "active"),
            "在本群表现为回复简洁且善于接梗",
        )

    def test_existing_duplicate_communication_styles_collapse_on_reopen(self) -> None:
        database_path = Path(self.temp_dir.name) / "profiles.sqlite3"
        self.store.connection.execute("DROP INDEX idx_one_active_communication_style")
        self.store.connection.executemany(
            """
            INSERT INTO observations
            (scope, user_id, field, value, canonical_value, confidence, last_seen, status)
            VALUES ('qq:100', '12345678', 'communication_style', ?, ?, ?, ?, 'active')
            """,
            [
                ("旧的低置信风格", "旧的低置信风格", 0.7, 20),
                ("应保留的高置信风格", "应保留的高置信风格", 0.9, 10),
            ],
        )
        self.store.connection.commit()
        self.store.close()
        self.store = ProfileStore(database_path)

        rows = self.store.connection.execute(
            "SELECT value, status FROM observations ORDER BY id"
        ).fetchall()
        self.assertEqual(sum(row["status"] == "active" for row in rows), 1)
        self.assertEqual(
            next(row["value"] for row in rows if row["status"] == "active"),
            "应保留的高置信风格",
        )

    def test_same_alias_merges_callers_but_distinct_aliases_remain(self) -> None:
        self.store.add_message(
            scope="qq:100",
            platform="qq",
            group_id="100",
            message_key="alias-test",
            sender_id="12345678",
            sender_name="当前昵称",
            text="测试消息",
        )
        result = {
            "facts": [],
            "relations": [],
            "observations": [],
            "aliases": [
                {
                    "target_user_id": "12345678",
                    "alias": "明哥",
                    "called_by_user_id": "22334455",
                    "confidence": 0.8,
                    "evidence": "证据一",
                },
                {
                    "target_user_id": "12345678",
                    "alias": "明哥",
                    "called_by_user_id": "33445566",
                    "confidence": 0.9,
                    "evidence": "证据二",
                },
                {
                    "target_user_id": "12345678",
                    "alias": "小明",
                    "called_by_user_id": "22334455",
                    "confidence": 0.85,
                    "evidence": "证据三",
                },
            ],
        }
        self.store.apply_result_and_consume("qq:100", [], result)

        profile = self.store.get_profile("qq:100", "12345678")
        self.assertIsNotNone(profile)
        assert profile is not None
        ming_profile = next(item for item in profile["aliases"] if item["alias"] == "明哥")
        self.assertEqual(set(ming_profile["called_by_user_ids"]), {"22334455", "33445566"})
        alias_rows = self.store.connection.execute(
            "SELECT alias, occurrences FROM aliases WHERE status='active' ORDER BY alias"
        ).fetchall()
        self.assertEqual(
            {row["alias"] for row in alias_rows}, {"当前昵称", "小明", "明哥"}
        )
        ming = next(row for row in alias_rows if row["alias"] == "明哥")
        self.assertEqual(ming["occurrences"], 2)
        callers = self.store.connection.execute(
            """
            SELECT called_by_user_id FROM alias_callers
            WHERE user_id='12345678' AND canonical_alias='明哥'
            ORDER BY called_by_user_id
            """
        ).fetchall()
        self.assertEqual([row[0] for row in callers], ["22334455", "33445566"])

    def test_old_alias_rows_migrate_to_one_alias_with_multiple_callers(self) -> None:
        database_path = Path(self.temp_dir.name) / "profiles.sqlite3"
        self.store.connection.execute("DROP INDEX idx_one_active_alias")
        self.store.connection.execute(
            "DELETE FROM schema_meta WHERE key='alias_callers_v1'"
        )
        self.store.connection.executemany(
            """
            INSERT INTO aliases
            (scope,user_id,alias,canonical_alias,source,called_by_user_id,confidence,
             occurrences,first_seen,last_seen,status,evidence)
            VALUES ('qq:100','12345678','明哥','明哥','observed',?,?,?,10,?,'active','证据')
            """,
            [
                ("22334455", 0.8, 2, 20),
                ("33445566", 0.9, 3, 30),
            ],
        )
        self.store.connection.commit()
        self.store.close()
        self.store = ProfileStore(database_path)

        aliases = self.store.connection.execute(
            "SELECT occurrences,status FROM aliases WHERE canonical_alias='明哥' ORDER BY id"
        ).fetchall()
        self.assertEqual(sum(row["status"] == "active" for row in aliases), 1)
        self.assertEqual(
            next(row["occurrences"] for row in aliases if row["status"] == "active"),
            5,
        )
        callers = self.store.connection.execute(
            "SELECT called_by_user_id,occurrences FROM alias_callers ORDER BY called_by_user_id"
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in callers], [("22334455", 2), ("33445566", 3)])

    def test_relation_replaces_same_pair_and_type(self) -> None:
        for value, confidence in (("偶尔一起游戏", 0.8), ("经常一起游戏", 0.9)):
            self.store.apply_result_and_consume(
                "qq:100",
                [],
                {
                    "facts": [],
                    "aliases": [],
                    "observations": [],
                    "relations": [
                        {
                            "from_user_id": "12345678",
                            "to_user_id": "22334455",
                            "relation_type": "游戏搭档",
                            "action": "replace",
                            "value": value,
                            "confidence": confidence,
                            "evidence": "测试证据",
                        }
                    ],
                },
            )
        rows = self.store.connection.execute(
            "SELECT value,status FROM relations ORDER BY id"
        ).fetchall()
        self.assertEqual(sum(row["status"] == "active" for row in rows), 1)
        self.assertEqual(
            next(row["value"] for row in rows if row["status"] == "active"),
            "经常一起游戏",
        )

    def test_context_alias_promotes_after_two_confirmations(self) -> None:
        candidate = {
            "target_user_id": "12345678",
            "alias": "小饼",
            "called_by_user_id": "22334455",
            "attribution_strength": "context",
            "confidence": 0.85,
            "evidence": "称呼后目标回应",
        }
        result = {
            "facts": [],
            "aliases": [],
            "alias_candidates": [candidate],
            "relations": [],
            "observations": [],
        }
        first = self.store.apply_result_and_consume("qq:100", [], result)
        self.assertEqual(first["aliases"], 0)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM aliases WHERE status='active'"
            ).fetchone()[0],
            0,
        )
        second = self.store.apply_result_and_consume("qq:100", [], result)
        self.assertEqual(second["aliases"], 1)
        alias = self.store.connection.execute(
            "SELECT alias FROM aliases WHERE status='active'"
        ).fetchone()
        self.assertEqual(alias["alias"], "小饼")
        status = self.store.connection.execute(
            "SELECT status FROM alias_candidates"
        ).fetchone()[0]
        self.assertEqual(status, "promoted")

    def test_competing_context_alias_targets_remain_conflicted(self) -> None:
        for target, caller in (("12345678", "22334455"), ("33445566", "44556677")):
            result = {
                "facts": [],
                "aliases": [],
                "alias_candidates": [
                    {
                        "target_user_id": target,
                        "alias": "星师傅",
                        "called_by_user_id": caller,
                        "attribution_strength": "context",
                        "confidence": 0.85,
                        "evidence": "上下文回应",
                    }
                ],
                "relations": [],
                "observations": [],
            }
            self.store.apply_result_and_consume("qq:100", [], result)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM aliases WHERE status='active'"
            ).fetchone()[0],
            0,
        )
        statuses = self.store.connection.execute(
            "SELECT DISTINCT status FROM alias_candidates"
        ).fetchall()
        self.assertEqual({row[0] for row in statuses}, {"conflict"})

    def test_anchored_candidate_cannot_override_existing_alias_target(self) -> None:
        now = int(time.time())
        self.store._upsert_alias_cursor(
            self.store.connection.cursor(),
            scope="qq:100",
            user_id="12345678",
            alias="星老师",
            source="observed",
            called_by_user_id="22334455",
            confidence=0.9,
            evidence="已有正确归属",
            now=now,
        )
        self.store.connection.commit()
        candidate = {
            "target_user_id": "33445566",
            "alias": "星老师",
            "called_by_user_id": "44556677",
            "attribution_strength": "anchored",
            "confidence": 0.9,
            "evidence": "昵称和 @ 同句但没有直接命名",
        }
        result = {
            "facts": [],
            "aliases": [],
            "alias_candidates": [candidate],
            "relations": [],
            "observations": [],
        }
        self.store.apply_result_and_consume("qq:100", [], result)
        self.store.apply_result_and_consume("qq:100", [], result)
        active_targets = self.store.connection.execute(
            "SELECT user_id FROM aliases WHERE canonical_alias='星老师' AND status='active'"
        ).fetchall()
        self.assertEqual([row["user_id"] for row in active_targets], ["12345678"])
        status = self.store.connection.execute(
            "SELECT status FROM alias_candidates WHERE user_id='33445566'"
        ).fetchone()[0]
        self.assertEqual(status, "conflict")


if __name__ == "__main__":
    unittest.main()
