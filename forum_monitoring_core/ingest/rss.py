from __future__ import annotations

import asyncio
import ssl
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp
try:
    import feedparser  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    feedparser = None

from ..config import AppConfig, ProjectConfig
from ..utils import collapse_spaces, extract_urls, html_to_text, parse_any_dt


def _to_media_item(entry: dict[str, Any], project: ProjectConfig) -> dict[str, Any]:
    title = str(entry.get("title") or "").strip()
    description = html_to_text(str(entry.get("description") or ""))
    link = str(
        entry.get("art_url")
        or entry.get("arturl")
        or entry.get("link")
        or entry.get("id")
        or ""
    ).strip()
    if not link:
        urls = extract_urls(" ".join([title, description]))
        if urls:
            link = urls[0]
    guid = str(entry.get("guid") or entry.get("id") or link or title).strip()
    source_title = str(entry.get("newspapername") or entry.get("source") or "").strip()

    published_at = None
    if entry.get("published_parsed"):
        try:
            published_at = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc)
        except Exception:
            published_at = None
    if not published_at:
        published_at = parse_any_dt(
            str(entry.get("published") or entry.get("updated") or entry.get("pubDate") or "")
        )
    if not published_at:
        published_at = datetime.now(timezone.utc)

    return {
        "project_code": project.code,
        "feed": project.code,
        "guid": guid,
        "title": title,
        "link": link,
        "canonical_url": link,
        "source_title": source_title,
        "description": description,
        "published_at": published_at,
        "last_seen_at": datetime.now(timezone.utc),
        "text_clean": collapse_spaces(f"{title}. {description}"),
        "is_relevant": 1,
        "is_noise": 0,
    }


def _parse_rss_fallback(content: bytes) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(content)
    except Exception:
        return {"entries": entries}
    for item in root.findall(".//item"):
        obj: dict[str, Any] = {}
        for tag in ("guid", "title", "link", "description", "pubDate", "art_url"):
            node = item.find(tag)
            if node is not None and node.text:
                obj[tag] = node.text.strip()
        if "pubDate" in obj:
            obj["published"] = obj["pubDate"]
        entries.append(obj)
    return {"entries": entries}


async def fetch_rss_resilient(url: str, proxy: str = "") -> dict[str, Any]:
    async def _fetch(disable_ssl: bool, use_proxy: str = "") -> bytes:
        timeout = aiohttp.ClientTimeout(total=35)
        ssl_ctx = None
        ssl_opt: bool | ssl.SSLContext = True
        if disable_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            ssl_opt = ssl_ctx

        connector = aiohttp.TCPConnector(ssl=ssl_opt)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            kwargs: dict[str, Any] = {}
            if use_proxy:
                kwargs["proxy"] = use_proxy
            async with session.get(url, **kwargs) as resp:
                resp.raise_for_status()
                return await resp.read()

    errors: list[str] = []
    for disable_ssl, use_proxy in (
        (False, ""),
        (True, ""),
        (False, proxy),
        (True, proxy),
    ):
        if use_proxy is None:
            use_proxy = ""
        try:
            content = await _fetch(disable_ssl=disable_ssl, use_proxy=use_proxy)
            if feedparser is not None:
                return feedparser.parse(content)
            return _parse_rss_fallback(content)
        except Exception as exc:
            errors.append(f"ssl_off={int(disable_ssl)} proxy={int(bool(use_proxy))} {type(exc).__name__}: {exc}")
    raise RuntimeError("RSS fetch failed: " + " | ".join(errors))


async def poll_project_rss(project: ProjectConfig, cfg: AppConfig) -> list[dict[str, Any]]:
    if not project.rss_url:
        return []
    feed = await fetch_rss_resilient(project.rss_url, proxy=cfg.rss_proxy)
    entries = feed.get("entries") or []
    return [_to_media_item(dict(e), project) for e in entries]


async def poll_all_rss(cfg: AppConfig) -> dict[str, list[dict[str, Any]]]:
    tasks = {
        code: asyncio.create_task(poll_project_rss(project, cfg))
        for code, project in cfg.projects.items()
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for code, task in tasks.items():
        try:
            result[code] = await task
        except Exception:
            result[code] = []
    return result
