from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import re

try:
    from rapidfuzz import fuzz

    def _token_set_ratio(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b))
except Exception:  # pragma: no cover - fallback for minimal env
    import difflib

    def _token_set_ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(a=a, b=b).ratio() * 100.0

from .utils import normalize_for_match, parse_any_dt


@dataclass
class GroupedTopic:
    group_key: str
    source_kind: str
    representative: dict
    items: list[dict]
    sources: list[dict]


def _clean_source_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return "Источник"
    s = re.sub(r"\(\s*https?://[^)]+\)", " ", s, flags=re.I)
    s = re.sub(r"(?iu)\bв\s+блоге\b", " ", s)
    # Убираем хвост вида "(rg.ru)" / "(khovar.tj/rus)" чтобы не дублировать домен в названии.
    s = re.sub(r"\s*\(\s*(?:https?://)?[a-z0-9.-]+\.[a-z]{2,}(?:/[^)]*)?\s*\)\s*$", "", s, flags=re.I)
    s = re.sub(r"\s{2,}", " ", s).strip(" -:;,.")
    s = re.sub(r"(?iu)^(.{5,90}?)\s+\1$", r"\1", s)
    return s or "Источник"


def _text_signature(item: dict) -> str:
    text = " ".join(
        [
            str(item.get("lead_clean") or ""),
            str(item.get("title") or ""),
            str(item.get("text_clean") or ""),
            str(item.get("description") or ""),
            str(item.get("text_raw") or ""),
        ]
    )
    return normalize_for_match(text)


def _pick_representative(items: list[dict]) -> dict:
    def _lead_penalty(item: dict) -> int:
        lead = str(item.get("lead_clean") or "")
        title = str(item.get("title") or "")
        score = 0
        if not lead:
            score += 20
        if len(lead.strip()) < 30:
            score += 12
        if re.search(r"(?iu)^(.{4,80}?)\s+\1$", lead.strip()):
            score += 25
        if re.search(r"(?iu)^(в\s+округе|вниманию\s+руководител|магнит\s+для\s+бизнеса)\b", lead.strip()):
            score += 15
        if lead.strip() == title.strip() and len(lead.strip()) < 35:
            score += 8
        return score

    scored = sorted(
        items,
        key=lambda x: (
            -_lead_penalty(x),
            float(x.get("_score", 0.0)),
            parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
            len(str(x.get("text_clean") or x.get("description") or "")),
        ),
        reverse=True,
    )
    return scored[0]


def _collect_sources(items: list[dict], source_kind: str) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    out: list[dict] = []
    for item in sorted(
        items,
        key=lambda x: parse_any_dt(x.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        if source_kind == "media":
            name = _clean_source_name(str(item.get("source_title") or item.get("title") or "Источник"))
            url = str(item.get("canonical_url") or item.get("link") or "").strip()
        else:
            name = _clean_source_name(str(item.get("author_name") or item.get("source") or "Площадка"))
            url = str(item.get("canonical_url") or item.get("url") or "").strip()
        norm_name = normalize_for_match(name)
        key = (name, url)
        if key in seen or norm_name in seen_names:
            continue
        seen.add(key)
        seen_names.add(norm_name)
        out.append({"name": name, "url": url})
    return out


def group_items(items: list[dict], source_kind: str, threshold: int = 87) -> list[GroupedTopic]:
    if not items:
        return []

    clusters: list[dict] = []
    for item in items:
        sig = _text_signature(item)
        if not sig:
            continue
        matched: dict | None = None
        best_score = 0.0
        for cluster in clusters:
            score = _token_set_ratio(sig, cluster["signature"])
            if score > best_score and score >= threshold:
                best_score = score
                matched = cluster
        if matched is None:
            clusters.append({"signature": sig, "items": [item]})
        else:
            matched["items"].append(item)

    grouped: list[GroupedTopic] = []
    for idx, cluster in enumerate(clusters, start=1):
        grp_items = cluster["items"]
        rep = _pick_representative(grp_items)
        grouped.append(
            GroupedTopic(
                group_key=f"{source_kind}:{idx}",
                source_kind=source_kind,
                representative=rep,
                items=grp_items,
                sources=_collect_sources(grp_items, source_kind=source_kind),
            )
        )

    grouped.sort(
        key=lambda g: (
            float(g.representative.get("_score", 0.0)),
            parse_any_dt(g.representative.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return grouped


def flatten_for_excel(items: list[dict], dedup_exact: bool = True) -> list[dict]:
    if not dedup_exact:
        return list(items)
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for it in items:
        key = (
            str(it.get("canonical_url") or it.get("url") or it.get("link") or "").strip(),
            str(it.get("published_at") or "").strip(),
            normalize_for_match(str(it.get("text_clean") or it.get("title") or ""))[:240],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def group_summary_counts(groups: list[GroupedTopic]) -> dict[str, int]:
    by_topic: dict[str, int] = defaultdict(int)
    for grp in groups:
        topic = str(grp.representative.get("topic") or grp.group_key)
        by_topic[topic] += len(grp.items)
    return dict(by_topic)
