from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _identity_decorator(*args, **kwargs):
    def decorate(function):
        return function

    return decorate


def _install_astrbot_stubs(data_dir: Path) -> None:
    # 纯运行时 smoke test：本机没有安装 AstrBot 包，因此用最小 API 桩验证插件边界。
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    event_filter_module = types.ModuleType("astrbot.api.event.filter")
    provider_module = types.ModuleType("astrbot.api.provider")
    star_module = types.ModuleType("astrbot.api.star")

    class DummyFilter:
        class EventMessageType:
            GROUP_MESSAGE = "group_message"

        event_message_type = staticmethod(_identity_decorator)
        on_astrbot_loaded = staticmethod(_identity_decorator)
        on_llm_request = staticmethod(_identity_decorator)
        permission_type = staticmethod(_identity_decorator)
        command = staticmethod(_identity_decorator)

    class DummyPermissionType:
        ADMIN = "admin"

    class DummyStar:
        def __init__(self, context):
            self.context = context

    class DummyStarTools:
        @staticmethod
        def get_data_dir(name=None):
            return str(data_dir / (name or "default"))

    def register(*args, **kwargs):
        return lambda cls: cls

    class DummyRequest:
        pass

    logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    api.AstrBotConfig = dict
    api.logger = logger
    event_module.AstrMessageEvent = object
    event_module.filter = DummyFilter
    event_filter_module.PermissionType = DummyPermissionType
    provider_module.ProviderRequest = DummyRequest
    star_module.Context = object
    star_module.Star = DummyStar
    star_module.StarTools = DummyStarTools
    star_module.register = register

    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_module,
            "astrbot.api.event.filter": event_filter_module,
            "astrbot.api.provider": provider_module,
            "astrbot.api.star": star_module,
        }
    )


class _Plain:
    def __init__(self, text):
        self.text = text


class _At:
    def __init__(self, qq):
        self.qq = qq


class _Reply:
    def __init__(self, sender_id):
        self.sender_id = sender_id


class _Event:
    def __init__(
        self,
        sender_id,
        nickname,
        text,
        *,
        group_id="100",
        message_id="",
        components=None,
        role="admin",
    ):
        self.message_str = text
        self.role = role
        self.message_obj = SimpleNamespace(
            sender=SimpleNamespace(user_id=sender_id, nickname=nickname),
            message=list(components or [_Plain(text)]),
            message_id=message_id,
            time=1_700_000_000,
        )
        self._sender_id = sender_id
        self._group_id = group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return "bot"

    def get_group_id(self):
        return self._group_id

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return text


class _Provider:
    def __init__(self):
        self.prompts = []

    async def text_chat(self, *, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "facts": [
                        {
                            "user_id": "12345678",
                            "field": "interests",
                            "action": "add",
                            "value": "原神",
                            "confidence": 0.95,
                            "source": "explicit",
                            "evidence_line_ids": [1],
                        }
                    ],
                    "aliases": [
                        {
                            "target_user_id": "12345678",
                            "alias": "明哥",
                            "called_by_user_id": "22334455",
                            "confidence": 0.9,
                            "evidence_line_ids": [2],
                        }
                    ],
                    "relations": [
                        {
                            "from_user_id": "22334455",
                            "to_user_id": "12345678",
                            "relation_type": "称呼",
                            "value": "经常称呼对方为明哥",
                            "confidence": 0.9,
                            "evidence_line_ids": [2],
                        }
                    ],
                    "observations": [],
                },
                ensure_ascii=False,
            )
        )


class _Context:
    def __init__(self, provider):
        self.provider = provider
        self.selected_ids = []

    def get_provider_by_id(self, provider_id):
        self.selected_ids.append(provider_id)
        return self.provider

    def get_using_provider(self):
        return self.provider


class MainRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        _install_astrbot_stubs(Path(cls.temp_dir.name))
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        cls.module = importlib.import_module("astrbot_plugin_glimmer_portraits.main")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        self.provider = _Provider()
        self.context = _Context(self.provider)
        self.config = {
            "group_whitelist": ["*"],
            "summary_provider_id": "configured-small-model",
            "auto_update_enabled": False,
            "daily_summary_enabled": False,
            "enabled_fields": ["兴趣爱好", "称呼与别名", "群内关系"],
            "min_confidence": 0.5,
            "allow_whole_group_reset": True,
        }
        self.plugin = self.module.GlimmerPortraitsPlugin(self.context, self.config)

    def tearDown(self):
        asyncio.run(self.plugin.terminate())

    def test_selected_provider_receives_mixed_group_context_and_old_profile(self):
        first = _Event(
            "12345678",
            "小明",
            "最近在玩原神",
            message_id="m1",
            components=[_Plain("最近在玩原神"), _At("22334455")],
        )
        second = _Event(
            "22334455",
            "小红",
            "明哥又带你打本？",
            message_id="m2",
            components=[_Plain("明哥又带你打本？"), _Reply("12345678")],
        )
        asyncio.run(self.plugin.capture_group_message(first))
        asyncio.run(self.plugin.capture_group_message(second))
        succeeded = asyncio.run(
            self.plugin._run_update("aiocqhttp:100", reason="test")
        )
        self.assertTrue(succeeded)
        self.assertEqual(self.context.selected_ids, ["configured-small-model"])
        self.assertEqual(len(self.provider.prompts), 1)
        prompt = self.provider.prompts[0]
        self.assertIn("L1 小明(uid=12345678)", prompt)
        self.assertIn("L2 小红(uid=22334455)", prompt)
        self.assertIn("按某个用户单独截取", prompt)

        profile = self.plugin.store.get_profile("aiocqhttp:100", "12345678")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertTrue(any(item["value"] == "原神" for item in profile["facts"]))
        self.assertTrue(any(item["alias"] == "明哥" for item in profile["aliases"]))

    def test_injection_uses_temporary_context_and_command_preview(self):
        event = _Event("12345678", "小明", "最近在玩原神", message_id="m1")
        asyncio.run(self.plugin.capture_group_message(event))
        asyncio.run(self.plugin._run_update("aiocqhttp:100", reason="test"))

        request = SimpleNamespace(extra_user_content_parts=[])
        asyncio.run(self.plugin.on_llm_request(event, request))
        self.assertEqual(len(request.extra_user_content_parts), 1)
        injected = request.extra_user_content_parts[0]
        self.assertIn("原神", injected.text)
        self.assertTrue(getattr(injected, "is_temp", False))

        async def collect(generator):
            return [item async for item in generator]

        output = asyncio.run(collect(self.plugin.command_group_profile(event, "12345678")))
        self.assertEqual(len(output), 1)
        self.assertIn("原神", output[0])

    def test_reset_command_requires_confirmation_and_is_group_scoped(self):
        event = _Event("12345678", "小明", "最近在玩原神", message_id="m-reset")
        asyncio.run(self.plugin.capture_group_message(event))
        asyncio.run(self.plugin._run_update("aiocqhttp:100", reason="test"))
        self.assertIsNotNone(
            self.plugin.store.get_profile("aiocqhttp:100", "12345678")
        )

        async def collect(generator):
            return [item async for item in generator]

        preview_event = _Event(
            "99999999", "管理员", "/重置画像 12345678", message_id="reset-preview"
        )
        preview = asyncio.run(
            collect(self.plugin.command_reset_profile(preview_event))
        )
        self.assertIn("如确认", preview[0])
        token_match = re.search(r"/重置画像 确认 12345678 ([A-F0-9]+)", preview[0])
        self.assertIsNotNone(token_match)
        assert token_match is not None

        confirm_event = _Event(
            "99999999",
            "管理员",
            f"/重置画像 确认 12345678 {token_match.group(1)}",
            message_id="reset-confirm",
        )
        result = asyncio.run(
            collect(self.plugin.command_reset_profile(confirm_event))
        )
        self.assertIn("已清除用户 12345678", result[0])
        self.assertIsNone(
            self.plugin.store.get_profile("aiocqhttp:100", "12345678")
        )


if __name__ == "__main__":
    unittest.main()
