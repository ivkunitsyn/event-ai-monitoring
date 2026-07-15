from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .utils import canonical_url


@dataclass
class TimeWindow:
    start: datetime
    end: datetime


def _iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_code TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    guid TEXT NOT NULL,
                    title TEXT,
                    link TEXT,
                    canonical_url TEXT,
                    source_title TEXT,
                    description TEXT,
                    published_at TEXT,
                    last_seen_at TEXT,
                    text_clean TEXT,
                    is_relevant INTEGER NOT NULL DEFAULT 1,
                    is_noise INTEGER NOT NULL DEFAULT 0,
                    noise_reason TEXT,
                    group_key TEXT,
                    group_id TEXT,
                    lead_clean TEXT,
                    topic TEXT,
                    signal_type TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_code, guid)
                );
                CREATE INDEX IF NOT EXISTS idx_items_project_pub ON items(project_code, published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_items_project_canon ON items(project_code, canonical_url);

                CREATE TABLE IF NOT EXISTS social_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    chat_id TEXT,
                    message_id TEXT,
                    synthetic_id TEXT NOT NULL,
                    source TEXT,
                    platform TEXT,
                    author_name TEXT,
                    url TEXT,
                    canonical_url TEXT,
                    text_raw TEXT,
                    text_clean TEXT,
                    metrics TEXT,
                    audience INTEGER,
                    published_at TEXT,
                    ingested_at TEXT,
                    project_primary TEXT,
                    project_secondary TEXT,
                    project_rule_match TEXT,
                    project_confidence REAL,
                    is_relevant INTEGER NOT NULL DEFAULT 1,
                    is_noise INTEGER NOT NULL DEFAULT 0,
                    noise_reason TEXT,
                    monitoring_eligible INTEGER NOT NULL DEFAULT 1,
                    excel_eligible INTEGER NOT NULL DEFAULT 1,
                    analytics_eligible INTEGER NOT NULL DEFAULT 1,
                    group_key TEXT,
                    group_id TEXT,
                    lead_clean TEXT,
                    topic TEXT,
                    signal_type TEXT,
                    risk_level TEXT,
                    opportunity_level TEXT,
                    recommended_action TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_type, synthetic_id)
                );
                CREATE INDEX IF NOT EXISTS idx_social_project_pub ON social_items(project_primary, published_at DESC);
                CREATE INDEX IF NOT EXISTS idx_social_project_ing ON social_items(project_primary, ingested_at DESC);
                CREATE INDEX IF NOT EXISTS idx_social_canon_pub ON social_items(canonical_url, published_at DESC);

                CREATE TABLE IF NOT EXISTS grouped_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_code TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    group_key TEXT NOT NULL,
                    topic TEXT,
                    lead_clean TEXT,
                    representative_id TEXT,
                    sources_json TEXT,
                    items_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_code, source_kind, group_key)
                );

                CREATE TABLE IF NOT EXISTS digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_code TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    message_text TEXT,
                    media_count INTEGER,
                    social_count INTEGER,
                    digest_risks_json TEXT,
                    digest_opportunities_json TEXT,
                    digest_overall_assessment TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_code TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    period_start TEXT,
                    period_end TEXT,
                    file_path TEXT,
                    rows_count INTEGER,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def state_get(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM state WHERE key=?", (key,))
            row = await cur.fetchone()
            await cur.close()
            return str(row[0]) if row and row[0] is not None else default

    async def state_set(self, key: str, value: str) -> None:
        now = _iso(datetime.now(timezone.utc))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO state(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
            await db.commit()

    async def state_items_by_prefix(self, prefix: str) -> list[tuple[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT key, value FROM state WHERE key LIKE ? ORDER BY key",
                (f"{prefix}%",),
            )
            rows = await cur.fetchall()
            await cur.close()
        out: list[tuple[str, str]] = []
        for row in rows:
            if not row:
                continue
            out.append((str(row[0] or ""), str(row[1] or "")))
        return out

    async def upsert_media_item(self, item: dict[str, Any]) -> bool:
        now = _iso(datetime.now(timezone.utc))
        can_url = canonical_url(str(item.get("canonical_url") or item.get("link") or ""))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id FROM items WHERE project_code=? AND guid=?",
                (item.get("project_code"), item.get("guid")),
            )
            row = await cur.fetchone()
            await cur.close()
            exists = bool(row)

            await db.execute(
                """
                INSERT INTO items(
                    project_code, feed, guid, title, link, canonical_url, source_title, description,
                    published_at, last_seen_at, text_clean, is_relevant, is_noise, noise_reason,
                    group_key, group_id, lead_clean, topic, signal_type, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_code, guid) DO UPDATE SET
                    title=excluded.title,
                    link=excluded.link,
                    canonical_url=excluded.canonical_url,
                    source_title=excluded.source_title,
                    description=excluded.description,
                    published_at=excluded.published_at,
                    last_seen_at=excluded.last_seen_at,
                    text_clean=excluded.text_clean,
                    is_relevant=excluded.is_relevant,
                    is_noise=excluded.is_noise,
                    noise_reason=excluded.noise_reason,
                    group_key=COALESCE(excluded.group_key, items.group_key),
                    group_id=COALESCE(excluded.group_id, items.group_id),
                    lead_clean=COALESCE(excluded.lead_clean, items.lead_clean),
                    topic=COALESCE(excluded.topic, items.topic),
                    signal_type=COALESCE(excluded.signal_type, items.signal_type),
                    updated_at=excluded.updated_at
                """,
                (
                    item.get("project_code"),
                    item.get("feed") or "rss",
                    item.get("guid") or can_url,
                    item.get("title") or "",
                    item.get("link") or "",
                    can_url,
                    item.get("source_title") or "",
                    item.get("description") or "",
                    _iso(item.get("published_at")),
                    _iso(item.get("last_seen_at") or item.get("published_at")),
                    item.get("text_clean") or "",
                    int(item.get("is_relevant", 1)),
                    int(item.get("is_noise", 0)),
                    item.get("noise_reason") or "",
                    item.get("group_key") or "",
                    item.get("group_id") or "",
                    item.get("lead_clean") or "",
                    item.get("topic") or "",
                    item.get("signal_type") or "",
                    now,
                    now,
                ),
            )
            await db.commit()
            return not exists

    async def upsert_social_item(self, item: dict[str, Any]) -> bool:
        now = _iso(datetime.now(timezone.utc))
        can_url = canonical_url(str(item.get("canonical_url") or item.get("url") or ""))
        project_secondary = item.get("project_secondary") or []
        if not isinstance(project_secondary, list):
            project_secondary = []
        metrics = item.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id FROM social_items WHERE source_type=? AND synthetic_id=?",
                (item.get("source_type"), item.get("synthetic_id")),
            )
            row = await cur.fetchone()
            await cur.close()
            exists = bool(row)

            await db.execute(
                """
                INSERT INTO social_items(
                    source_type, chat_id, message_id, synthetic_id, source, platform, author_name,
                    url, canonical_url, text_raw, text_clean, metrics, audience,
                    published_at, ingested_at,
                    project_primary, project_secondary, project_rule_match, project_confidence,
                    is_relevant, is_noise, noise_reason,
                    monitoring_eligible, excel_eligible, analytics_eligible,
                    group_key, group_id, lead_clean, topic, signal_type,
                    risk_level, opportunity_level, recommended_action,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_type, synthetic_id) DO UPDATE SET
                    source=excluded.source,
                    platform=excluded.platform,
                    author_name=excluded.author_name,
                    url=excluded.url,
                    canonical_url=excluded.canonical_url,
                    text_raw=excluded.text_raw,
                    text_clean=excluded.text_clean,
                    metrics=excluded.metrics,
                    audience=excluded.audience,
                    published_at=excluded.published_at,
                    ingested_at=excluded.ingested_at,
                    project_primary=excluded.project_primary,
                    project_secondary=excluded.project_secondary,
                    project_rule_match=excluded.project_rule_match,
                    project_confidence=excluded.project_confidence,
                    is_relevant=excluded.is_relevant,
                    is_noise=excluded.is_noise,
                    noise_reason=excluded.noise_reason,
                    monitoring_eligible=excluded.monitoring_eligible,
                    excel_eligible=excluded.excel_eligible,
                    analytics_eligible=excluded.analytics_eligible,
                    group_key=COALESCE(excluded.group_key, social_items.group_key),
                    group_id=COALESCE(excluded.group_id, social_items.group_id),
                    lead_clean=COALESCE(excluded.lead_clean, social_items.lead_clean),
                    topic=COALESCE(excluded.topic, social_items.topic),
                    signal_type=COALESCE(excluded.signal_type, social_items.signal_type),
                    risk_level=COALESCE(excluded.risk_level, social_items.risk_level),
                    opportunity_level=COALESCE(excluded.opportunity_level, social_items.opportunity_level),
                    recommended_action=COALESCE(excluded.recommended_action, social_items.recommended_action),
                    updated_at=excluded.updated_at
                """,
                (
                    item.get("source_type") or "unknown",
                    str(item.get("chat_id") or ""),
                    str(item.get("message_id") or ""),
                    item.get("synthetic_id") or "",
                    item.get("source") or "",
                    item.get("platform") or "",
                    item.get("author_name") or "",
                    item.get("url") or "",
                    can_url,
                    item.get("text_raw") or "",
                    item.get("text_clean") or "",
                    json.dumps(metrics, ensure_ascii=False),
                    int(item.get("audience") or 0),
                    _iso(item.get("published_at")),
                    _iso(item.get("ingested_at") or datetime.now(timezone.utc)),
                    item.get("project_primary") or "",
                    json.dumps(project_secondary, ensure_ascii=False),
                    item.get("project_rule_match") or "",
                    float(item.get("project_confidence") or 0.0),
                    int(item.get("is_relevant", 1)),
                    int(item.get("is_noise", 0)),
                    item.get("noise_reason") or "",
                    int(item.get("monitoring_eligible", 1)),
                    int(item.get("excel_eligible", 1)),
                    int(item.get("analytics_eligible", 1)),
                    item.get("group_key") or "",
                    item.get("group_id") or "",
                    item.get("lead_clean") or "",
                    item.get("topic") or "",
                    item.get("signal_type") or "",
                    item.get("risk_level") or "",
                    item.get("opportunity_level") or "",
                    item.get("recommended_action") or "",
                    now,
                    now,
                ),
            )
            await db.commit()
            return not exists

    async def fetch_media_items(self, project_code: str, window: TimeWindow, limit: int = 1000) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT * FROM items
                WHERE project_code=?
                  AND is_relevant=1
                  AND is_noise=0
                  AND published_at>=?
                  AND published_at<=?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (project_code, _iso(window.start), _iso(window.end), limit),
            )
            rows = await cur.fetchall()
            cols = [x[0] for x in cur.description]
            await cur.close()
        return [dict(zip(cols, r)) for r in rows]

    async def fetch_social_items(
        self,
        project_code: str,
        window: TimeWindow,
        limit: int = 5000,
        monitoring_only: bool = True,
    ) -> list[dict[str, Any]]:
        cond = "AND monitoring_eligible=1" if monitoring_only else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT * FROM social_items
                WHERE project_primary=?
                  AND is_relevant=1
                  AND is_noise=0
                  {cond}
                  AND published_at>=?
                  AND published_at<=?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (project_code, _iso(window.start), _iso(window.end), limit),
            )
            rows = await cur.fetchall()
            cols = [x[0] for x in cur.description]
            await cur.close()
        result = [dict(zip(cols, r)) for r in rows]
        for item in result:
            try:
                item["metrics"] = json.loads(item.get("metrics") or "{}")
            except Exception:
                item["metrics"] = {}
            try:
                item["project_secondary"] = json.loads(item.get("project_secondary") or "[]")
            except Exception:
                item["project_secondary"] = []
        return result

    async def count_media_last_24h(self, project_code: str, now: datetime) -> int:
        start = now - timedelta(days=1)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT COUNT(*) FROM items
                WHERE project_code=? AND is_relevant=1 AND is_noise=0
                  AND published_at>=? AND published_at<=?
                """,
                (project_code, _iso(start), _iso(now)),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    async def count_social_last_24h(self, project_code: str, now: datetime) -> int:
        start = now - timedelta(days=1)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT COUNT(*) FROM social_items
                WHERE project_primary=? AND is_relevant=1 AND is_noise=0
                  AND published_at>=? AND published_at<=?
                """,
                (project_code, _iso(start), _iso(now)),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row else 0

    async def add_digest_record(
        self,
        project_code: str,
        window: TimeWindow,
        message_text: str,
        media_count: int,
        social_count: int,
        risks: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        overall: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO digests(
                    project_code, window_start, window_end, message_text,
                    media_count, social_count, digest_risks_json,
                    digest_opportunities_json, digest_overall_assessment, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    project_code,
                    _iso(window.start),
                    _iso(window.end),
                    message_text,
                    media_count,
                    social_count,
                    json.dumps(risks, ensure_ascii=False),
                    json.dumps(opportunities, ensure_ascii=False),
                    overall,
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            await db.commit()

    async def add_export_record(
        self,
        project_code: str,
        kind: str,
        period_start: datetime,
        period_end: datetime,
        file_path: str,
        rows_count: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO exports(project_code, kind, period_start, period_end, file_path, rows_count, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    project_code,
                    kind,
                    _iso(period_start),
                    _iso(period_end),
                    file_path,
                    rows_count,
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            await db.commit()

    async def cleanup_social_technical_duplicates(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM social_items")
            before_row = await cur.fetchone()
            await cur.close()
            before = int(before_row[0]) if before_row else 0

            # 1) exact technical duplicates from same source/message/url/time/text
            await db.execute(
                """
                DELETE FROM social_items
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM social_items
                    GROUP BY
                        source_type,
                        COALESCE(message_id, ''),
                        COALESCE(canonical_url, ''),
                        COALESCE(published_at, ''),
                        COALESCE(author_name, ''),
                        COALESCE(text_clean, '')
                )
                """
            )
            # 2) normalize duplicate imap rows by stable message key
            await db.execute(
                """
                DELETE FROM social_items
                WHERE source_type='imap'
                  AND id NOT IN (
                    SELECT MAX(id)
                    FROM social_items
                    WHERE source_type='imap'
                    GROUP BY COALESCE(message_id, '')
                  )
                """
            )

            await db.commit()
            cur = await db.execute("SELECT COUNT(*) FROM social_items")
            after_row = await cur.fetchone()
            await cur.close()
            after = int(after_row[0]) if after_row else 0
            return max(0, before - after)
