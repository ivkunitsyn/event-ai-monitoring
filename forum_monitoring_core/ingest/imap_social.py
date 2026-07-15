from __future__ import annotations

import email as pyemail
import html
import imaplib
import re
import base64
from datetime import datetime, timezone
from email import policy as email_policy
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any

from ..config import AppConfig
from ..utils import (
    collapse_spaces,
    extract_urls,
    html_to_text,
    is_social_post_url,
    normalize_for_match,
    pick_social_post_url,
    safe_int,
    stable_hash,
)

_DATE_LINE_RE = re.compile(r"(?m)^\s*\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*$")
_DATE_ANY_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?")
_INLINE_JUNK_RE = re.compile(
    r"(?iu)\b(поиск дешевых авиабилетов|горящие туры|бронирование(?: отелей)?|экскурсии|туризм, путевки|авиабилеты)\b"
)
_FORUM_MARKER_RE = re.compile(
    r"(?iu)\b(киф|кавказск\w*\s+инвестиц\w*\s+форум\w*|внот|всероссийск\w+\s+недел\w+\s+охран\w+\s+труд\w*|"
    r"рэн|российск\w+\s+энергетическ\w+\s+недел\w*|путешествуй|туристическ\w+\s+форум\w*|"
    r"ркф|российск\w+\s+космическ\w+\s+форум\w*)\b"
)


def _imap_mailbox_encode(name: str) -> bytes:
    """
    IMAP modified UTF-7 for non-ASCII mailbox names (e.g. Cyrillic).
    """
    s = str(name or "INBOX")
    out = bytearray()
    buf: list[str] = []

    def _flush_buf() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        b64 = base64.b64encode(raw).rstrip(b"=").replace(b"/", b",")
        out.extend(b"&" + b64 + b"-")
        buf.clear()

    for ch in s:
        o = ord(ch)
        if 0x20 <= o <= 0x7E and ch != "&":
            _flush_buf()
            out.extend(ch.encode("ascii"))
        elif ch == "&":
            _flush_buf()
            out.extend(b"&-")
        else:
            buf.append(ch)
    _flush_buf()
    return bytes(out)


def _imap_mailbox_decode(name: str) -> str:
    s = str(name or "")
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        j = s.find("-", i)
        if j < 0:
            out.append(ch)
            i += 1
            continue
        token = s[i + 1 : j]
        if token == "":
            out.append("&")
            i = j + 1
            continue
        b64 = token.replace(",", "/")
        pad = "=" * ((4 - len(b64) % 4) % 4)
        try:
            raw = base64.b64decode((b64 + pad).encode("ascii"))
            out.append(raw.decode("utf-16-be", errors="replace"))
        except Exception:
            out.append("&" + token + "-")
        i = j + 1
    return "".join(out)


def _resolve_mailbox_token(im: imaplib.IMAP4_SSL, requested: str) -> bytes:
    req_norm = normalize_for_match(requested or "")
    if not req_norm:
        return b"INBOX"
    try:
        typ, data = im.list()
    except Exception:
        typ, data = "NO", None
    if typ != "OK" or not data:
        return _imap_mailbox_encode(requested)
    for row in data:
        if not isinstance(row, (bytes, bytearray)):
            continue
        line = bytes(row).decode("ascii", errors="ignore")
        m = re.search(r'\)\s+"[^"]*"\s+(.+)$', line)
        if not m:
            continue
        token_raw = m.group(1).strip()
        token_raw = token_raw.strip('"')
        decoded = _imap_mailbox_decode(token_raw)
        if normalize_for_match(decoded) == req_norm:
            token = token_raw.encode("ascii", errors="ignore")
            if token and not (token.startswith(b'"') and token.endswith(b'"')):
                token = b'"' + token + b'"'
            return token
    token = _imap_mailbox_encode(requested)
    if token and not (token.startswith(b'"') and token.endswith(b'"')):
        token = b'"' + token + b'"'
    return token


def _fix_mojibake(text: str) -> str:
    s = text or ""
    if not s:
        return s
    # Типичный случай: UTF-8 ошибочно прочитан как latin-1.
    if not any(ch in s for ch in ("ð", "Ð", "Ñ", "â", "Ã", "Â", "�")):
        return s
    try:
        fixed = s.encode("latin-1", errors="replace").decode("utf-8", errors="replace")
    except Exception:
        return s
    if not fixed:
        return s

    def _score(v: str) -> int:
        bad = sum(v.count(ch) for ch in ("ð", "Ð", "Ñ", "â", "Ã", "Â", "�", "?"))
        cyr = len(re.findall(r"[А-Яа-яЁё]", v))
        return cyr * 3 - bad * 5

    return fixed if _score(fixed) > _score(s) else s


def _mime_decode(raw: str) -> str:
    if not raw:
        return ""
    parts: list[str] = []
    try:
        for chunk, enc in decode_header(raw):
            if isinstance(chunk, bytes):
                try:
                    parts.append(chunk.decode(enc or "utf-8", errors="replace"))
                except Exception:
                    parts.append(chunk.decode("utf-8", errors="replace"))
            else:
                parts.append(str(chunk))
    except Exception:
        return str(raw)
    return "".join(parts).strip()


def _html_email_to_text(html_body: str) -> str:
    s = html_body or ""
    s = re.sub(
        r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda m: f"{html_to_text(m.group(2) or '').strip()} ({m.group(1).strip()})",
        s,
    )
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|li|tr|h\d)>", "\n", s)
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return collapse_spaces(s.replace("\n", " \n "))


def _message_body_text(msg_obj: Any) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if getattr(msg_obj, "is_multipart", lambda: False)():
        for part in msg_obj.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype not in {"text/plain", "text/html"}:
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                if isinstance(payload, bytes):
                    cs = part.get_content_charset() or "utf-8"
                    try:
                        content = payload.decode(cs, errors="replace")
                    except Exception:
                        content = payload.decode("utf-8", errors="replace")
                else:
                    content = str(payload or "")
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            text = _fix_mojibake(str(content or "")).strip()
            if not text:
                continue
            if ctype == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
    else:
        ctype = (msg_obj.get_content_type() or "").lower()
        try:
            content = msg_obj.get_content()
        except Exception:
            payload = msg_obj.get_payload(decode=True) or b""
            if isinstance(payload, bytes):
                cs = msg_obj.get_content_charset() or "utf-8"
                try:
                    content = payload.decode(cs, errors="replace")
                except Exception:
                    content = payload.decode("utf-8", errors="replace")
            else:
                content = str(payload or "")
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        text = _fix_mojibake(str(content or "")).strip()
        if text:
            if ctype == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    plain_text = "\n\n".join(plain_parts).replace("\r", "\n").strip()
    html_text = _html_email_to_text("\n".join(html_parts)).strip() if html_parts else ""

    if html_text and "Читать на площадке" in html_text:
        return html_text
    return plain_text or html_text


def _strip_footer(text: str) -> str:
    s = (text or "").replace("\r", "\n")
    low = s.lower()
    # Удаляем только хвост уведомления, не трогая основное тело.
    marker = low.find("вы получили это письмо")
    if marker >= 0:
        s = s[:marker]

    # Часто заголовок уведомления слипается с телом в одну строку.
    if re.search(r"(?iu)^\s*новые\s+сообщения\s+в\s+отчет[её]", s):
        m_dt = _DATE_ANY_RE.search(s)
        if m_dt:
            s = s[m_dt.start() :]

    s = re.sub(r"(?iu)\bперейти\s+в\s+отчет\b.*$", "", s)
    s = re.sub(r"(?iu)\bотписаться\s+от\s+рассылки\b.*$", "", s)
    s = re.sub(r"(?im)^\s*Поиск дешевых авиабилетов[^\n]*$", "", s)
    s = re.sub(r"(?im)^\s*Туризм,\s*путевки[^\n]*$", "", s)
    s = re.sub(r"(?im)^\s*\d{1,2}:\d{2}\s*$", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _split_blocks(raw_text: str) -> list[str]:
    s = (raw_text or "").replace("\r", "\n")
    starts = [m.start() for m in _DATE_ANY_RE.finditer(s)]
    if not starts:
        return [s.strip()] if s.strip() else []
    blocks: list[str] = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(s)
        part = s[st:en].strip()
        if part:
            blocks.append(part)
    return blocks


def _parse_block(block: str) -> dict[str, Any]:
    s = _strip_footer(block)
    content = {
        "raw_text": s,
        "content_text": s,
        "platform": "",
        "author": "",
        "audience": 0,
        "likes": 0,
        "comments": 0,
        "published_at": None,
    }
    if not s:
        return content

    m_dt = re.search(
        r"(?iu)(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
        s,
    )
    if m_dt:
        try:
            content["published_at"] = datetime(
                int(m_dt.group(3)),
                int(m_dt.group(2)),
                int(m_dt.group(1)),
                int(m_dt.group(4)),
                int(m_dt.group(5)),
                int(m_dt.group(6) or 0),
                tzinfo=timezone.utc,
            )
        except Exception:
            content["published_at"] = None

    m_platform = re.search(
        r"(?iu)Площадка:\s*([^|\n]+?)\s*\|\s*Написал\s+(.+?)(?=\s+Аудитория\s+автора:|\s+[⚡🔥⭐️]|\s+\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}|$)",
        s,
    )
    if m_platform:
        content["platform"] = m_platform.group(1).strip()
        author = m_platform.group(2).strip()
        author = re.sub(r"\s*\(\s*https?://[^)]+\)\s*$", "", author).strip()
        content["author"] = author

    m_metrics = re.search(
        r"(?iu)Аудитория\s+автора:\s*([\d\s]+)(?:\s*\|\s*Лайки:\s*(\d+))?(?:\s*\|\s*Комментарии:\s*(\d+))?",
        s,
    )
    if m_metrics:
        content["audience"] = safe_int(m_metrics.group(1), 0)
        content["likes"] = safe_int(m_metrics.group(2), 0)
        content["comments"] = safe_int(m_metrics.group(3), 0)

    body = s
    if m_metrics:
        body = s[m_metrics.end() :].strip()
    elif m_platform:
        body = s[m_platform.end() :].strip()
    body = re.sub(r"(?iu)\bЧитать\s+на\s+площадке\b", "", body)
    body = re.sub(r"(?im)^\s*Площадка:\s*[^\n]*$", "", body)
    body = re.sub(r"(?im)^\s*Аудитория\s+автора:\s*[^\n]*$", "", body)
    body = re.sub(r"(?iu)\bперейти\s+в\s+отчет\b.*$", "", body)
    body = re.sub(r"(?iu)\bотписаться\s+от\s+рассылки\b.*$", "", body)
    body = _INLINE_JUNK_RE.sub(" ", body)
    body = re.sub(r"(?iu)\b(?:youtube|instagram|inslagram)\.?\s*com\S*", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    content["content_text"] = body
    return content


def _extract_source_urls(text: str) -> str:
    url = pick_social_post_url(text)
    if url and is_social_post_url(url):
        return url
    for u in extract_urls(text):
        if is_social_post_url(u):
            return u
    return ""


def _extract_forum_context(text: str) -> str:
    s = collapse_spaces(text or "")
    if not s:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", s) if p.strip()]
    if not parts:
        return s
    for idx, part in enumerate(parts):
        if _FORUM_MARKER_RE.search(part):
            prev = parts[idx - 1] if idx > 0 else ""
            # Берем только информативный контекст перед ключевой форумной фразой.
            if prev and len(prev) > 25 and not _INLINE_JUNK_RE.search(prev):
                return collapse_spaces(f"{prev} {part}")
            return collapse_spaces(part)
    return s


def fetch_imap_rows(
    cfg: AppConfig,
    *,
    last_uid: int,
    force_full_reread: bool,
    mailbox: str | None = None,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    if not cfg.imap_enable:
        return [], last_uid, {"disabled": 1}

    out_rows: list[dict[str, Any]] = []
    max_uid = int(last_uid or 0)
    stats = {
        "scanned": 0,
        "new": 0,
        "from_sender": 0,
        "blocks": 0,
        "inserted": 0,
    }

    sender_allow = {x.strip().lower() for x in cfg.imap_sender_allowlist if x.strip()}
    im = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=25)
    try:
        im.login(cfg.imap_login, cfg.imap_password)
        selected_mailbox = (mailbox or cfg.imap_mailbox or "INBOX").strip() or "INBOX"
        mailbox_token = _resolve_mailbox_token(im, selected_mailbox)
        typ, _ = im.select(mailbox_token, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"imap select failed: {typ}")

        typ, data = "NO", None
        if len(sender_allow) == 1:
            only_sender = next(iter(sender_allow))
            try:
                typ, data = im.uid("search", None, "FROM", f'"{only_sender}"')
            except Exception:
                typ, data = "NO", None
        if typ != "OK":
            typ, data = im.uid("search", None, "ALL")
            if typ != "OK":
                raise RuntimeError(f"imap search failed: {typ}")

        uids = sorted(
            {
                int(x)
                for x in (data[0] or b"").split()
                if x and str(x, errors="ignore").isdigit()
            }
        )
        stats["scanned"] = len(uids)

        if not force_full_reread and last_uid <= 0 and cfg.imap_bootstrap_limit > 0 and len(uids) > cfg.imap_bootstrap_limit:
            uids = uids[-cfg.imap_bootstrap_limit :]
        if not force_full_reread:
            uids = [x for x in uids if x > int(last_uid or 0)]
        stats["new"] = len(uids)

        for uid in uids:
            max_uid = max(max_uid, uid)
            try:
                typ, msg_data = im.uid("fetch", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data:
                    continue
            except TimeoutError:
                continue
            except Exception:
                continue

            raw_bytes = b""
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                    raw_bytes = bytes(part[1])
                    break
            if not raw_bytes:
                continue

            try:
                msg_obj = pyemail.message_from_bytes(raw_bytes, policy=email_policy.default)
            except Exception:
                msg_obj = pyemail.message_from_bytes(raw_bytes)

            from_hdr = _mime_decode(str(msg_obj.get("From") or ""))
            from_addr = (pyemail.utils.parseaddr(from_hdr)[1] or "").strip().lower()
            if sender_allow and from_addr not in sender_allow:
                continue
            stats["from_sender"] += 1

            body = _message_body_text(msg_obj)
            if not body:
                continue
            body = _strip_footer(body)
            blocks = _split_blocks(body)
            if not blocks:
                blocks = [body]
            stats["blocks"] += len(blocks)

            date_hdr = str(msg_obj.get("Date") or "").strip()
            try:
                hdr_dt = parsedate_to_datetime(date_hdr).astimezone(timezone.utc) if date_hdr else datetime.now(timezone.utc)
            except Exception:
                hdr_dt = datetime.now(timezone.utc)

            for idx, block in enumerate(blocks, start=1):
                parsed = _parse_block(block)
                text_raw = parsed.get("raw_text") or block
                content_text = str(parsed.get("content_text") or text_raw)
                text_clean = _extract_forum_context(re.sub(r"https?://\S+", " ", content_text))
                text_norm = normalize_for_match(content_text)
                if not text_norm:
                    continue
                post_url = _extract_source_urls(text_raw)
                published_at = parsed.get("published_at") or hdr_dt

                synthetic = stable_hash(
                    ["imap", selected_mailbox.lower(), str(uid), str(idx)]
                )

                out_rows.append(
                    {
                        "source_type": "imap",
                        "chat_id": "imap",
                        "message_id": f"{selected_mailbox}:{uid}:{idx}",
                        "synthetic_id": synthetic,
                        "source": "Medialogia Email",
                        "platform": parsed.get("platform") or "",
                        "author_name": parsed.get("author") or "",
                        "url": post_url,
                        "canonical_url": post_url,
                        "text_raw": text_raw,
                        "text_clean": text_clean,
                        "metrics": {
                            "likes": safe_int(parsed.get("likes"), 0),
                            "comments": safe_int(parsed.get("comments"), 0),
                        },
                        "audience": safe_int(parsed.get("audience"), 0),
                        "published_at": published_at,
                        "ingested_at": datetime.now(timezone.utc),
                    }
                )
                stats["inserted"] += 1
    finally:
        try:
            im.logout()
        except Exception:
            pass

    return out_rows, max_uid, stats
