from __future__ import annotations

import hashlib
import html
import math
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse, urlunparse

try:
    from dateutil import parser as dateparser
except Exception:  # pragma: no cover - optional dependency
    dateparser = None


SOCIAL_HOSTS = {
    "t.me",
    "telegram.me",
    "vk.com",
    "vk.ru",
    "m.vk.com",
    "ok.ru",
    "odnoklassniki.ru",
    "max.ru",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def html_to_text(value: str) -> str:
    s = value or ""
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|li|tr|h\d)>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    return collapse_spaces(s)


def parse_any_dt(raw: str | datetime | None) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    if dateparser is not None:
        try:
            dt = dateparser.parse(s)
            if dt:
                if not dt.tzinfo:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
        except Exception:
            pass

    # Fallback parser without python-dateutil
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def short_date(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.strftime("%d.%m")


def clean_url_tail(url: str) -> str:
    u = (url or "").strip().replace("&#124;", "|")
    if u.startswith(("http://", "https://")) and "|" in u:
        u = u.split("|", 1)[0].strip()
    u = re.sub(r"[)\]>»\"'“”.,;:]+$", "", u)
    return u


def canonical_url(url: str) -> str:
    u = clean_url_tail(url)
    if not u:
        return ""
    try:
        p = urlparse(u)
    except Exception:
        return u
    scheme = (p.scheme or "https").lower()
    host = (p.netloc or "").lower().split(":")[0]
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    q = p.query
    if host in {"t.me", "telegram.me"} and path:
        parts = [x for x in path.strip("/").split("/") if x]
        if len(parts) >= 2:
            path = f"/{parts[0]}/{parts[1]}"
            q = ""
    if host in {"vk.com", "vk.ru", "m.vk.com"}:
        m = re.search(r"(?i)\b(wall-?\d+_\d+)\b", path)
        if m:
            path = f"/{m.group(1).lower()}"
            q = ""
    return urlunparse((scheme, host, path, "", q, ""))


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"https?://[^\s)\]»\"']+", text):
        u = clean_url_tail(m.group(0))
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    for m in re.finditer(
        r"(?iu)\b((?:t\.me|telegram\.me|vk\.com|vk\.ru|m\.vk\.com|max\.ru|ok\.ru|odnoklassniki\.ru)/[^\s)\]»\"']+)",
        text,
    ):
        u = clean_url_tail("https://" + m.group(1))
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def is_social_post_url(url: str) -> bool:
    u = clean_url_tail(url)
    if not u.startswith(("http://", "https://")):
        return False
    try:
        p = urlparse(u)
    except Exception:
        return False
    host = (p.netloc or "").lower().split(":")[0]
    if host not in SOCIAL_HOSTS:
        return False
    path = (p.path or "").strip("/")
    parts = [x for x in path.split("/") if x]
    if host in {"t.me", "telegram.me"}:
        return len(parts) >= 2 and bool(re.fullmatch(r"\d+", parts[-1].split("?")[0]))
    if host in {"vk.com", "vk.ru", "m.vk.com"}:
        return bool(re.search(r"(?i)\bwall-?\d+_\d+\b", path))
    if host == "max.ru":
        return len(parts) >= 2
    if host in {"ok.ru", "odnoklassniki.ru"}:
        return len(parts) >= 2
    return False


def pick_social_post_url(text: str, fallback: str = "") -> str:
    s = text or ""
    for m in re.finditer(r"(?iu)\bчитать\b(?:\s+на\s+площадке)?\s*(?:\(|:)?\s*((?:https?://)?[^\s)]+)", s):
        u = clean_url_tail(m.group(1))
        if u and not u.startswith(("http://", "https://")):
            u = "https://" + u
        if is_social_post_url(u):
            return u
    for u in extract_urls(s):
        if is_social_post_url(u):
            return u
    if fallback and is_social_post_url(fallback):
        return fallback
    return ""


def normalize_for_match(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^a-zа-я0-9\s]", " ", s, flags=re.IGNORECASE)
    return collapse_spaces(s)


def to_sentence(text: str, max_len: int = 220) -> str:
    s = collapse_spaces(text)
    if not s:
        return ""
    s = s.strip(" \t\r\n-–—,:;")
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,.;:!?])(?=[^\s])", r"\1 ", s)
    s = re.sub(r"\s*…+\s*", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    if len(s) > max_len:
        clipped = s[: max_len + 1]
        terminal = max(clipped.rfind(". "), clipped.rfind("! "), clipped.rfind("? "), clipped.rfind("… "))
        if terminal > max_len // 2:
            s = clipped[: terminal + 1].strip(" ,;:-")
        else:
            cut = clipped.rfind(" ")
            if cut < max_len // 2:
                cut = max_len
            s = clipped[:cut].strip(" ,;:-")
    s = re.sub(r"(?iu)\bв\s+публикации\s+сообщается,\s+что\s+", "", s)
    if not re.search(r"[.!?…]$", s):
        s += "."
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def strip_allcaps_prefix(text: str) -> str:
    s = collapse_spaces(text)
    if not s:
        return ""
    m = re.match(r"^([A-ZА-ЯЁ\s]{4,})([:\-–—]\s*)(.+)$", s)
    if m:
        return m.group(3).strip()
    return s


def stable_hash(values: Iterable[str]) -> str:
    h = hashlib.sha1()
    for v in values:
        h.update((v or "").encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, float):
            if math.isnan(value):
                return default
        return int(str(value).replace(" ", "").strip())
    except Exception:
        return default
