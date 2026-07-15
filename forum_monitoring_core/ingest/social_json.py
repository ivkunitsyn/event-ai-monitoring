from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import re

from ..utils import collapse_spaces, normalize_for_match, pick_social_post_url, safe_int, stable_hash


def _parse_row(obj: dict[str, Any], source_file: str) -> dict[str, Any] | None:
    text_raw = str(obj.get("text") or obj.get("text_raw") or obj.get("message") or "").strip()
    if not text_raw:
        return None
    text_clean = collapse_spaces(re.sub(r"https?://\S+", " ", text_raw))
    text_norm = normalize_for_match(text_raw)
    post_url = pick_social_post_url(text_raw, fallback=str(obj.get("url") or ""))

    published_raw = obj.get("published_at") or obj.get("date") or obj.get("time")
    published = None
    if isinstance(published_raw, str):
        try:
            from ..utils import parse_any_dt

            published = parse_any_dt(published_raw)
        except Exception:
            published = None
    if not published:
        published = datetime.now(timezone.utc)

    platform = str(obj.get("platform") or "").strip()
    author = str(obj.get("author") or obj.get("source") or "").strip()
    synthetic = stable_hash(["telegram_export", source_file, post_url, text_norm[:300], published.isoformat()])

    return {
        "source_type": "telegram_export",
        "chat_id": str(obj.get("chat_id") or ""),
        "message_id": str(obj.get("message_id") or ""),
        "synthetic_id": synthetic,
        "source": author or "Telegram export",
        "platform": platform,
        "author_name": author,
        "url": post_url,
        "canonical_url": post_url,
        "text_raw": text_raw,
        "text_clean": text_clean,
        "metrics": {
            "likes": safe_int(obj.get("likes"), 0),
            "comments": safe_int(obj.get("comments"), 0),
            "reposts": safe_int(obj.get("reposts"), 0),
        },
        "audience": safe_int(obj.get("audience") or obj.get("subscribers"), 0),
        "published_at": published,
        "ingested_at": datetime.now(timezone.utc),
    }


def read_social_export(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return []

    out: list[dict[str, Any]] = []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except Exception:
        return []

    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("messages"), list):
        rows = data["messages"]
    else:
        rows = []

    for obj in rows:
        if not isinstance(obj, dict):
            continue
        row = _parse_row(obj, source_file=p.name)
        if row:
            out.append(row)
    return out
