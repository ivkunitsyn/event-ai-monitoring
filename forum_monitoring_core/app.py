from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .analytics import analyze_topics
from .config import AppConfig, ProjectConfig
from .export_excel import build_project_excel
from .grouping import GroupedTopic, group_items
from .ingest import fetch_imap_rows, poll_all_rss, read_social_export
from .leads import LeadEditor
from .ranking import media_score, social_score
from .render import render_digest
from .routing import route_social_item
from .storage import Storage, TimeWindow
from .utils import collapse_spaces, extract_urls, normalize_for_match, parse_any_dt, pick_social_post_url


_PUTESH_FOCUS_RE = re.compile(
    r"(?iu)(международн\w*\s+туристическ\w*\s+форум\w*\s+путешествуй|форум\w*\s+путешествуй|мтф\s+путешествуй)"
)
_PUTESH_SPAM_RE = re.compile(
    r"(?iu)(поиск\s+дешев\w*\s+авиабилет\w*|горящ\w+\s+тур\w*|бронировани\w+\s+отел\w*|"
    r"youtube|instagram|inslagram|душанбеводоканал|черешн\w+\s+в\s+мелитопол)"
)
_NON_MEDIA_SOURCE_RE = re.compile(r"(?iu)(посольств|консульств|канцелярия\s+россии|ambassade|embassy)")
_KIF_FULL_RE = re.compile(r"(?iu)кавказск\w+\s+инвестиц\w+\s+форум\w+")
_KIF_ABBR_CTX_RE = re.compile(r"(?iu)\bкиф\b.*\b(кавказ|инвестиц|форум)\b|\b(кавказ|инвестиц|форум)\b.*\bкиф\b")
_VNOT_FULL_RE = re.compile(r"(?iu)всероссийск\w+\s+недел\w+\s+охран\w+\s+труд\w+")
_VNOT_ABBR_CTX_RE = re.compile(r"(?iu)\bвнот\b.*\b(охран\w+\s+труд\w+|недел\w+\s+охран\w+\s+труд\w+)\b|\b(охран\w+\s+труд\w+|недел\w+\s+охран\w+\s+труд\w+)\b.*\bвнот\b")
_REN_FULL_RE = re.compile(r"(?iu)российск\w+\s+энергетическ\w+\s+недел\w+|russian\s+energy\s+week")
_REN_ABBR_CTX_RE = re.compile(r"(?iu)\bрэн\b.*\b(энергетическ|недел)\b|\b(энергетическ|недел)\b.*\bрэн\b")
_RKF_FULL_RE = re.compile(r"(?iu)российск\w+\s+космическ\w+\s+форум\w+")
_RKF_ABBR_CTX_RE = re.compile(r"(?iu)\bркф\b.*\b(космическ|форум)\b|\b(космическ|форум)\b.*\bркф\b")


def _best_item_url(item: dict[str, Any]) -> str:
    for key in ("canonical_url", "url", "link"):
        v = str(item.get(key) or "").strip()
        if v:
            return v
    return ""


def _is_public_url(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    try:
        host = (urlparse(u).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    if host in {"pr.mlg.ru", "www.pr.mlg.ru"}:
        return False
    return True


def _lead_matches_project(project: ProjectConfig, lead: str) -> bool:
    text = normalize_for_match(lead or "")
    if not text:
        return False
    if project.code == "puteshestvuy":
        return bool(_PUTESH_FOCUS_RE.search(text))
    if any(normalize_for_match(m) in text for m in project.strict_markers if m):
        return True
    return any(normalize_for_match(m) in text for m in project.markers if m)


@dataclass
class IngestStats:
    rss_inserted: int = 0
    rss_seen: int = 0
    social_inserted: int = 0
    social_seen: int = 0


class MonitoringEngine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.storage = Storage(cfg.db_path.as_posix())
        self.tz = ZoneInfo(cfg.timezone)
        self.leads = LeadEditor(
            enabled=cfg.use_openai,
            model_simple=cfg.model_simple,
            model_complex=cfg.model_complex,
            api_key=cfg.openai_api_key,
            proxy=cfg.openai_proxy,
            base_url=cfg.openai_base_url,
            http_referer=cfg.openai_http_referer,
            x_title=cfg.openai_x_title,
        )

    async def init(self) -> None:
        await self.storage.init()

    async def ingest_rss(self) -> IngestStats:
        stats = IngestStats()
        feeds = await poll_all_rss(self.cfg)
        for code, rows in feeds.items():
            for row in rows:
                ok = await self.storage.upsert_media_item(row)
                stats.rss_seen += 1
                if ok:
                    stats.rss_inserted += 1
            latest_dt = ""
            if rows:
                dt = max(parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc) for x in rows)
                latest_dt = dt.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[rss] project={code} docs={len(rows)} inserted={stats.rss_inserted} latest_pub={latest_dt}")
        return stats

    async def ingest_social_imap(self) -> IngestStats:
        stats = IngestStats()
        rows: list[dict[str, Any]] = []
        max_uid = 0
        imap_stats: dict[str, int] = {}
        raw_boxes = re.split(r"[,\n;]+", str(self.cfg.imap_mailbox or "INBOX"))
        mailboxes = [x.strip() for x in raw_boxes if x.strip()] or ["INBOX"]

        if self.cfg.imap_enable and self.cfg.imap_host and self.cfg.imap_login and self.cfg.imap_password:
            agg = {"scanned": 0, "new": 0, "from_sender": 0, "blocks": 0, "inserted": 0}
            for mailbox in mailboxes:
                state_key = (
                    f"imap:last_uid:{mailbox.lower()}:"
                    + ",".join(sorted(self.cfg.imap_sender_allowlist))
                )
                last_uid_raw = await self.storage.state_get(state_key, "0")
                try:
                    last_uid = int(last_uid_raw)
                except Exception:
                    last_uid = 0

                one_rows: list[dict[str, Any]] = []
                one_max_uid = last_uid
                one_stats: dict[str, int] = {}
                try:
                    one_rows, one_max_uid, one_stats = fetch_imap_rows(
                        self.cfg,
                        last_uid=last_uid,
                        force_full_reread=self.cfg.imap_force_full_reread,
                        mailbox=mailbox,
                    )
                    if one_max_uid > last_uid:
                        await self.storage.state_set(state_key, str(one_max_uid))
                except Exception as exc:
                    print(f"[imap] error mailbox={mailbox}: {type(exc).__name__}: {exc}")
                    if self.cfg.imap_force_full_reread:
                        try:
                            print(f"[imap] retry incremental mode mailbox={mailbox}")
                            one_rows, one_max_uid, one_stats = fetch_imap_rows(
                                self.cfg,
                                last_uid=last_uid,
                                force_full_reread=False,
                                mailbox=mailbox,
                            )
                            if one_max_uid > last_uid:
                                await self.storage.state_set(state_key, str(one_max_uid))
                        except Exception as exc2:
                            print(f"[imap] incremental retry failed mailbox={mailbox}: {type(exc2).__name__}: {exc2}")
                            one_rows, one_max_uid, one_stats = [], last_uid, {}
                    else:
                        one_rows, one_max_uid, one_stats = [], last_uid, {}
                rows.extend(one_rows)
                max_uid = max(max_uid, one_max_uid)
                for k in agg:
                    agg[k] += int(one_stats.get(k, 0))
                print(
                    f"[imap] mailbox={mailbox}"
                    f" scanned={int(one_stats.get('scanned',0))}"
                    f" new={int(one_stats.get('new',0))}"
                    f" from_sender={int(one_stats.get('from_sender',0))}"
                    f" blocks={int(one_stats.get('blocks',0))}"
                    f" inserted={int(one_stats.get('inserted',0))}"
                    f" last_uid={one_max_uid}"
                )
            imap_stats = agg
        else:
            if not self.cfg.imap_enable:
                print("[imap] disabled")
            else:
                print("[imap] missing credentials")

        export_rows = 0
        for path in self.cfg.social_export_paths:
            data = read_social_export(path)
            export_rows += len(data)
            rows.extend(data)

        for row in rows:
            routing = route_social_item(row, self.cfg.projects)
            row["project_primary"] = routing.project_primary
            row["project_secondary"] = routing.project_secondary
            row["project_rule_match"] = routing.rule_match
            row["project_confidence"] = routing.confidence
            row["is_noise"] = int(routing.is_noise)
            row["is_relevant"] = int(not routing.is_noise)
            row["noise_reason"] = routing.noise_reason
            row["monitoring_eligible"] = int(bool(row.get("url")))
            row["excel_eligible"] = 1
            row["analytics_eligible"] = int(not routing.is_noise)
            if not routing.project_primary:
                continue
            inserted = await self.storage.upsert_social_item(row)
            stats.social_seen += 1
            if inserted:
                stats.social_inserted += 1

        removed_dups = await self.storage.cleanup_social_technical_duplicates()

        print(
            "[imap]"
            f" scanned={imap_stats.get('scanned',0)}"
            f" new={imap_stats.get('new',0)}"
            f" from_sender={imap_stats.get('from_sender',0)}"
            f" blocks={imap_stats.get('blocks',0)}"
            f" export_rows={export_rows}"
            f" accepted={stats.social_seen}"
            f" inserted={stats.social_inserted}"
            f" dedup_removed={removed_dups}"
            f" last_uid={max_uid}"
        )
        return stats

    def _pick_groups_time_first(self, groups: list[GroupedTopic], now: datetime, limit: int) -> list[GroupedTopic]:
        day_start = now - timedelta(days=1)

        def _key(g: GroupedTopic) -> tuple[datetime, float]:
            dt = parse_any_dt(g.representative.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)
            score = float(g.representative.get("_score", 0.0))
            return dt, score

        recent = [g for g in groups if (parse_any_dt(g.representative.get("published_at")) or now) >= day_start]
        older = [g for g in groups if (parse_any_dt(g.representative.get("published_at")) or now) < day_start]

        recent.sort(key=_key, reverse=True)
        older.sort(key=_key, reverse=True)
        out = recent[:limit]
        if len(out) < limit:
            out.extend(older[: max(0, limit - len(out))])
        return out[:limit]

    def _is_project_relevant(self, project: ProjectConfig, item: dict[str, Any], source_kind: str) -> bool:
        text = normalize_for_match(
            " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("description") or ""),
                    str(item.get("text_clean") or ""),
                    str(item.get("text_raw") or ""),
                    str(item.get("source_title") or ""),
                ]
            )
        )
        if not text:
            return False

        # Для СМИ используем проектные RSS как первичный фильтр.
        # Дополнительно режем только явный антимусор и записи без публичной ссылки.
        if source_kind == "media":
            for anti in project.anti_markers:
                anti_norm = normalize_for_match(anti)
                if anti_norm and anti_norm in text:
                    return False
            if project.code == "puteshestvuy" and _PUTESH_SPAM_RE.search(text):
                return False
            return _is_public_url(_best_item_url(item))

        strict_hit = any(normalize_for_match(m) in text for m in project.strict_markers if m)
        marker_hit = any(normalize_for_match(m) in text for m in project.markers if m)
        if not strict_hit and not marker_hit:
            return False

        # Ключевые проектные связки: полное название или аббревиатура + тематический контекст.
        if project.code == "kif":
            if not (_KIF_FULL_RE.search(text) or _KIF_ABBR_CTX_RE.search(text)):
                return False
        if project.code == "vnot":
            if not (_VNOT_FULL_RE.search(text) or _VNOT_ABBR_CTX_RE.search(text)):
                return False
        if project.code == "ren":
            if not (_REN_FULL_RE.search(text) or _REN_ABBR_CTX_RE.search(text)):
                return False
        if project.code == "rkf":
            if not (_RKF_FULL_RE.search(text) or _RKF_ABBR_CTX_RE.search(text)):
                return False
        if project.code == "puteshestvuy":
            # Для «Путешествуй» берем только форумный контекст "форум + путешествуй".
            m = _PUTESH_FOCUS_RE.search(text)
            if not m:
                return False
            # Если форум упомянут далеко в конце, обычно это нерелевантный шум.
            if m.start() > 180:
                return False
            if _PUTESH_SPAM_RE.search(text):
                return False
            src = normalize_for_match(str(item.get("source_title") or ""))
            if _NON_MEDIA_SOURCE_RE.search(src):
                return False

        for anti in project.anti_markers:
            anti_norm = normalize_for_match(anti)
            if anti_norm and anti_norm in text:
                return False

        return True

    def _is_social_quality_ok(self, item: dict[str, Any]) -> bool:
        raw = collapse_spaces(
            " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("description") or ""),
                    str(item.get("text_clean") or ""),
                    str(item.get("text_raw") or ""),
                ]
            )
        )
        raw_l = raw.lower()
        if not raw or len(raw) < 40:
            return False
        raw_norm = normalize_for_match(raw)
        if any(x in raw for x in ("ð", "Ð", "Ñ", "â", "Ã", "Â", "�")):
            return False
        if any(x in raw_l for x in ("поиск дешевых авиабилетов", "горящие туры", "бронирование отелей")):
            return False
        if any(
            x in raw_norm
            for x in (
                "поиск дешевых авиабилетов",
                "горящие туры",
                "бронирование отелей",
                "youtube",
                "instagram",
                "inslagram",
                "последние новости г2",
                "душанбеводоканал",
                "черешню в мелитополе",
            )
        ):
            return False
        if raw_l.startswith("..."):
            return False
        if "t.me/niatkhovar" in raw_l:
            return False
        if "последние новости г2" in raw_l:
            return False
        if ("youtube.com" in raw_l or "youtube .com" in raw_l) and ("inslagram" in raw_l or "instagram" in raw_l):
            return False
        # Минимальная доля кириллицы для русскоязычного мониторинга.
        letters = [ch for ch in raw if ch.isalpha()]
        if letters:
            cyr = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
            if cyr / max(1, len(letters)) < 0.35:
                return False
        return True

    @staticmethod
    def _recover_social_url(item: dict[str, Any]) -> str:
        existing = str(item.get("canonical_url") or item.get("url") or "").strip()
        if existing:
            return existing
        raw = " ".join(
            [
                str(item.get("text_raw") or ""),
                str(item.get("text_clean") or ""),
                str(item.get("description") or ""),
                str(item.get("title") or ""),
            ]
        )
        post_url = pick_social_post_url(raw)
        if post_url:
            return post_url
        urls = extract_urls(raw)
        return urls[0].strip() if urls else ""

    def _primary_monitoring_window(self, now_utc: datetime, now_local: datetime) -> TimeWindow:
        # Плановый утренний выпуск в понедельник покрывает окно с пятницы 09:00 Мск.
        if now_local.weekday() == 0 and now_local.hour == 9:
            end_local = now_local
            start_local = end_local.replace(hour=9, minute=0, second=0, microsecond=0) - timedelta(days=3)
            return TimeWindow(start=start_local.astimezone(timezone.utc), end=now_utc)
        return TimeWindow(start=now_utc - timedelta(days=1), end=now_utc)

    @staticmethod
    def _previous_24h_window(window: TimeWindow) -> TimeWindow:
        return TimeWindow(start=window.start - timedelta(days=1), end=window.start)

    async def _daily_media_count(self, project: ProjectConfig, now_utc: datetime) -> int:
        window = TimeWindow(start=now_utc - timedelta(days=1), end=now_utc)
        items = await self.storage.fetch_media_items(project.code, window, limit=5000)
        items = [x for x in items if self._is_project_relevant(project, x, source_kind="media")]
        items = [x for x in items if _is_public_url(_best_item_url(x))]
        return len(items)

    async def _daily_social_count(self, project: ProjectConfig, now_utc: datetime) -> int:
        window = TimeWindow(start=now_utc - timedelta(days=1), end=now_utc)
        items = await self.storage.fetch_social_items(project.code, window, limit=20000, monitoring_only=False)
        items = [x for x in items if self._is_project_relevant(project, x, source_kind="social")]
        return len(items)

    async def _prepare_media_topics(self, project: ProjectConfig, window: TimeWindow, now_utc: datetime) -> list[GroupedTopic]:
        items = await self.storage.fetch_media_items(project.code, window, limit=3000)
        items = [x for x in items if self._is_project_relevant(project, x, source_kind="media")]
        items = [x for x in items if _is_public_url(_best_item_url(x))]
        for item in items:
            item["_score"] = media_score(item, now_utc)
            existing_lead = str(item.get("lead_clean") or "")
            if not existing_lead or self.leads.needs_rewrite(existing_lead):
                item["lead_clean"] = self.leads.make_lead(item, source_kind="media", allow_openai=False)
        media_threshold = 80 if project.code == "puteshestvuy" else 86
        groups = group_items(items, source_kind="media", threshold=media_threshold)
        picked = self._pick_groups_time_first(groups, now_utc, limit=5)
        for topic in picked:
            fresh = self.leads.make_lead(
                topic.representative,
                source_kind="media",
                allow_openai=True,
            )
            if fresh:
                topic.representative["lead_clean"] = fresh
        return picked

    async def _prepare_social_topics(self, project: ProjectConfig, window: TimeWindow, now_utc: datetime) -> list[GroupedTopic]:
        items = await self.storage.fetch_social_items(project.code, window, limit=5000, monitoring_only=False)
        items = [x for x in items if self._is_project_relevant(project, x, source_kind="social")]
        items = [x for x in items if self._is_social_quality_ok(x)]
        for item in items:
            item["_score"] = social_score(item, now_utc)
            existing_lead = str(item.get("lead_clean") or "")
            if not existing_lead or self.leads.needs_rewrite(existing_lead):
                item["lead_clean"] = self.leads.make_lead(item, source_kind="social", allow_openai=False)
            # в мониторинг берем только постовые ссылки
            rec_url = self._recover_social_url(item)
            if rec_url and not item.get("url"):
                item["url"] = rec_url
            if rec_url and not item.get("canonical_url"):
                item["canonical_url"] = rec_url

        items = [x for x in items if x.get("canonical_url")]
        social_threshold = 78 if project.code == "puteshestvuy" else 88
        groups = group_items(items, source_kind="social", threshold=social_threshold)
        candidate_pool = self._pick_groups_time_first(groups, now_utc, limit=30)
        picked: list[GroupedTopic] = []
        for topic in candidate_pool:
            fresh = self.leads.make_lead(
                topic.representative,
                source_kind="social",
                allow_openai=True,
            )
            if fresh:
                topic.representative["lead_clean"] = fresh
            if not _lead_matches_project(project, str(topic.representative.get("lead_clean") or "")):
                continue
            picked.append(topic)
            if len(picked) >= 5:
                break
        return picked[:5]

    async def build_digest(self, project_code: str, now_utc: datetime | None = None) -> str:
        project = self.cfg.projects[project_code]
        now_utc = now_utc or datetime.now(timezone.utc)
        now_local = now_utc.astimezone(self.tz)
        primary_window = self._primary_monitoring_window(now_utc, now_local)

        media_window = primary_window
        media_topics = await self._prepare_media_topics(project, media_window, now_utc)
        media_fallback = False
        if not media_topics:
            media_window = self._previous_24h_window(primary_window)
            media_topics = await self._prepare_media_topics(project, media_window, now_utc)
            media_fallback = True

        social_window = primary_window
        social_topics = await self._prepare_social_topics(project, social_window, now_utc)
        social_fallback = False
        if not social_topics:
            social_window = self._previous_24h_window(primary_window)
            social_topics = await self._prepare_social_topics(project, social_window, now_utc)
            social_fallback = True

        media_daily = await self._daily_media_count(project, now_utc)
        social_daily = await self._daily_social_count(project, now_utc)
        analytics = analyze_topics(
            [*media_topics, *social_topics],
            media_daily_count=media_daily,
            social_daily_count=social_daily,
            project_name=project.name,
            use_openai=self.cfg.use_openai,
            openai_api_key=self.cfg.openai_api_key,
            openai_proxy=self.cfg.openai_proxy,
            openai_model=self.cfg.model_analytics,
            openai_base_url=self.cfg.openai_base_url,
            openai_http_referer=self.cfg.openai_http_referer,
            openai_x_title=self.cfg.openai_x_title,
        )

        message = render_digest(
            project=project,
            now_local=now_local,
            media_daily_count=media_daily,
            social_daily_count=social_daily,
            media_topics=media_topics,
            social_topics=social_topics,
            media_show_dates=media_fallback,
            social_show_dates=social_fallback,
            analytics=analytics,
        )

        window = TimeWindow(
            start=min(media_window.start, social_window.start),
            end=max(media_window.end, social_window.end),
        )
        await self.storage.add_digest_record(
            project_code=project.code,
            window=window,
            message_text=message,
            media_count=len(media_topics),
            social_count=len(social_topics),
            risks=analytics.get("risks", []),
            opportunities=analytics.get("opportunities", []),
            overall=analytics.get("overall_assessment", "neutral"),
        )
        return message

    async def build_excel(self, project_code: str, now_utc: datetime | None = None) -> Path:
        now_utc = now_utc or datetime.now(timezone.utc)
        start = now_utc - timedelta(days=7)
        return await self.build_excel_for_period(project_code, start_utc=start, end_utc=now_utc, now_utc=now_utc)

    async def build_excel_for_period(
        self,
        project_code: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
        now_utc: datetime | None = None,
    ) -> Path:
        project = self.cfg.projects[project_code]
        now_utc = now_utc or datetime.now(timezone.utc)
        window = TimeWindow(start=start_utc, end=end_utc)

        media = await self.storage.fetch_media_items(project.code, window, limit=20000)
        social = await self.storage.fetch_social_items(project.code, window, limit=30000, monitoring_only=False)
        media = [x for x in media if self._is_project_relevant(project, x, source_kind="media")]
        media = [x for x in media if _is_public_url(_best_item_url(x))]
        # Для Excel сохраняем максимально полный массив по проекту:
        # проект уже проставлен на этапе routing, здесь убираем только явный noise.
        social = [x for x in social if int(x.get("is_relevant", 1)) == 1 and int(x.get("is_noise", 0)) == 0]
        for item in social:
            rec_url = self._recover_social_url(item)
            if rec_url and not item.get("url"):
                item["url"] = rec_url
            if rec_url and not item.get("canonical_url"):
                item["canonical_url"] = rec_url
        # В выгрузке оставляем только записи, где удалось восстановить ссылку.
        social = [x for x in social if str(x.get("canonical_url") or x.get("url") or "").strip()]

        ts = now_utc.astimezone(self.tz).strftime("%Y%m%d_%H%M")
        out = self.cfg.reports_dir / f"{project.code}_monitoring_{ts}.xlsx"
        build_project_excel(project.name, media, social, out)

        await self.storage.add_export_record(
            project_code=project.code,
            kind="weekly_excel",
            period_start=start_utc,
            period_end=end_utc,
            file_path=out.as_posix(),
            rows_count=len(media) + len(social),
        )
        return out

    async def run_ingest_once(self) -> IngestStats:
        rss_stats = await self.ingest_rss()
        social_stats = await self.ingest_social_imap()
        return IngestStats(
            rss_inserted=rss_stats.rss_inserted,
            rss_seen=rss_stats.rss_seen,
            social_inserted=social_stats.social_inserted,
            social_seen=social_stats.social_seen,
        )

    async def run_project_once(self, project_code: str) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        digest = await self.build_digest(project_code, now_utc)
        excel = await self.build_excel(project_code, now_utc)
        return {
            "project": project_code,
            "digest": digest,
            "excel_path": excel.as_posix(),
        }
