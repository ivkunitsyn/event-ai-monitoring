from __future__ import annotations

import html
import re
from datetime import datetime

from .config import ProjectConfig
from .grouping import GroupedTopic
from .utils import parse_any_dt, short_date, to_sentence

_FORUM_SENTENCE_RE = re.compile(
    r"(?iu)\b(форум|киф|внот|рэн|путешествуй|космическ|энергетическ|охраны труда)\b"
)


def _clean_source_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "Источник"
    s = re.sub(r"\(\s*https?://[^)]+\)", " ", s, flags=re.I)
    s = re.sub(r"(?iu)\bв\s+блоге\b", " ", s)
    s = re.sub(r"\s*\(\s*(?:https?://)?[a-z0-9.-]+\.[a-z]{2,}(?:/[^)]*)?\s*\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" -:;,.")
    s = re.sub(r"(?iu)^(.{5,90}?)\s+\1$", r"\1", s)
    return s or "Источник"


def _greeting(now_local: datetime) -> str:
    h = now_local.hour
    if 5 <= h < 12:
        return "Коллеги, доброе утро."
    if 12 <= h < 18:
        return "Коллеги, добрый день."
    return "Коллеги, добрый вечер."


def _project_display_name(project: ProjectConfig) -> str:
    if project.code == "puteshestvuy":
        return "Путешествуй"
    return project.name


def _topic_url(topic: GroupedTopic) -> str:
    rep = topic.representative or {}
    for key in ("canonical_url", "url", "link"):
        v = str(rep.get(key) or "").strip()
        if v:
            return v
    for src in topic.sources:
        v = str((src or {}).get("url") or "").strip()
        if v:
            return v
    return ""


def _source_links(topic: GroupedTopic, *, show_date: bool, max_sources: int = 4) -> str:
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    topic_url = _topic_url(topic)
    for src in topic.sources:
        if not isinstance(src, dict):
            continue
        name = _clean_source_name(str(src.get("name") or "").strip())
        url = str(src.get("url") or "").strip() or topic_url
        if not name:
            continue
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    if not pairs:
        rep = topic.representative or {}
        fallback_name = _clean_source_name(str(
            rep.get("source_title") or rep.get("author_name") or rep.get("source") or "Источник"
        ).strip())
        pairs.append((fallback_name or "Источник", topic_url))

    chunks: list[str] = []
    for name, url in pairs[:max_sources]:
        safe_name = html.escape(name)
        if url:
            safe_url = html.escape(url, quote=True)
            chunks.append(f'<a href="{safe_url}">{safe_name}</a>')
        else:
            chunks.append(safe_name)
    if len(pairs) > max_sources:
        chunks.append("и др.")

    if show_date:
        pub = parse_any_dt(topic.representative.get("published_at"))
        dt_label = short_date(pub)
        if dt_label:
            chunks.append(dt_label)
    return ", ".join(chunks)


def _normalize_display_lead(lead: str) -> str:
    s = (lead or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\u0080-\u009f]", " ", s)
    s = re.sub(r"[ðÐÑâÃÂ]", " ", s)
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё«»\"'().,!?;:—–/%+\s-]", " ", s)
    s = re.sub(r"^[^A-Za-zА-Яа-яЁё0-9«\"]+", "", s)
    s = re.sub(r'\s*"\s*([^"]+?)\s*"\s*', r" «\1» ", s)
    s = re.sub(r"«[\s\u00A0\u2009\u202F]+", "«", s)
    s = re.sub(r"[\s\u00A0\u2009\u202F]+»", "»", s)
    s = re.sub(r"![\s\u00A0\u2009\u202F]+»", "!»", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\s+\.", ".", s)
    s = re.sub(r"(?:\.{3,}|…)+", ". ", s)
    s = re.sub(r"(?iu)\bамит\s*«?ховар»?\b", " ", s)
    s = re.sub(r"(?iu)\bстатья\b", " ", s)
    s = re.sub(r"(?iu)\bновости\s+душанбе,\s*события\s+таджикистана\s+сегодня\b", " ", s)
    s = re.sub(
        r"(?iu)\b(международн\w*\s+туристическ\w*\s+форум\w*\s+«?путешествуй!?»?\s+пройд[её]т\s+в\s+москве)\b.*$",
        r"\1",
        s,
    )
    s = re.sub(r"(?iu)\bв\s+г\b(?:\s*[.,:;!?…])?\s*$", "", s).strip(" ,;:-")
    s = re.sub(r"\s{2,}", " ", s).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", s) if p.strip()]
    for p in parts:
        if _FORUM_SENTENCE_RE.search(p):
            s = p
            break
    # Срезаем явно оборванные хвосты: "пр.", "г." и т.п., но не выкидываем весь лид.
    if re.search(r"(?iu)\b[а-яa-z]{1,2}\.$", s):
        s = re.sub(r"(?iu)\s*\b[а-яa-z]{1,2}\.$", "", s).strip(" ,;:-")
    if not s:
        return ""
    s = to_sentence(s, max_len=220)
    s = re.sub(r"!\s+»", "!»", s)
    s = re.sub(r"[\s\u00A0\u2009\u202F]+»", "»", s)
    return s


def _topic_line(topic: GroupedTopic, *, show_date: bool) -> str:
    lead_raw = _normalize_display_lead(str(topic.representative.get("lead_clean") or "").strip())
    if not lead_raw:
        lead_raw = (
            _normalize_display_lead(str(topic.representative.get("title") or "").strip())
            or _normalize_display_lead(str(topic.representative.get("text_clean") or "").strip())
            or _normalize_display_lead(str(topic.representative.get("description") or "").strip())
            or _normalize_display_lead(str(topic.representative.get("author_name") or topic.representative.get("source") or "").strip())
            or "Публикация по теме форума"
        )
    lead = re.sub(r"[.?!…]+\s*$", "", lead_raw).strip()
    if not lead:
        lead = "Публикация по теме форума"
    source_part = _source_links(topic, show_date=show_date)
    return f"• {html.escape(lead)} ({source_part})"


def render_digest(
    *,
    project: ProjectConfig,
    now_local: datetime,
    media_daily_count: int,
    social_daily_count: int,
    media_topics: list[GroupedTopic],
    social_topics: list[GroupedTopic],
    media_show_dates: bool,
    social_show_dates: bool,
    analytics: dict,
) -> str:
    forum_name = _project_display_name(project)
    lines: list[str] = []
    lines.append(f"<b>{html.escape(_greeting(now_local))}</b>")
    lines.append("")
    lines.append(
        f'Мониторинг упоминаний форума <b>«{html.escape(forum_name)}»</b> в СМИ и соцсетях.'
    )
    lines.append(f"<i>{now_local.strftime('%d.%m.%Y')} · {now_local.strftime('%H:%M')} Мск</i>")
    lines.append("")
    lines.append(f"<b>Сообщений за сутки в СМИ:</b> {media_daily_count}")
    lines.append(f"<b>Сообщений за сутки в соцсетях:</b> {social_daily_count}")
    lines.append("")
    lines.append("<b>Топ тем в СМИ:</b>")
    lines.append("")
    if media_topics:
        for topic in media_topics[:5]:
            lines.append(_topic_line(topic, show_date=media_show_dates))
            lines.append("")
    else:
        lines.append("• Нет релевантных тем за период.")
        lines.append("")
    lines.append("")
    lines.append("<b>Топ тем в соцсетях:</b>")
    lines.append("")
    if social_topics:
        for topic in social_topics[:5]:
            lines.append(_topic_line(topic, show_date=social_show_dates))
            lines.append("")
    else:
        lines.append("• Нет релевантных тем за период.")
        lines.append("")
    lines.append("")
    lines.append("<b>Коммуникационные риски:</b>")
    lines.append("")
    risks = [str(x.get("title") or "").strip() for x in analytics.get("risks", [])[:3] if str(x.get("title") or "").strip()]
    if risks:
        for risk in risks:
            lines.append(f"• {html.escape(risk)}")
            lines.append("")
            lines.append("")
    else:
        lines.append("• Явных значимых рисков в текущем массиве не выявлено.")
        lines.append("")
        lines.append("")
    lines.append("")
    lines.append("<b>Коммуникационные возможности:</b>")
    lines.append("")
    opps = [str(x.get("title") or "").strip() for x in analytics.get("opportunities", [])[:3] if str(x.get("title") or "").strip()]
    if opps:
        for opp in opps:
            lines.append(f"• {html.escape(opp)}")
            lines.append("")
            lines.append("")
    else:
        lines.append("• Явных выраженных возможностей в текущем массиве не выявлено.")
        lines.append("")
        lines.append("")
    lines.append("")
    return "\n".join(lines).strip() + "\n"
