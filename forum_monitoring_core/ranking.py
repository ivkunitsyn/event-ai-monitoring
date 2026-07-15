from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .utils import parse_any_dt


_MEDIA_SOURCE_BOOST = {
    "tass.ru": 35,
    "interfax": 32,
    "vedomosti.ru": 28,
    "kommersant.ru": 28,
    "rbc.ru": 27,
}

_MEDIA_LOW_QUALITY_HOST_HINTS = (
    "bezformata",
    "worldinform",
    "travelpayhot",
    "103news",
)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def media_score(item: dict, now: datetime) -> float:
    published = parse_any_dt(item.get("published_at")) or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    recency = max(0.0, 100 - age_hours * 2.2)
    domain = _domain(str(item.get("canonical_url") or item.get("link") or ""))
    source_weight = 8
    for key, value in _MEDIA_SOURCE_BOOST.items():
        if key in domain:
            source_weight = max(source_weight, value)
    title_len = min(20, len(str(item.get("title") or "")) // 8)
    source_title = str(item.get("source_title") or "").lower()
    penalty = 0.0
    if any(x in source_title for x in ("посольств", "консульств", "канцелярия россии")):
        penalty += 15.0
    if ".mid.ru" in domain:
        penalty += 18.0
    if any(x in domain for x in _MEDIA_LOW_QUALITY_HOST_HINTS):
        penalty += 14.0
    return recency + source_weight + title_len - penalty


def social_score(item: dict, now: datetime) -> float:
    published = parse_any_dt(item.get("published_at")) or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    recency = max(0.0, 100 - age_hours * 2.5)
    audience = float(item.get("audience") or 0)
    aud_weight = min(35.0, math.log10(max(10.0, audience)) * 10)

    metrics = item.get("metrics") or {}
    likes = float(metrics.get("likes") or 0)
    comments = float(metrics.get("comments") or 0)
    reposts = float(metrics.get("reposts") or 0)
    engagement = min(20.0, math.log10(max(1.0, likes + comments * 2 + reposts * 3 + 1)) * 8)
    return recency + aud_weight + engagement


def sort_media(items: list[dict], now: datetime) -> list[dict]:
    return sorted(items, key=lambda x: (media_score(x, now), parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)


def sort_social(items: list[dict], now: datetime) -> list[dict]:
    return sorted(items, key=lambda x: (social_score(x, now), parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)


def pick_time_first(items: list[dict], now: datetime, top_n: int) -> list[dict]:
    day_start = now - timedelta(days=1)
    recent = [x for x in items if (parse_any_dt(x.get("published_at")) or now) >= day_start]
    older = [x for x in items if (parse_any_dt(x.get("published_at")) or now) < day_start]

    ranked_recent = sorted(
        recent,
        key=lambda x: (parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc), x.get("_score", 0)),
        reverse=True,
    )
    ranked_older = sorted(
        older,
        key=lambda x: (parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc), x.get("_score", 0)),
        reverse=True,
    )
    out = ranked_recent[:top_n]
    if len(out) < top_n:
        out.extend(ranked_older[: max(0, top_n - len(out))])
    return out[:top_n]
