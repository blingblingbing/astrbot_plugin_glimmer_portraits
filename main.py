"""浮光群像：轻量群聊用户画像插件。

设计边界：

* 仅按 ``平台 + 群号 + 用户 ID`` 保存画像，不做私聊和跨群画像；
* 采集的是按时间排列的多人群聊片段，不把消息按用户拆开后再总结；
* 画像总结使用 AstrBot 已配置的 Provider，可与正常聊天模型分开；
* 只把当前用户、@对象和回复目标的紧凑画像注入本轮请求；
* EverOS、LivingMemory、ReadingSteiner Custom 和 Daily Analysis 都不是运行依赖。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - only used by a minimal test environment
    TextPart = None  # type: ignore[assignment]

from .profile_engine import (
    DEFAULT_ENABLED_FIELDS,
    build_summary_prompt,
    clean_text,
    is_valid_model_result,
    normalize_enabled_fields,
    normalize_user_id,
    number_messages,
    parse_model_result,
    render_profile,
)
from .profile_store import ProfileStore


PLUGIN_NAME = "astrbot_plugin_glimmer_portraits"
COMMAND_NAMES = ("群画像", "更新画像", "重置画像")
QQ_ID_RE = re.compile(r"^[0-9]{5,20}$")
MEDIA_PLACEHOLDER_RE = re.compile(
    r"\[(?:图片(?:内容)?|图像|image|动画表情|表情包?|贴纸|sticker|face|视频|video|语音|voice|音频|audio|文件|file)(?:\s*[:：,，][^\]]*)?\]",
    re.IGNORECASE,
)
CQ_MEDIA_RE = re.compile(r"\[CQ:(?:image|face|video|record|file),[^\]]*\]", re.IGNORECASE)


class _FallbackTextPart:
    """让纯业务测试和旧版 AstrBot 都能安全构造临时文本块。"""

    type = "text"

    def __init__(self, text: str):
        self.text = text
        self.is_temp = False

    def mark_as_temp(self) -> "_FallbackTextPart":
        self.is_temp = True
        return self

    def model_dump_for_context(self) -> dict[str, Any]:
        payload = {"type": "text", "text": self.text}
        if self.is_temp:
            payload["_no_save"] = True
        return payload


def _make_temp_text_part(text: str) -> Any:
    if TextPart is None:
        return _FallbackTextPart(text).mark_as_temp()
    part = TextPart(text=text)
    marker = getattr(part, "mark_as_temp", None)
    return marker() if callable(marker) else part


def _config_value(config: Any, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes", "on", "是", "开启"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def _as_int(value: Any, default: int, minimum: int = 0, maximum: int = 2_000_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _as_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _split_config_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = re.split(r"[,，;；\s]+", str(value or ""))
    return [str(item).strip() for item in raw if str(item).strip()]


def _normalize_scope_part(value: Any, max_chars: int = 128) -> str:
    return clean_text(value, max_chars)


def _event_method(event: Any, name: str, default: Any = "") -> Any:
    method = getattr(event, name, None)
    if callable(method):
        try:
            return method()
        except Exception:
            return default
    return default


def _object_value(obj: Any, name: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _components(event: Any) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    components = _object_value(message_obj, "message", [])
    return list(components) if isinstance(components, (list, tuple)) else []


def _sender(event: Any) -> Any:
    message_obj = getattr(event, "message_obj", None)
    return _object_value(message_obj, "sender", None)


def _sender_name(event: Any) -> str:
    sender = _sender(event)
    for name in ("card", "nickname", "name", "user_name"):
        value = clean_text(_object_value(sender, name, ""), 80)
        if value:
            return value
    return clean_text(_event_method(event, "get_sender_name", ""), 80)


def _event_text(event: Any, components: Iterable[Any]) -> str:
    plain_parts = [
        clean_text(_object_value(component, "text", ""), 500)
        for component in components
        if component.__class__.__name__.casefold() == "plain"
        and clean_text(_object_value(component, "text", ""), 500)
    ]
    raw = " ".join(plain_parts)
    if not raw:
        raw = _event_method(event, "get_message_str", "")
    if not raw:
        raw = getattr(event, "message_str", "") or ""
    raw = clean_text(raw, 500)
    raw = CQ_MEDIA_RE.sub(" ", raw)
    raw = MEDIA_PLACEHOLDER_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _mentioned_and_reply(components: Iterable[Any]) -> tuple[tuple[str, ...], str]:
    mentioned: list[str] = []
    reply_user_id = ""
    for component in components:
        kind = component.__class__.__name__.casefold()
        if kind == "at":
            value = (
                _object_value(component, "qq", "")
                or _object_value(component, "target", "")
                or _object_value(component, "user_id", "")
            )
            user_id = normalize_user_id(value)
            if user_id and user_id not in mentioned:
                mentioned.append(user_id)
        elif kind == "reply":
            value = _object_value(component, "sender_id", "")
            sender = _object_value(component, "sender", None)
            value = value or _object_value(sender, "user_id", "")
            reply_user_id = normalize_user_id(value) or reply_user_id
    return tuple(mentioned), reply_user_id


def _created_at(event: Any) -> int:
    message_obj = getattr(event, "message_obj", None)
    value = _object_value(message_obj, "time", 0) or _object_value(event, "timestamp", 0)
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        timestamp = 0
    return timestamp if timestamp > 0 else int(time.time())


def _message_id(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    value = _object_value(message_obj, "message_id", "") or _object_value(event, "message_id", "")
    return clean_text(value, 160)


def _make_message_key(
    *,
    scope: str,
    sender_id: str,
    text: str,
    message_id: str,
    created_at: int,
    mentioned_user_ids: Iterable[str],
    reply_user_id: str,
) -> str:
    if message_id:
        return f"id:{message_id}"
    raw = "|".join(
        (
            scope,
            sender_id,
            text,
            str(created_at),
            ",".join(mentioned_user_ids),
            reply_user_id,
        )
    )
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@register(
    PLUGIN_NAME,
    "Codex",
    "浮光群像：从群聊中增量勾勒群友的称呼、兴趣、特点与关系",
    "0.1.0",
)
class GlimmerPortraitsPlugin(Star):
    """按群维护轻量画像，并在 LLM 请求前注入相关画像。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.store = ProfileStore(data_dir / "group_profiles.sqlite3")
        self._update_tasks: dict[str, asyncio.Task[Any]] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._reset_confirmations: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._terminating = False

    # ───────────────────────────── 范围与配置 ─────────────────────────────

    def _scope(self, event: Any) -> tuple[str, str, str] | None:
        group_id = _normalize_scope_part(_event_method(event, "get_group_id", ""))
        if not group_id:
            return None
        platform = _normalize_scope_part(
            _event_method(event, "get_platform_name", "")
            or _event_method(event, "get_platform_id", "")
            or "unknown",
            60,
        )
        scope = f"{platform}:{group_id}"
        return platform, group_id, scope

    def _group_allowed(self, group_id: str) -> bool:
        whitelist = set(_split_config_values(_config_value(self.config, "group_whitelist", ["*"])))
        blacklist = set(_split_config_values(_config_value(self.config, "group_blacklist", [])))
        if group_id in blacklist:
            return False
        return not whitelist or "*" in whitelist or group_id in whitelist

    def _enabled_fields(self) -> list[str]:
        raw = _config_value(self.config, "enabled_fields", DEFAULT_ENABLED_FIELDS)
        return normalize_enabled_fields(
            raw,
            allow_sensitive=_as_bool(_config_value(self.config, "allow_sensitive_fields", False)),
        )

    def _is_command_message(self, text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped.startswith("/"):
            return False
        return True

    def _admin_id(self, event: Any) -> str:
        return clean_text(_event_method(event, "get_sender_id", "") or "unknown", 128)

    def _command_tail(self, event: Any, command: str, fallback: str = "") -> str:
        raw = clean_text(getattr(event, "message_str", "") or "", 600)
        if raw:
            for token in (f"/{command}", command):
                if raw == token:
                    return ""
                if raw.startswith(token) and (len(raw) == len(token) or raw[len(token)].isspace()):
                    return raw[len(token) :].strip()
        return clean_text(fallback, 600)

    # ───────────────────────────── 消息采集 ─────────────────────────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def capture_group_message(self, event: AstrMessageEvent):
        """保存一条按群排序的待总结消息；不会唤醒 LLM。"""

        if self._terminating or not _as_bool(_config_value(self.config, "enabled", True), True):
            return
        scoped = self._scope(event)
        if scoped is None:
            return
        platform, group_id, scope = scoped
        if not self._group_allowed(group_id):
            return

        sender_id = normalize_user_id(_event_method(event, "get_sender_id", ""))
        if not sender_id:
            return
        self_id = normalize_user_id(_event_method(event, "get_self_id", ""))
        if self_id and sender_id == self_id and not _as_bool(
            _config_value(self.config, "capture_bot_messages", False)
        ):
            return

        components = _components(event)
        text = _event_text(event, components)
        if not text or self._is_command_message(text):
            return
        mentioned_user_ids, reply_user_id = _mentioned_and_reply(components)
        created_at = _created_at(event)
        inserted, pending_count = self.store.add_message(
            scope=scope,
            platform=platform,
            group_id=group_id,
            message_key=_make_message_key(
                scope=scope,
                sender_id=sender_id,
                text=text,
                message_id=_message_id(event),
                created_at=created_at,
                mentioned_user_ids=mentioned_user_ids,
                reply_user_id=reply_user_id,
            ),
            sender_id=sender_id,
            sender_name=_sender_name(event),
            text=text,
            mentioned_user_ids=mentioned_user_ids,
            reply_user_id=reply_user_id,
            created_at=created_at,
        )
        if not inserted:
            return
        max_pending = _as_int(_config_value(self.config, "max_pending_messages", 2000), 2000, 100, 100000)
        if pending_count > max_pending:
            logger.warning(
                "[GlimmerPortraits] 群 %s 待总结消息超过上限：%s > %s；将由后续任务尽快处理",
                group_id,
                pending_count,
                max_pending,
            )
        threshold = _as_int(
            _config_value(self.config, "summary_every_n_messages", 80), 80, 5, 2000
        )
        if (
            _as_bool(_config_value(self.config, "auto_update_enabled", True), True)
            and pending_count >= threshold
        ):
            self._schedule_update(scope, reason="消息量达到阈值")

    # ───────────────────────────── LLM Provider 与总结 ─────────────────────────────

    async def _resolve_provider(self) -> Any:
        provider_id = clean_text(
            _config_value(self.config, "summary_provider_id", ""), 200
        )
        if provider_id:
            resolver = getattr(self.context, "get_provider_by_id", None)
            if not callable(resolver):
                raise RuntimeError("当前 AstrBot 不支持按 Provider ID 选择模型")
            provider = resolver(provider_id)
            if inspect.isawaitable(provider):
                provider = await provider
            if provider is None:
                raise RuntimeError(f"找不到已配置的 Provider：{provider_id}")
            return provider

        resolver = getattr(self.context, "get_using_provider", None)
        if not callable(resolver):
            raise RuntimeError("AstrBot 没有可用的默认 Provider")
        try:
            provider = resolver()
        except TypeError:
            provider = resolver("")
        if inspect.isawaitable(provider):
            provider = await provider
        if provider is None:
            raise RuntimeError("AstrBot 没有可用的默认 Provider")
        return provider

    def _schedule_update(self, scope: str, *, reason: str) -> str:
        current = self._update_tasks.get(scope)
        if current is not None and not current.done():
            return "running"
        task = asyncio.create_task(self._run_update(scope, reason=reason))
        self._update_tasks[scope] = task

        def _done(done_task: asyncio.Task[Any]) -> None:
            if self._update_tasks.get(scope) is done_task:
                self._update_tasks.pop(scope, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                succeeded = bool(done_task.result())
                threshold = _as_int(
                    _config_value(self.config, "summary_every_n_messages", 80), 80, 5, 2000
                )
                if (
                    succeeded
                    and not self._terminating
                    and _as_bool(_config_value(self.config, "auto_update_enabled", True), True)
                    and self.store.pending_count(scope) >= threshold
                ):
                    self._schedule_update(scope, reason="处理总结积压")

        task.add_done_callback(_done)
        return "queued"

    async def _run_update(self, scope: str, *, reason: str) -> bool:
        lock = self._group_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            pending_limit = _as_int(
                _config_value(self.config, "summary_batch_messages", 120), 120, 10, 1000
            )
            pending = self.store.get_pending(scope, pending_limit)
            if not pending:
                return True
            self.store.mark_attempt(scope)
            try:
                provider = await self._resolve_provider()
                numbered = number_messages(pending)
                user_ids = self.store.get_group_users_for_pending(pending)
                old_profiles = self.store.get_profiles(scope, user_ids)
                prompt = build_summary_prompt(
                    numbered,
                    old_profiles,
                    self._enabled_fields(),
                    max_prompt_chars=_as_int(
                        _config_value(self.config, "summary_max_prompt_chars", 24000),
                        24000,
                        4000,
                        100000,
                    ),
                )
                response = provider.text_chat(prompt=prompt)
                if inspect.isawaitable(response):
                    timeout = _as_int(
                        _config_value(self.config, "summary_timeout_seconds", 90), 90, 10, 600
                    )
                    response = await asyncio.wait_for(response, timeout=timeout)
                raw = getattr(response, "completion_text", response)
                if not is_valid_model_result(raw):
                    raise ValueError("画像总结模型返回的不是有效 JSON 对象")
                parsed = parse_model_result(
                    raw,
                    numbered,
                    self._enabled_fields(),
                    min_confidence=_as_float(
                        _config_value(self.config, "min_confidence", 0.58), 0.58, 0.0, 1.0
                    ),
                )
                counts = self.store.apply_result_and_consume(
                    scope,
                    [message["id"] for message in pending],
                    parsed,
                    temporary_ttl_days=_as_int(
                        _config_value(self.config, "temporary_ttl_days", 45), 45, 0, 3650
                    ),
                    observation_ttl_days=_as_int(
                        _config_value(self.config, "observation_ttl_days", 90), 90, 0, 3650
                    ),
                    max_facts_per_user=_as_int(
                        _config_value(self.config, "max_facts_per_user", 12), 12, 1, 100
                    ),
                    max_aliases_per_user=_as_int(
                        _config_value(self.config, "max_aliases_per_user", 8), 8, 1, 100
                    ),
                    max_observations_per_user=_as_int(
                        _config_value(self.config, "max_observations_per_user", 5), 5, 1, 100
                    ),
                    max_relations_per_user=_as_int(
                        _config_value(self.config, "max_relations_per_user", 12), 12, 1, 100
                    ),
                )
                self.store.mark_success(scope)
                logger.info(
                    "[GlimmerPortraits] 群画像总结完成 scope=%s reason=%s messages=%s facts=%s aliases=%s relations=%s observations=%s",
                    scope,
                    reason,
                    len(pending),
                    counts["facts"],
                    counts["aliases"],
                    counts["relations"],
                    counts["observations"],
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.mark_attempt(scope, error=str(exc))
                logger.warning("[GlimmerPortraits] 群画像总结失败 scope=%s: %s", scope, exc)
                return False

    async def _daily_summary_loop(self) -> None:
        while not self._terminating:
            try:
                await asyncio.sleep(30)
                if not _as_bool(_config_value(self.config, "daily_summary_enabled", True), True):
                    continue
                now = datetime.now()
                hour = _as_int(_config_value(self.config, "daily_summary_hour", 3), 3, 0, 23)
                if now.hour != hour:
                    continue
                date_text = now.strftime("%Y-%m-%d")
                minimum = _as_int(
                    _config_value(self.config, "daily_min_messages", 20), 20, 1, 2000
                )
                for state in self.store.list_scopes():
                    scope = state.get("scope", "")
                    if not scope or state.get("last_auto_date") == date_text:
                        continue
                    if self.store.pending_count(scope) < minimum:
                        continue
                    # 先标记日期，避免 30 秒循环重复排队；失败消息仍会保留，管理员可手动重试。
                    self.store.mark_auto_date(scope, date_text)
                    self._schedule_update(scope, reason="每日定时")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[GlimmerPortraits] 每日总结调度异常: %s", exc)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._daily_summary_loop())

    # ───────────────────────────── Prompt 注入 ─────────────────────────────

    @staticmethod
    def _profile_for_injection(profile: dict[str, Any]) -> dict[str, Any]:
        copy = dict(profile)
        # AstrNa 已经负责当前平台昵称；这里保留模型发现的历史/社交称呼。
        copy["aliases"] = [
            item for item in profile.get("aliases", []) if item.get("source") != "platform"
        ]
        return copy

    async def _inject_profile_context(self, event: AstrMessageEvent, req: ProviderRequest):
        if self._terminating or not _as_bool(_config_value(self.config, "enable_injection", True), True):
            return
        scoped = self._scope(event)
        if scoped is None:
            return
        _, group_id, scope = scoped
        if not self._group_allowed(group_id):
            return
        sender_id = normalize_user_id(_event_method(event, "get_sender_id", ""))
        if not sender_id:
            return
        mentioned, reply_user_id = _mentioned_and_reply(_components(event))
        user_ids: list[str] = [sender_id]
        for user_id in (*mentioned, reply_user_id):
            normalized = normalize_user_id(user_id)
            if normalized and normalized not in user_ids:
                user_ids.append(normalized)
        max_users = _as_int(
            _config_value(self.config, "injection_max_users", 4), 4, 1, 12
        )
        profiles = self.store.get_profiles(scope, user_ids[:max_users])
        rendered: list[str] = []
        per_user_chars = _as_int(
            _config_value(self.config, "injection_max_chars_per_user", 500), 500, 120, 3000
        )
        for user_id in user_ids[:max_users]:
            profile = profiles.get(user_id)
            if not profile:
                continue
            profile = self._profile_for_injection(profile)
            if not any(
                profile.get(key)
                for key in ("facts", "observations", "relations", "aliases")
            ):
                continue
            rendered.append(
                render_profile(profile, max_chars=per_user_chars, injection=True)
            )
        if not rendered:
            return
        total_limit = _as_int(
            _config_value(self.config, "injection_max_total_chars", 1400), 1400, 200, 12000
        )
        text = "\n".join(rendered)
        if len(text) > total_limit:
            text = text[: max(0, total_limit - 1)].rstrip() + "…"
        part = _make_temp_text_part(text)
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            parts.append(part)
        else:
            try:
                setattr(req, "extra_user_content_parts", [part])
            except Exception:
                logger.debug("[GlimmerPortraits] ProviderRequest 不支持 extra_user_content_parts")

    @filter.on_llm_request(priority=900)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            await self._inject_profile_context(event, req)
        except Exception as exc:
            # 画像故障不应阻断正常回复。
            logger.warning("[GlimmerPortraits] 画像注入失败: %s", exc)

    # ───────────────────────────── 管理员指令 ─────────────────────────────

    def _overview_text(self, scope: str, group_id: str) -> str:
        limit = _as_int(_config_value(self.config, "overview_page_size", 20), 20, 1, 100)
        active_days = _as_int(_config_value(self.config, "profile_active_days", 30), 30, 0, 3650)
        rows = self.store.get_overview(scope, active_days=active_days, limit=limit)
        pending = self.store.pending_count(scope)
        if not rows:
            return f"【群画像】群 {group_id} 暂无画像记录。待总结消息：{pending}"
        lines = [f"【群画像概览｜群 {group_id}】", f"活跃用户：{len(rows)}，待总结消息：{pending}"]
        for index, row in enumerate(rows, start=1):
            name = clean_text(row.get("display_name", ""), 32) or "未知用户"
            user_id = clean_text(row.get("user_id", ""), 64)
            counts = (
                int(row.get("fact_count", 0) or 0),
                int(row.get("alias_count", 0) or 0),
                int(row.get("relation_count", 0) or 0),
                int(row.get("observation_count", 0) or 0),
            )
            lines.append(
                f"{index}. {name}（{user_id}）｜事实 {counts[0]}｜称呼 {counts[1]}｜关系 {counts[2]}｜观察 {counts[3]}"
            )
        if self.store.overview_total(scope, active_days=active_days) > limit:
            lines.append(f"仅显示前 {limit} 人，可调整 overview_page_size。")
        return "\n".join(lines)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("群画像")
    async def command_group_profile(self, event: AstrMessageEvent, target: str = ""):
        scoped = self._scope(event)
        if scoped is None:
            yield event.plain_result("❌ 请在群聊中使用此指令。")
            return
        _, group_id, scope = scoped
        if not self._group_allowed(group_id):
            yield event.plain_result("❌ 此群未启用群画像。")
            return
        target = self._command_tail(event, "群画像", target)
        if not target or target in {"帮助", "help"}:
            if target:
                yield event.plain_result(
                    "用法：/群画像 查看概览；/群画像 <QQ号> 查看实际注入画像。"
                )
                return
            yield event.plain_result(self._overview_text(scope, group_id))
            return
        if not QQ_ID_RE.fullmatch(target):
            yield event.plain_result("❌ QQ号格式不正确，应为 5～20 位数字。")
            return
        profile = self.store.get_profile(scope, target)
        if not profile:
            yield event.plain_result(f"用户 {target} 在当前群暂无画像。")
            return
        # 与运行时使用同一份过滤逻辑和渲染器，确保管理员看到的就是实际注入内容。
        preview = self._profile_for_injection(profile)
        preview_limit = _as_int(
            _config_value(self.config, "injection_max_chars_per_user", 500),
            500,
            120,
            3000,
        )
        yield event.plain_result(render_profile(preview, max_chars=preview_limit, injection=True))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("更新画像")
    async def command_update_profile(self, event: AstrMessageEvent):
        scoped = self._scope(event)
        if scoped is None:
            yield event.plain_result("❌ 请在群聊中使用此指令。")
            return
        _, group_id, scope = scoped
        if not self._group_allowed(group_id):
            yield event.plain_result("❌ 此群未启用群画像。")
            return
        pending = self.store.pending_count(scope)
        if pending <= 0:
            yield event.plain_result("当前群没有待总结的新消息。")
            return
        status = self._schedule_update(scope, reason="管理员手动触发")
        if status == "running":
            yield event.plain_result(f"当前群画像正在更新中，待处理消息：{pending}。")
        else:
            yield event.plain_result(
                f"✅ 已排队更新当前群画像，待处理消息：{pending}。完成后会写入 SQLite。"
            )

    def _reset_confirmation_key(self, scope: str, event: Any, target: str) -> tuple[str, str, str]:
        return scope, self._admin_id(event), target

    def _reset_preview(self, scope: str, target: str, event: Any) -> str:
        if target == "全群":
            if not _as_bool(_config_value(self.config, "allow_whole_group_reset", False)):
                return "❌ 全群重置默认关闭，请先在插件配置中开启 allow_whole_group_reset。"
            counts = self.store.profile_data_counts(scope)
            label = "全群画像"
        else:
            counts = self.store.profile_data_counts(scope, target)
            label = f"用户 {target} 的当前群画像"
        token = secrets.token_hex(2).upper()
        ttl = _as_int(
            _config_value(self.config, "reset_confirmation_ttl_seconds", 60), 60, 10, 600
        )
        key = self._reset_confirmation_key(scope, event, target)
        self._reset_confirmations[key] = (token, time.time() + ttl)
        return (
            f"⚠️ 即将清除{label}：画像事实 {counts['facts']} 条、称呼 {counts['aliases']} 条、"
            f"关系 {counts['relations']} 条、观察 {counts['observations']} 条、"
            f"待总结消息 {counts['pending_messages']} 条。\n"
            f"如确认，请在 {ttl} 秒内发送：/重置画像 确认 {target} {token}"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("重置画像")
    async def command_reset_profile(self, event: AstrMessageEvent, args: str = ""):
        scoped = self._scope(event)
        if scoped is None:
            yield event.plain_result("❌ 请在群聊中使用此指令。")
            return
        _, group_id, scope = scoped
        if not self._group_allowed(group_id):
            yield event.plain_result("❌ 此群未启用群画像。")
            return
        tail = self._command_tail(event, "重置画像", args)
        parts = tail.split()
        if not parts:
            yield event.plain_result(
                "为避免误删，请明确指定目标：/重置画像 <QQ号> 或 /重置画像 全群。"
            )
            return

        if parts[0] == "确认":
            if len(parts) != 3:
                yield event.plain_result("用法：/重置画像 确认 <QQ号|全群> <确认码>")
                return
            target, token = parts[1], parts[2].upper()
            if target != "全群" and not QQ_ID_RE.fullmatch(target):
                yield event.plain_result("❌ QQ号格式不正确。")
                return
            key = self._reset_confirmation_key(scope, event, target)
            confirmation = self._reset_confirmations.get(key)
            if confirmation is None or confirmation[0] != token or confirmation[1] < time.time():
                self._reset_confirmations.pop(key, None)
                yield event.plain_result("❌ 确认码无效或已过期，请重新执行预览命令。")
                return
            if target == "全群" and not _as_bool(
                _config_value(self.config, "allow_whole_group_reset", False)
            ):
                yield event.plain_result("❌ 全群重置当前未启用。")
                return
            self._reset_confirmations.pop(key, None)
            # 与总结任务共用群锁，避免旧的 in-flight 总结在重置完成后又写回画像。
            lock = self._group_locks.setdefault(scope, asyncio.Lock())
            async with lock:
                if target == "全群":
                    counts = self.store.reset_group(scope, self._admin_id(event))
                    result_text = (
                        f"✅ 已清除当前群全部画像，影响记录约 {counts['affected_rows']} 条。"
                    )
                else:
                    counts = self.store.reset_user(scope, target, self._admin_id(event))
                    result_text = (
                        f"✅ 已清除用户 {target} 在当前群的画像，影响记录约 {counts['affected_rows']} 条。"
                    )
            yield event.plain_result(result_text)
            return

        if len(parts) != 1 or (parts[0] != "全群" and not QQ_ID_RE.fullmatch(parts[0])):
            yield event.plain_result(
                "用法：/重置画像 <QQ号> 或 /重置画像 全群；执行前会要求二次确认。"
            )
            return
        yield event.plain_result(self._reset_preview(scope, parts[0], event))

    async def terminate(self):
        self._terminating = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
        tasks = list(self._update_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._update_tasks.clear()
        self.store.close()
        logger.info("[GlimmerPortraits] 插件已停止")
