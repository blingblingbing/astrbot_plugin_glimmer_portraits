"""SQLite 持久层。

只保存尚未处理的短期群消息，以及经过模型确认后的画像状态。成功总结后，已处理的
消息会被删除；画像中的 evidence 只保留少量截断文本，避免把完整群聊变成长久日志。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .profile_engine import TEMPORARY_FIELDS, canonical_text, clean_text, normalize_alias


def _now() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except (TypeError, ValueError):
        return default


class ProfileStore:
    """单个插件实例使用的 SQLite 存储。"""

    def __init__(
        self, database_path: str | Path, *, observed_alias_ttl_days: int = 0
    ):
        self.database_path = Path(database_path)
        try:
            self.observed_alias_ttl_days = max(0, int(observed_alias_ttl_days))
        except (TypeError, ValueError):
            self.observed_alias_ttl_days = 0
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            str(self.database_path), check_same_thread=False, timeout=30
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                scope TEXT NOT NULL,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_active INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                active_hours_json TEXT NOT NULL DEFAULT '{}',
                last_profile_update INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, user_id)
            );

            CREATE TABLE IF NOT EXISTS pending_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                platform TEXT NOT NULL,
                group_id TEXT NOT NULL,
                message_key TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                mentioned_user_ids_json TEXT NOT NULL DEFAULT '[]',
                reply_user_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE (scope, message_key)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_scope_id
                ON pending_messages(scope, id);

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                user_id TEXT NOT NULL,
                field TEXT NOT NULL,
                value TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'repeated',
                status TEXT NOT NULL DEFAULT 'active',
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                evidence TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE (scope, user_id, field, canonical_value)
            );
            CREATE INDEX IF NOT EXISTS idx_facts_profile
                ON facts(scope, user_id, status, field);

            CREATE TABLE IF NOT EXISTS aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                user_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                canonical_alias TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'observed',
                called_by_user_id TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                evidence TEXT NOT NULL DEFAULT '',
                UNIQUE (scope, user_id, canonical_alias, called_by_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_aliases_profile
                ON aliases(scope, user_id, status);

            CREATE TABLE IF NOT EXISTS alias_callers (
                scope TEXT NOT NULL,
                user_id TEXT NOT NULL,
                canonical_alias TEXT NOT NULL,
                called_by_user_id TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                evidence TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scope, user_id, canonical_alias, called_by_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_alias_callers_profile
                ON alias_callers(scope, user_id, canonical_alias);

            CREATE TABLE IF NOT EXISTS alias_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                user_id TEXT NOT NULL,
                alias TEXT NOT NULL,
                canonical_alias TEXT NOT NULL,
                callers_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                context_confirmations INTEGER NOT NULL DEFAULT 0,
                weak_mentions INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                evidence TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE (scope, user_id, canonical_alias)
            );
            CREATE INDEX IF NOT EXISTS idx_alias_candidates_scope_alias
                ON alias_candidates(scope, canonical_alias, status);

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                from_user_id TEXT NOT NULL,
                to_user_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                value TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                evidence TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                UNIQUE (scope, from_user_id, to_user_id, relation_type, canonical_value)
            );
            CREATE INDEX IF NOT EXISTS idx_relations_scope_users
                ON relations(scope, from_user_id, to_user_id, status);

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                user_id TEXT NOT NULL,
                field TEXT NOT NULL DEFAULT 'observations',
                value TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                evidence TEXT NOT NULL DEFAULT '',
                expires_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                UNIQUE (scope, user_id, field, canonical_value)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_profile
                ON observations(scope, user_id, status);

            CREATE TABLE IF NOT EXISTS group_state (
                scope TEXT PRIMARY KEY,
                platform TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                last_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_success_at INTEGER NOT NULL DEFAULT 0,
                last_auto_date TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS reset_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                admin_user_id TEXT NOT NULL,
                target_user_id TEXT NOT NULL DEFAULT '',
                reset_type TEXT NOT NULL,
                affected_rows INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.connection.commit()
        self._migrate_alias_callers()
        self._collapse_aliases()
        self._collapse_relations()
        self._collapse_communication_styles()
        self.connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_alias
            ON aliases(scope, user_id, canonical_alias)
            WHERE status = 'active';

            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_relation_type
            ON relations(scope, from_user_id, to_user_id, relation_type)
            WHERE status = 'active';

            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_communication_style
            ON observations(scope, user_id)
            WHERE field = 'communication_style' AND status = 'active';
            """
        )
        self.connection.commit()

    def _migrate_alias_callers(self) -> int:
        """一次性把旧昵称行中的称呼者拆分到来源表。"""

        marker = "alias_callers_v1"
        if self.connection.execute(
            "SELECT 1 FROM schema_meta WHERE key = ?", (marker,)
        ).fetchone():
            return 0
        rows = self.connection.execute(
            """
            SELECT scope, user_id, canonical_alias, called_by_user_id, confidence,
                   occurrences, first_seen, last_seen, evidence
            FROM aliases
            WHERE called_by_user_id <> ''
            """
        ).fetchall()
        cursor = self.connection.cursor()
        try:
            cursor.execute("BEGIN")
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO alias_callers
                    (scope, user_id, canonical_alias, called_by_user_id, confidence,
                     occurrences, first_seen, last_seen, evidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope, user_id, canonical_alias, called_by_user_id)
                    DO UPDATE SET
                      confidence=MAX(alias_callers.confidence, excluded.confidence),
                      occurrences=alias_callers.occurrences + excluded.occurrences,
                      first_seen=MIN(alias_callers.first_seen, excluded.first_seen),
                      last_seen=MAX(alias_callers.last_seen, excluded.last_seen),
                      evidence=CASE WHEN excluded.last_seen >= alias_callers.last_seen
                                    THEN excluded.evidence ELSE alias_callers.evidence END
                    """,
                    tuple(row),
                )
            cursor.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (marker, str(_now())),
            )
            self.connection.commit()
            return len(rows)
        except Exception:
            self.connection.rollback()
            raise

    def _collapse_aliases(self) -> int:
        """同一用户的同一昵称只保留一条活跃主记录。"""

        rows = self.connection.execute(
            """
            SELECT * FROM aliases WHERE status = 'active'
            ORDER BY scope, user_id, canonical_alias, confidence DESC,
                     occurrences DESC, last_seen DESC, id DESC
            """
        ).fetchall()
        groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["scope"]), str(row["user_id"]), str(row["canonical_alias"]))
            groups.setdefault(key, []).append(row)
        cursor = self.connection.cursor()
        collapsed = 0
        try:
            cursor.execute("BEGIN")
            for group in groups.values():
                if len(group) < 2:
                    continue
                winner = group[0]
                duplicate_ids = [int(row["id"]) for row in group[1:]]
                placeholders = ",".join("?" for _ in duplicate_ids)
                cursor.execute(
                    f"UPDATE aliases SET status='archived' WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
                cursor.execute(
                    """
                    UPDATE aliases
                    SET confidence = ?, occurrences = ?, first_seen = ?, last_seen = ?,
                        source = ?, evidence = ?
                    WHERE id = ?
                    """,
                    (
                        max(float(row["confidence"] or 0) for row in group),
                        sum(int(row["occurrences"] or 0) for row in group),
                        min(int(row["first_seen"] or 0) for row in group),
                        max(int(row["last_seen"] or 0) for row in group),
                        "platform"
                        if any(row["source"] == "platform" for row in group)
                        else "explicit"
                        if any(row["source"] == "explicit" for row in group)
                        else "observed",
                        str(winner["evidence"] or ""),
                        int(winner["id"]),
                    ),
                )
                collapsed += len(duplicate_ids)
            self.connection.commit()
            return collapsed
        except Exception:
            self.connection.rollback()
            raise

    def _collapse_relations(self) -> int:
        """同方向用户对的同一关系类型只保留一条活跃记录。"""

        rows = self.connection.execute(
            """
            SELECT * FROM relations WHERE status = 'active'
            ORDER BY scope, from_user_id, to_user_id, relation_type,
                     confidence DESC, occurrences DESC, last_seen DESC, id DESC
            """
        ).fetchall()
        groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (
                str(row["scope"]),
                str(row["from_user_id"]),
                str(row["to_user_id"]),
                str(row["relation_type"]),
            )
            groups.setdefault(key, []).append(row)
        cursor = self.connection.cursor()
        collapsed = 0
        try:
            cursor.execute("BEGIN")
            for group in groups.values():
                if len(group) < 2:
                    continue
                winner = group[0]
                duplicate_ids = [int(row["id"]) for row in group[1:]]
                placeholders = ",".join("?" for _ in duplicate_ids)
                cursor.execute(
                    f"UPDATE relations SET status='archived' WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
                cursor.execute(
                    """
                    UPDATE relations
                    SET confidence = ?, occurrences = ?, first_seen = ?, last_seen = ?,
                        expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        max(float(row["confidence"] or 0) for row in group),
                        sum(int(row["occurrences"] or 0) for row in group),
                        min(int(row["first_seen"] or 0) for row in group),
                        max(int(row["last_seen"] or 0) for row in group),
                        max(int(row["expires_at"] or 0) for row in group),
                        int(winner["id"]),
                    ),
                )
                collapsed += len(duplicate_ids)
            self.connection.commit()
            return collapsed
        except Exception:
            self.connection.rollback()
            raise

    def _collapse_communication_styles(self) -> int:
        """将旧版本累积的沟通风格收敛为每位用户一条活跃记录。"""

        rows = self.connection.execute(
            """
            SELECT id, scope, user_id
            FROM observations
            WHERE field = 'communication_style' AND status = 'active'
            ORDER BY scope, user_id, confidence DESC, last_seen DESC, id DESC
            """
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        archive_ids: list[int] = []
        for row in rows:
            key = (str(row["scope"]), str(row["user_id"]))
            if key in seen:
                archive_ids.append(int(row["id"]))
            else:
                seen.add(key)
        if archive_ids:
            placeholders = ",".join("?" for _ in archive_ids)
            self.connection.execute(
                f"UPDATE observations SET status='archived' WHERE id IN ({placeholders})",
                archive_ids,
            )
            self.connection.commit()
        return len(archive_ids)

    def add_message(
        self,
        *,
        scope: str,
        platform: str,
        group_id: str,
        message_key: str,
        sender_id: str,
        sender_name: str,
        text: str,
        mentioned_user_ids: Iterable[str] = (),
        reply_user_id: str = "",
        created_at: int | None = None,
    ) -> tuple[bool, int]:
        """保存一条待总结消息，并更新本地统计。

        返回 ``(是否新插入, 当前待总结条数)``。消息 ID 重复时不会重复计数。
        """

        now = int(created_at or _now())
        scope = clean_text(scope, 200)
        platform = clean_text(platform, 60) or "unknown"
        group_id = clean_text(group_id, 128)
        sender_id = clean_text(sender_id, 128)
        sender_name = clean_text(sender_name, 80)
        text = clean_text(text, 500)
        message_key = clean_text(message_key, 240)
        mention_ids = [clean_text(value, 128) for value in mentioned_user_ids if clean_text(value, 128)]
        reply_user_id = clean_text(reply_user_id, 128)
        if not scope or not group_id or not sender_id or not text or not message_key:
            return False, self.pending_count(scope)

        cursor = self.connection.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                """
                INSERT OR IGNORE INTO pending_messages
                (scope, platform, group_id, message_key, sender_id, sender_name, text,
                 mentioned_user_ids_json, reply_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    platform,
                    group_id,
                    message_key,
                    sender_id,
                    sender_name,
                    text,
                    _json(list(dict.fromkeys(mention_ids))),
                    reply_user_id,
                    now,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                row = cursor.execute(
                    "SELECT * FROM profiles WHERE scope = ? AND user_id = ?",
                    (scope, sender_id),
                ).fetchone()
                if row is None:
                    active_hours = {str(now_hour(now)): 1}
                    cursor.execute(
                        """
                        INSERT INTO profiles
                        (scope, platform, group_id, user_id, display_name, first_seen,
                         last_active, message_count, active_hours_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            scope,
                            platform,
                            group_id,
                            sender_id,
                            sender_name,
                            now,
                            now,
                            _json(active_hours),
                        ),
                    )
                else:
                    active_hours = _load_json(row["active_hours_json"], {})
                    if not isinstance(active_hours, dict):
                        active_hours = {}
                    hour_key = str(now_hour(now))
                    try:
                        active_hours[hour_key] = int(active_hours.get(hour_key, 0)) + 1
                    except (TypeError, ValueError):
                        active_hours[hour_key] = 1
                    cursor.execute(
                        """
                        UPDATE profiles
                        SET platform = ?, group_id = ?,
                            display_name = CASE WHEN ? <> '' THEN ? ELSE display_name END,
                            last_active = ?, message_count = message_count + 1,
                            active_hours_json = ?
                        WHERE scope = ? AND user_id = ?
                        """,
                        (
                            platform,
                            group_id,
                            sender_name,
                            sender_name,
                            now,
                            _json(active_hours),
                            scope,
                            sender_id,
                        ),
                    )
                if sender_name:
                    self._upsert_alias_cursor(
                        cursor,
                        scope=scope,
                        user_id=sender_id,
                        alias=sender_name,
                        source="platform",
                        called_by_user_id="",
                        confidence=1.0,
                        evidence="平台展示名",
                        now=now,
                    )
                self._ensure_group_state_cursor(cursor, scope, platform, group_id)
            pending = int(
                cursor.execute(
                    "SELECT COUNT(*) FROM pending_messages WHERE scope = ?", (scope,)
                ).fetchone()[0]
            )
            self.connection.commit()
            return inserted, pending
        except Exception:
            self.connection.rollback()
            raise

    def pending_count(self, scope: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM pending_messages WHERE scope = ?", (scope,)
        ).fetchone()
        return int(row[0] if row else 0)

    def get_pending(self, scope: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        rows = self.connection.execute(
            """
            SELECT id, scope, platform, group_id, sender_id, sender_name, text,
                   mentioned_user_ids_json, reply_user_id, created_at
            FROM pending_messages
            WHERE scope = ? ORDER BY id ASC LIMIT ?
            """,
            (scope, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            mentioned = _load_json(row["mentioned_user_ids_json"], [])
            if not isinstance(mentioned, list):
                mentioned = []
            result.append(
                {
                    "id": int(row["id"]),
                    "scope": row["scope"],
                    "platform": row["platform"],
                    "group_id": row["group_id"],
                    "sender_id": row["sender_id"],
                    "sender_name": row["sender_name"],
                    "text": row["text"],
                    "mentioned_user_ids": tuple(str(value) for value in mentioned),
                    "reply_user_id": row["reply_user_id"],
                    "created_at": int(row["created_at"] or 0),
                }
            )
        return result

    def get_profiles(
        self,
        scope: str,
        user_ids: Iterable[str] | None = None,
        *,
        include_inactive: bool = False,
    ) -> dict[str, dict[str, Any]]:
        self.expire_stale()
        params: list[Any] = [scope]
        query = "SELECT * FROM profiles WHERE scope = ?"
        ids = [str(value) for value in (user_ids or []) if str(value)]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            query += f" AND user_id IN ({placeholders})"
            params.extend(ids)
        query += " ORDER BY last_active DESC"
        rows = self.connection.execute(query, params).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            user_id = str(row["user_id"])
            profile: dict[str, Any] = {
                "scope": row["scope"],
                "platform": row["platform"],
                "group_id": row["group_id"],
                "user_id": user_id,
                "display_name": row["display_name"],
                "first_seen": int(row["first_seen"] or 0),
                "last_active": int(row["last_active"] or 0),
                "message_count": int(row["message_count"] or 0),
                "active_hours": _load_json(row["active_hours_json"], {}),
                "last_updated": int(row["last_profile_update"] or 0),
                "facts": [],
                "aliases": [],
                "observations": [],
                "relations": [],
            }
            result[user_id] = profile

        if not result:
            return result
        selected_ids = list(result)
        placeholders = ",".join("?" for _ in selected_ids)
        base_params = [scope, *selected_ids]

        fact_rows = self.connection.execute(
            f"SELECT * FROM facts WHERE scope = ? AND user_id IN ({placeholders}) "
            "AND status = 'active' ORDER BY confidence DESC, last_seen DESC",
            base_params,
        ).fetchall()
        for row in fact_rows:
            result[str(row["user_id"])]["facts"].append(dict(row))

        alias_rows = self.connection.execute(
            f"SELECT * FROM aliases WHERE scope = ? AND user_id IN ({placeholders}) "
            "AND status = 'active' ORDER BY confidence DESC, occurrences DESC, last_seen DESC",
            base_params,
        ).fetchall()
        alias_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in alias_rows:
            data = dict(row)
            data["called_by_user_ids"] = []
            result[str(row["user_id"])]["aliases"].append(data)
            alias_by_key[(str(row["user_id"]), str(row["canonical_alias"]))] = data

        caller_rows = self.connection.execute(
            f"SELECT * FROM alias_callers WHERE scope = ? AND user_id IN ({placeholders}) "
            "ORDER BY occurrences DESC, confidence DESC, last_seen DESC",
            base_params,
        ).fetchall()
        for row in caller_rows:
            alias = alias_by_key.get((str(row["user_id"]), str(row["canonical_alias"])))
            if alias is not None:
                alias["called_by_user_ids"].append(str(row["called_by_user_id"]))

        observation_rows = self.connection.execute(
            f"SELECT * FROM observations WHERE scope = ? AND user_id IN ({placeholders}) "
            "AND status = 'active' ORDER BY confidence DESC, last_seen DESC",
            base_params,
        ).fetchall()
        for row in observation_rows:
            result[str(row["user_id"])]["observations"].append(dict(row))

        relation_rows = self.connection.execute(
            f"""SELECT * FROM relations
                WHERE scope = ? AND (from_user_id IN ({placeholders}) OR to_user_id IN ({placeholders}))
                AND status = 'active'
                ORDER BY confidence DESC, occurrences DESC, last_seen DESC""",
            [scope, *selected_ids, *selected_ids],
        ).fetchall()
        for row in relation_rows:
            data = dict(row)
            from_user = str(row["from_user_id"])
            to_user = str(row["to_user_id"])
            if from_user in result:
                result[from_user]["relations"].append(data)
            if to_user in result and to_user != from_user:
                result[to_user]["relations"].append(data)

        return result

    def get_profile(self, scope: str, user_id: str) -> dict[str, Any] | None:
        return self.get_profiles(scope, [user_id]).get(str(user_id))

    def get_overview(
        self, scope: str, *, active_days: int = 30, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        self.expire_stale()
        cutoff = _now() - max(0, int(active_days)) * 86400
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        rows = self.connection.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM facts f WHERE f.scope=p.scope AND f.user_id=p.user_id AND f.status='active') AS fact_count,
              (SELECT COUNT(*) FROM aliases a WHERE a.scope=p.scope AND a.user_id=p.user_id AND a.status='active') AS alias_count,
              (SELECT COUNT(*) FROM observations o WHERE o.scope=p.scope AND o.user_id=p.user_id AND o.status='active') AS observation_count,
              (SELECT COUNT(*) FROM relations r WHERE r.scope=p.scope AND r.status='active'
                 AND (r.from_user_id=p.user_id OR r.to_user_id=p.user_id)) AS relation_count
            FROM profiles p
            WHERE p.scope = ? AND p.last_active >= ?
            ORDER BY p.last_active DESC
            LIMIT ? OFFSET ?
            """,
            (scope, cutoff, limit, offset),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["active_hours"] = _load_json(data.get("active_hours_json"), {})
            result.append(data)
        return result

    def overview_total(self, scope: str, *, active_days: int = 30) -> int:
        cutoff = _now() - max(0, int(active_days)) * 86400
        row = self.connection.execute(
            "SELECT COUNT(*) FROM profiles WHERE scope = ? AND last_active >= ?",
            (scope, cutoff),
        ).fetchone()
        return int(row[0] if row else 0)

    def get_group_users_for_pending(self, messages: Iterable[Mapping[str, Any]]) -> list[str]:
        result: list[str] = []
        for message in messages:
            candidates = [
                message.get("sender_id", ""),
                *(message.get("mentioned_user_ids", ()) or ()),
                message.get("reply_user_id", ""),
            ]
            for value in candidates:
                user_id = clean_text(value, 128)
                if user_id and user_id not in result:
                    result.append(user_id)
        return result

    def state(self, scope: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM group_state WHERE scope = ?", (scope,)
        ).fetchone()
        return dict(row) if row else {}

    def mark_attempt(self, scope: str, *, error: str = "") -> None:
        now = _now()
        self.connection.execute(
            """
            INSERT INTO group_state(scope, last_attempt_at, last_error)
            VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error
            """,
            (scope, now, clean_text(error, 500)),
        )
        self.connection.commit()

    def mark_success(self, scope: str) -> None:
        now = _now()
        self.connection.execute(
            """
            INSERT INTO group_state(scope, last_success_at, last_error)
            VALUES (?, ?, '')
            ON CONFLICT(scope) DO UPDATE SET last_success_at=excluded.last_success_at,
                last_error=''
            """,
            (scope, now),
        )
        self.connection.commit()

    def mark_auto_date(self, scope: str, date_text: str) -> None:
        self.connection.execute(
            """
            INSERT INTO group_state(scope, last_auto_date)
            VALUES (?, ?)
            ON CONFLICT(scope) DO UPDATE SET last_auto_date=excluded.last_auto_date
            """,
            (scope, date_text),
        )
        self.connection.commit()

    def list_scopes(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT scope, platform, group_id FROM group_state ORDER BY group_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def alias_target_map(self, scope: str) -> dict[str, set[str]]:
        rows = self.connection.execute(
            """
            SELECT canonical_alias, user_id FROM aliases
            WHERE scope = ? AND status = 'active'
            """,
            (scope,),
        ).fetchall()
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(str(row["canonical_alias"]), set()).add(
                str(row["user_id"])
            )
        return result

    def archive_alias(self, scope: str, user_id: str, alias: str) -> dict[str, int]:
        """归档指定用户的正式昵称及其候选记录，不物理删除历史数据。"""

        scope = clean_text(scope, 200)
        user_id = clean_text(user_id, 128)
        normalized_alias = normalize_alias(alias)
        canonical = canonical_text(normalized_alias or "")
        result = {"aliases": 0, "alias_candidates": 0}
        if not scope or not user_id or not canonical:
            return result

        cursor = self.connection.cursor()
        try:
            cursor.execute("BEGIN")
            cursor.execute(
                """
                UPDATE aliases SET status='archived'
                WHERE scope = ? AND user_id = ? AND canonical_alias = ?
                  AND status = 'active'
                """,
                (scope, user_id, canonical),
            )
            result["aliases"] = max(0, int(cursor.rowcount or 0))
            cursor.execute(
                """
                UPDATE alias_candidates SET status='archived'
                WHERE scope = ? AND user_id = ? AND canonical_alias = ?
                  AND status IN ('pending', 'conflict')
                """,
                (scope, user_id, canonical),
            )
            result["alias_candidates"] = max(0, int(cursor.rowcount or 0))
            if result["aliases"] or result["alias_candidates"]:
                cursor.execute(
                    """
                    UPDATE profiles SET last_profile_update = ?
                    WHERE scope = ? AND user_id = ?
                    """,
                    (_now(), scope, user_id),
                )
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def apply_result_and_consume(
        self,
        scope: str,
        message_ids: Iterable[int],
        result: Mapping[str, Iterable[Mapping[str, Any]]],
        *,
        temporary_ttl_days: int = 45,
        observation_ttl_days: int = 90,
        alias_candidate_ttl_days: int = 30,
        alias_context_confirmations: int = 2,
        max_facts_per_user: int = 12,
        max_aliases_per_user: int = 8,
        max_observations_per_user: int = 5,
        max_relations_per_user: int = 12,
    ) -> dict[str, int]:
        """原子地合并模型结果并删除本次已经处理的消息。"""

        now = _now()
        cursor = self.connection.cursor()
        counts = {
            "facts": 0,
            "aliases": 0,
            "alias_candidates": 0,
            "alias_activity": 0,
            "relations": 0,
            "observations": 0,
        }
        try:
            cursor.execute("BEGIN")
            for item in result.get("facts", []) or []:
                self._apply_fact_cursor(
                    cursor,
                    scope,
                    item,
                    now,
                    temporary_ttl_days=max(0, int(temporary_ttl_days)),
                )
                counts["facts"] += 1
            for item in result.get("aliases", []) or []:
                self._apply_alias_cursor(cursor, scope, item, now)
                counts["aliases"] += 1
            for item in result.get("alias_candidates", []) or []:
                promoted = self._apply_alias_candidate_cursor(
                    cursor,
                    scope,
                    item,
                    now,
                    ttl_days=max(1, int(alias_candidate_ttl_days)),
                    required_confirmations=max(2, int(alias_context_confirmations)),
                )
                counts["alias_candidates"] += 1
                if promoted:
                    counts["aliases"] += 1
            for item in result.get("alias_activity", []) or []:
                if self._apply_alias_activity_cursor(cursor, scope, item, now):
                    counts["alias_activity"] += 1
            for item in result.get("relations", []) or []:
                self._apply_relation_cursor(
                    cursor,
                    scope,
                    item,
                    now,
                    temporary_ttl_days=max(0, int(temporary_ttl_days)),
                )
                counts["relations"] += 1
            for item in result.get("observations", []) or []:
                self._apply_observation_cursor(
                    cursor,
                    scope,
                    item,
                    now,
                    observation_ttl_days=max(0, int(observation_ttl_days)),
                )
                counts["observations"] += 1

            self._enforce_limits_cursor(
                cursor,
                scope,
                max_facts_per_user=max(1, int(max_facts_per_user)),
                max_aliases_per_user=max(1, int(max_aliases_per_user)),
                max_observations_per_user=max(1, int(max_observations_per_user)),
                max_relations_per_user=max(1, int(max_relations_per_user)),
            )
            ids = [int(value) for value in message_ids if int(value) > 0]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                cursor.execute(
                    f"DELETE FROM pending_messages WHERE scope = ? AND id IN ({placeholders})",
                    [scope, *ids],
                )
            affected_users = {
                str(item.get("user_id", ""))
                for key in ("facts", "observations")
                for item in result.get(key, []) or []
                if str(item.get("user_id", ""))
            }
            affected_users.update(
                str(item.get("target_user_id", ""))
                for key in ("aliases", "alias_candidates", "alias_activity")
                for item in result.get(key, []) or []
                if str(item.get("target_user_id", ""))
            )
            affected_users.update(
                str(item.get("from_user_id", ""))
                for item in result.get("relations", []) or []
                if str(item.get("from_user_id", ""))
            )
            affected_users.update(
                str(item.get("to_user_id", ""))
                for item in result.get("relations", []) or []
                if str(item.get("to_user_id", ""))
            )
            for user_id in affected_users:
                cursor.execute(
                    "UPDATE profiles SET last_profile_update = ? WHERE scope = ? AND user_id = ?",
                    (now, scope, user_id),
                )
            self.connection.commit()
            return counts
        except Exception:
            self.connection.rollback()
            raise

    def profile_data_counts(self, scope: str, user_id: str | None = None) -> dict[str, int]:
        clauses = "scope = ?"
        params: list[Any] = [scope]
        if user_id:
            clauses += " AND user_id = ?"
            params.append(user_id)
        result: dict[str, int] = {}
        for table, column in (
            ("profiles", "user_id"),
            ("facts", "user_id"),
            ("aliases", "user_id"),
            ("alias_callers", "user_id"),
            ("alias_candidates", "user_id"),
            ("observations", "user_id"),
        ):
            query = f"SELECT COUNT(*) FROM {table} WHERE {clauses}"
            if table == "profiles":
                query += ""
            elif table in {"facts", "aliases", "observations"}:
                query += " AND status = 'active'"
            row = self.connection.execute(query, params).fetchone()
            result[table] = int(row[0] if row else 0)
        relation_clauses = "scope = ? AND status = 'active'"
        relation_params: list[Any] = [scope]
        if user_id:
            relation_clauses += " AND (from_user_id = ? OR to_user_id = ?)"
            relation_params.extend([user_id, user_id])
        row = self.connection.execute(
            f"SELECT COUNT(*) FROM relations WHERE {relation_clauses}", relation_params
        ).fetchone()
        result["relations"] = int(row[0] if row else 0)
        if user_id:
            pending_rows = self.connection.execute(
                "SELECT sender_id, reply_user_id, mentioned_user_ids_json "
                "FROM pending_messages WHERE scope = ?",
                (scope,),
            ).fetchall()
            pending_count = 0
            for row in pending_rows:
                mentioned = _load_json(row["mentioned_user_ids_json"], [])
                if (
                    row["sender_id"] == user_id
                    or row["reply_user_id"] == user_id
                    or user_id in mentioned
                ):
                    pending_count += 1
            result["pending_messages"] = pending_count
        else:
            result["pending_messages"] = self.pending_count(scope)
        return result

    def reset_user(self, scope: str, user_id: str, admin_user_id: str) -> dict[str, int]:
        return self._reset(scope, user_id, admin_user_id, reset_type="user")

    def reset_group(self, scope: str, admin_user_id: str) -> dict[str, int]:
        return self._reset(scope, None, admin_user_id, reset_type="group")

    def _reset(
        self,
        scope: str,
        user_id: str | None,
        admin_user_id: str,
        *,
        reset_type: str,
    ) -> dict[str, int]:
        cursor = self.connection.cursor()
        try:
            cursor.execute("BEGIN")
            counts = self.profile_data_counts(scope, user_id)
            if user_id:
                cursor.execute("DELETE FROM facts WHERE scope = ? AND user_id = ?", (scope, user_id))
                cursor.execute("DELETE FROM aliases WHERE scope = ? AND user_id = ?", (scope, user_id))
                cursor.execute("DELETE FROM alias_callers WHERE scope = ? AND user_id = ?", (scope, user_id))
                cursor.execute("DELETE FROM alias_candidates WHERE scope = ? AND user_id = ?", (scope, user_id))
                cursor.execute("DELETE FROM observations WHERE scope = ? AND user_id = ?", (scope, user_id))
                cursor.execute(
                    "DELETE FROM relations WHERE scope = ? AND (from_user_id = ? OR to_user_id = ?)",
                    (scope, user_id, user_id),
                )
                cursor.execute("DELETE FROM profiles WHERE scope = ? AND user_id = ?", (scope, user_id))
                pending_rows = cursor.execute(
                    "SELECT id, sender_id, reply_user_id, mentioned_user_ids_json "
                    "FROM pending_messages WHERE scope = ?",
                    (scope,),
                ).fetchall()
                pending_ids: list[int] = []
                for row in pending_rows:
                    mentioned = _load_json(row["mentioned_user_ids_json"], [])
                    if (
                        row["sender_id"] == user_id
                        or row["reply_user_id"] == user_id
                        or user_id in mentioned
                    ):
                        pending_ids.append(int(row["id"]))
                if pending_ids:
                    placeholders = ",".join("?" for _ in pending_ids)
                    cursor.execute(
                        f"DELETE FROM pending_messages WHERE scope = ? AND id IN ({placeholders})",
                        [scope, *pending_ids],
                    )
                affected = sum(counts.values())
            else:
                for table in (
                    "facts",
                    "aliases",
                    "alias_callers",
                    "alias_candidates",
                    "relations",
                    "observations",
                    "profiles",
                    "pending_messages",
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE scope = ?", (scope,))
                affected = sum(counts.values())
            cursor.execute(
                "INSERT INTO reset_audit(scope, admin_user_id, target_user_id, reset_type, affected_rows, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scope, admin_user_id, user_id or "", reset_type, affected, _now()),
            )
            if reset_type == "group":
                cursor.execute("DELETE FROM group_state WHERE scope = ?", (scope,))
            self.connection.commit()
            counts["affected_rows"] = affected
            return counts
        except Exception:
            self.connection.rollback()
            raise

    def expire_stale(self) -> None:
        now = _now()
        self.connection.execute("BEGIN")
        try:
            self.connection.execute(
                "UPDATE facts SET status='expired' WHERE status='active' AND expires_at > 0 AND expires_at <= ?",
                (now,),
            )
            self.connection.execute(
                "UPDATE observations SET status='expired' WHERE status='active' AND expires_at > 0 AND expires_at <= ?",
                (now,),
            )
            self.connection.execute(
                "UPDATE relations SET status='expired' WHERE status='active' AND expires_at > 0 AND expires_at <= ?",
                (now,),
            )
            self.connection.execute(
                """
                UPDATE alias_candidates SET status='expired'
                WHERE status IN ('pending', 'conflict')
                  AND expires_at > 0 AND expires_at <= ?
                """,
                (now,),
            )
            if self.observed_alias_ttl_days > 0:
                cutoff = now - self.observed_alias_ttl_days * 86400
                self.connection.execute(
                    """
                    UPDATE aliases SET status='expired'
                    WHERE status = 'active' AND source = 'observed'
                      AND last_seen > 0 AND last_seen <= ?
                    """,
                    (cutoff,),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _ensure_group_state_cursor(
        self, cursor: sqlite3.Cursor, scope: str, platform: str, group_id: str
    ) -> None:
        cursor.execute(
            """
            INSERT INTO group_state(scope, platform, group_id)
            VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET platform=excluded.platform, group_id=excluded.group_id
            """,
            (scope, platform, group_id),
        )

    def _upsert_alias_cursor(
        self,
        cursor: sqlite3.Cursor,
        *,
        scope: str,
        user_id: str,
        alias: str,
        source: str,
        called_by_user_id: str,
        confidence: float,
        evidence: str,
        now: int,
    ) -> None:
        alias = normalize_alias(alias)
        if not alias:
            return
        source = clean_text(source, 20).lower()
        if source not in {"platform", "explicit"}:
            source = "observed"
        canonical = canonical_text(alias)
        if called_by_user_id:
            cursor.execute(
                """
                INSERT INTO alias_callers
                (scope, user_id, canonical_alias, called_by_user_id, confidence,
                 occurrences, first_seen, last_seen, evidence)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(scope, user_id, canonical_alias, called_by_user_id)
                DO UPDATE SET
                  confidence=MAX(alias_callers.confidence, excluded.confidence),
                  occurrences=alias_callers.occurrences + 1,
                  last_seen=excluded.last_seen, evidence=excluded.evidence
                """,
                (
                    scope,
                    user_id,
                    canonical,
                    called_by_user_id,
                    confidence,
                    now,
                    now,
                    clean_text(evidence, 360),
                ),
            )
        active = cursor.execute(
            """
            SELECT id FROM aliases
            WHERE scope = ? AND user_id = ? AND canonical_alias = ? AND status = 'active'
            LIMIT 1
            """,
            (scope, user_id, canonical),
        ).fetchone()
        if active:
            cursor.execute(
                """
                UPDATE aliases
                SET alias = ?,
                    source = CASE
                        WHEN source='platform' OR ?='platform' THEN 'platform'
                        WHEN source='explicit' OR ?='explicit' THEN 'explicit'
                        ELSE 'observed'
                    END,
                    confidence = MAX(confidence, ?), occurrences = occurrences + 1,
                    last_seen = ?, evidence = ?, status = 'active'
                WHERE id = ?
                """,
                (
                    alias,
                    source,
                    source,
                    confidence,
                    now,
                    clean_text(evidence, 360),
                    int(active["id"]),
                ),
            )
            return
        cursor.execute(
            """
            INSERT INTO aliases
            (scope, user_id, alias, canonical_alias, source, called_by_user_id, confidence,
             occurrences, first_seen, last_seen, status, evidence)
            VALUES (?, ?, ?, ?, ?, '', ?, 1, ?, ?, 'active', ?)
            ON CONFLICT(scope, user_id, canonical_alias, called_by_user_id) DO UPDATE SET
              alias=excluded.alias, source=excluded.source,
              confidence=MAX(aliases.confidence, excluded.confidence),
              occurrences=aliases.occurrences + 1,
              last_seen=excluded.last_seen, status='active', evidence=excluded.evidence
            """,
            (
                scope,
                user_id,
                alias,
                canonical,
                source,
                confidence,
                now,
                now,
                clean_text(evidence, 360),
            ),
        )

    def _apply_fact_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        item: Mapping[str, Any],
        now: int,
        *,
        temporary_ttl_days: int,
    ) -> None:
        user_id = clean_text(item.get("user_id", ""), 128)
        field = clean_text(item.get("field", ""), 64)
        value = clean_text(item.get("value", ""), 200)
        action = clean_text(item.get("action", "add"), 20).lower()
        if not user_id or not field:
            return
        if action in {"expire", "remove"} and not value:
            cursor.execute(
                "UPDATE facts SET status = 'expired', updated_at = ? "
                "WHERE scope = ? AND user_id = ? AND field = ? AND status = 'active'",
                (now, scope, user_id, field),
            )
            return
        if not value:
            return
        canonical = canonical_text(value)
        if action in {"expire", "remove"}:
            cursor.execute(
                "UPDATE facts SET status = 'expired', updated_at = ? "
                "WHERE scope = ? AND user_id = ? AND field = ? AND canonical_value = ?",
                (now, scope, user_id, field, canonical),
            )
            return
        if action == "replace":
            cursor.execute(
                "UPDATE facts SET status = 'superseded', updated_at = ? "
                "WHERE scope = ? AND user_id = ? AND field = ? AND status = 'active' "
                "AND canonical_value <> ?",
                (now, scope, user_id, field, canonical),
            )
        confidence = float(item.get("confidence", 0) or 0)
        source = clean_text(item.get("source", "repeated"), 24) or "repeated"
        expires_at = now + temporary_ttl_days * 86400 if field in TEMPORARY_FIELDS and temporary_ttl_days else 0
        evidence = clean_text(item.get("evidence", ""), 360)
        cursor.execute(
            """
            INSERT INTO facts
            (scope, user_id, field, value, canonical_value, confidence, source, status,
             first_seen, last_seen, evidence_count, evidence, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1, ?, ?, ?)
            ON CONFLICT(scope, user_id, field, canonical_value) DO UPDATE SET
              value=excluded.value,
              confidence=MAX(facts.confidence, excluded.confidence),
              source=CASE WHEN excluded.source='explicit' THEN 'explicit' ELSE facts.source END,
              status='active', last_seen=excluded.last_seen,
              evidence_count=facts.evidence_count + 1,
              evidence=CASE WHEN excluded.evidence <> '' THEN excluded.evidence ELSE facts.evidence END,
              expires_at=excluded.expires_at, updated_at=excluded.updated_at
            """,
            (
                scope,
                user_id,
                field,
                value,
                canonical,
                confidence,
                source,
                now,
                now,
                evidence,
                expires_at,
                now,
            ),
        )

    def _apply_alias_cursor(
        self, cursor: sqlite3.Cursor, scope: str, item: Mapping[str, Any], now: int
    ) -> None:
        target = clean_text(item.get("target_user_id", ""), 128)
        alias = normalize_alias(item.get("alias", ""))
        if not target or not alias:
            return
        called_by = clean_text(item.get("called_by_user_id", ""), 128)
        attribution_type = clean_text(item.get("attribution_type", ""), 32).lower()
        requested_source = clean_text(item.get("source", ""), 20).lower()
        source = (
            "explicit"
            if requested_source == "explicit"
            or attribution_type in {"self_declaration", "explicit_at"}
            else "observed"
        )
        self._upsert_alias_cursor(
            cursor,
            scope=scope,
            user_id=target,
            alias=alias,
            source=source,
            called_by_user_id=called_by,
            confidence=float(item.get("confidence", 0) or 0),
            evidence=str(item.get("evidence", "") or ""),
            now=now,
        )
        canonical = canonical_text(alias)
        cursor.execute(
            """
            UPDATE alias_candidates
            SET status = CASE WHEN user_id = ? THEN 'promoted' ELSE 'conflict' END
            WHERE scope = ? AND canonical_alias = ?
              AND status IN ('pending', 'conflict')
            """,
            (target, scope, canonical),
        )

    def _apply_alias_activity_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        item: Mapping[str, Any],
        now: int,
    ) -> bool:
        """刷新当前批次明确使用过的正式昵称的 last_seen。"""

        target = clean_text(item.get("target_user_id", ""), 128)
        alias = normalize_alias(item.get("alias", ""))
        called_by = clean_text(item.get("called_by_user_id", ""), 128)
        if not target or not alias:
            return False
        canonical = canonical_text(alias)
        row = cursor.execute(
            """
            SELECT id, source, confidence FROM aliases
            WHERE scope = ? AND user_id = ? AND canonical_alias = ?
              AND status = 'active'
            LIMIT 1
            """,
            (scope, target, canonical),
        ).fetchone()
        if row is None:
            return False
        evidence = clean_text(item.get("evidence", ""), 360)
        if called_by:
            self._upsert_alias_cursor(
                cursor,
                scope=scope,
                user_id=target,
                alias=alias,
                source=str(row["source"] or "observed"),
                called_by_user_id=called_by,
                confidence=float(row["confidence"] or 0),
                evidence=evidence,
                now=now,
            )
        else:
            cursor.execute(
                """
                UPDATE aliases
                SET last_seen = ?, occurrences = occurrences + 1,
                    evidence = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, evidence, int(row["id"])),
            )
        return True

    def _apply_alias_candidate_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        item: Mapping[str, Any],
        now: int,
        *,
        ttl_days: int,
        required_confirmations: int,
    ) -> bool:
        target = clean_text(item.get("target_user_id", ""), 128)
        alias = normalize_alias(item.get("alias", ""))
        called_by = clean_text(item.get("called_by_user_id", ""), 128)
        strength = clean_text(item.get("attribution_strength", "weak"), 20)
        if not target or not alias or strength not in {"anchored", "context", "weak"}:
            return False
        canonical = canonical_text(alias)
        existing = cursor.execute(
            """
            SELECT callers_json FROM alias_candidates
            WHERE scope = ? AND user_id = ? AND canonical_alias = ?
            """,
            (scope, target, canonical),
        ).fetchone()
        callers = _load_json(existing["callers_json"], []) if existing else []
        if not isinstance(callers, list):
            callers = []
        callers = [str(value) for value in callers if str(value)]
        if called_by and called_by not in callers:
            callers.append(called_by)
        context_increment = 1 if strength in {"anchored", "context"} else 0
        weak_increment = 1 if strength == "weak" else 0
        cursor.execute(
            """
            INSERT INTO alias_candidates
            (scope, user_id, alias, canonical_alias, callers_json, confidence,
             context_confirmations, weak_mentions, first_seen, last_seen,
             status, evidence, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(scope, user_id, canonical_alias) DO UPDATE SET
              alias=excluded.alias, callers_json=excluded.callers_json,
              confidence=MAX(alias_candidates.confidence, excluded.confidence),
              context_confirmations=alias_candidates.context_confirmations + excluded.context_confirmations,
              weak_mentions=alias_candidates.weak_mentions + excluded.weak_mentions,
              last_seen=excluded.last_seen, status='pending', evidence=excluded.evidence,
              expires_at=excluded.expires_at
            """,
            (
                scope,
                target,
                alias,
                canonical,
                _json(callers),
                float(item.get("confidence", 0) or 0),
                context_increment,
                weak_increment,
                now,
                now,
                clean_text(item.get("evidence", ""), 360),
                now + ttl_days * 86400,
            ),
        )
        formal_targets = {
            str(row["user_id"])
            for row in cursor.execute(
                """
                SELECT user_id FROM aliases
                WHERE scope = ? AND canonical_alias = ? AND status = 'active'
                """,
                (scope, canonical),
            ).fetchall()
        }
        candidate_rows = cursor.execute(
            """
            SELECT * FROM alias_candidates
            WHERE scope = ? AND canonical_alias = ?
              AND status IN ('pending', 'conflict')
            ORDER BY context_confirmations DESC, confidence DESC, last_seen DESC
            """,
            (scope, canonical),
        ).fetchall()
        contextual_targets = {
            str(row["user_id"])
            for row in candidate_rows
            if int(row["context_confirmations"] or 0) > 0
        }
        eligible = [
            row
            for row in candidate_rows
            if int(row["context_confirmations"] or 0) >= required_confirmations
        ]
        conflict = len(contextual_targets) > 1 or bool(
            formal_targets and formal_targets != {target}
        )
        if conflict:
            cursor.execute(
                """
                UPDATE alias_candidates SET status='conflict'
                WHERE scope = ? AND canonical_alias = ?
                  AND status IN ('pending', 'conflict')
                """,
                (scope, canonical),
            )
            return False
        if len(eligible) != 1:
            return False
        winner = eligible[0]
        winner_callers = _load_json(winner["callers_json"], [])
        if not isinstance(winner_callers, list):
            winner_callers = []
        if not winner_callers:
            winner_callers = [called_by] if called_by else []
        for caller in dict.fromkeys(str(value) for value in winner_callers if str(value)):
            self._upsert_alias_cursor(
                cursor,
                scope=scope,
                user_id=str(winner["user_id"]),
                alias=str(winner["alias"]),
                source="observed",
                called_by_user_id=caller,
                confidence=float(winner["confidence"] or 0),
                evidence=str(winner["evidence"] or ""),
                now=now,
            )
        cursor.execute(
            """
            UPDATE alias_candidates
            SET status = CASE WHEN user_id = ? THEN 'promoted' ELSE 'conflict' END
            WHERE scope = ? AND canonical_alias = ?
              AND status IN ('pending', 'conflict')
            """,
            (str(winner["user_id"]), scope, canonical),
        )
        return True

    def _apply_relation_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        item: Mapping[str, Any],
        now: int,
        *,
        temporary_ttl_days: int,
    ) -> None:
        from_user = clean_text(item.get("from_user_id", ""), 128)
        to_user = clean_text(item.get("to_user_id", ""), 128)
        relation_type = clean_text(item.get("relation_type", "其他"), 40)
        value = clean_text(item.get("value", ""), 180)
        if not from_user or not to_user or from_user == to_user or not relation_type or not value:
            return
        canonical = canonical_text(value)
        expires_at = now + temporary_ttl_days * 86400 if temporary_ttl_days else 0
        cursor.execute(
            """
            UPDATE relations SET status='archived'
            WHERE scope = ? AND from_user_id = ? AND to_user_id = ?
              AND relation_type = ? AND status = 'active'
            """,
            (scope, from_user, to_user, relation_type),
        )
        cursor.execute(
            """
            INSERT INTO relations
            (scope, from_user_id, to_user_id, relation_type, value, canonical_value,
             confidence, occurrences, first_seen, last_seen, status, evidence, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'active', ?, ?)
            ON CONFLICT(scope, from_user_id, to_user_id, relation_type, canonical_value) DO UPDATE SET
              value=excluded.value, confidence=MAX(relations.confidence, excluded.confidence),
              occurrences=relations.occurrences + 1, last_seen=excluded.last_seen,
              status='active', evidence=excluded.evidence, expires_at=excluded.expires_at
            """,
            (
                scope,
                from_user,
                to_user,
                relation_type,
                value,
                canonical,
                float(item.get("confidence", 0) or 0),
                now,
                now,
                clean_text(item.get("evidence", ""), 360),
                expires_at,
            ),
        )

    def _apply_observation_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        item: Mapping[str, Any],
        now: int,
        *,
        observation_ttl_days: int,
    ) -> None:
        user_id = clean_text(item.get("user_id", ""), 128)
        field = clean_text(item.get("field", "observations"), 64) or "observations"
        value = clean_text(item.get("value", ""), 180)
        if not user_id or not value:
            return
        expires_at = now + observation_ttl_days * 86400 if observation_ttl_days else 0
        if field == "communication_style":
            # 沟通风格是完整状态而非可无限追加的事件列表。先归档旧状态，
            # 随后的 upsert 会将同文案重新激活，或插入新的替代状态。
            cursor.execute(
                """
                UPDATE observations SET status='archived'
                WHERE scope = ? AND user_id = ? AND field = 'communication_style'
                  AND status = 'active'
                """,
                (scope, user_id),
            )
        cursor.execute(
            """
            INSERT INTO observations
            (scope, user_id, field, value, canonical_value, confidence, first_seen,
             last_seen, evidence, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(scope, user_id, field, canonical_value) DO UPDATE SET
              value=excluded.value, confidence=MAX(observations.confidence, excluded.confidence),
              last_seen=excluded.last_seen, evidence=excluded.evidence,
              expires_at=excluded.expires_at, status='active'
            """,
            (
                scope,
                user_id,
                field,
                value,
                canonical_text(value),
                float(item.get("confidence", 0) or 0),
                now,
                now,
                clean_text(item.get("evidence", ""), 360),
                expires_at,
            ),
        )

    def _enforce_limits_cursor(
        self,
        cursor: sqlite3.Cursor,
        scope: str,
        *,
        max_facts_per_user: int,
        max_aliases_per_user: int,
        max_observations_per_user: int,
        max_relations_per_user: int,
    ) -> None:
        user_rows = cursor.execute(
            "SELECT user_id FROM profiles WHERE scope = ?", (scope,)
        ).fetchall()
        for user_row in user_rows:
            user_id = str(user_row[0])
            self._archive_over_limit(
                cursor,
                "facts",
                "scope = ? AND user_id = ? AND status = 'active'",
                (scope, user_id),
                max_facts_per_user,
            )
            self._archive_over_limit(
                cursor,
                "aliases",
                "scope = ? AND user_id = ? AND status = 'active'",
                (scope, user_id),
                max_aliases_per_user,
            )
            self._archive_over_limit(
                cursor,
                "observations",
                "scope = ? AND user_id = ? AND status = 'active'",
                (scope, user_id),
                max_observations_per_user,
            )
            relation_rows = cursor.execute(
                """
                SELECT id FROM relations WHERE scope = ? AND status = 'active'
                  AND (from_user_id = ? OR to_user_id = ?)
                ORDER BY confidence DESC, occurrences DESC, last_seen DESC
                """,
                (scope, user_id, user_id),
            ).fetchall()
            for row in relation_rows[max_relations_per_user:]:
                cursor.execute("UPDATE relations SET status='archived' WHERE id = ?", (row[0],))

    @staticmethod
    def _archive_over_limit(
        cursor: sqlite3.Cursor,
        table: str,
        where: str,
        params: tuple[Any, ...],
        limit: int,
    ) -> None:
        order = {
            "facts": "confidence DESC, evidence_count DESC, last_seen DESC",
            "aliases": "confidence DESC, occurrences DESC, last_seen DESC",
            "observations": "confidence DESC, last_seen DESC",
        }[table]
        rows = cursor.execute(
            f"SELECT id FROM {table} WHERE {where} ORDER BY {order}", params
        ).fetchall()
        for row in rows[limit:]:
            cursor.execute(f"UPDATE {table} SET status='archived' WHERE id = ?", (row[0],))


def now_hour(timestamp: int) -> int:
    return time.localtime(timestamp).tm_hour
