from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import random
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from forum_monitoring_core import MonitoringEngine, ensure_paths, load_config


def _get_by_path(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _norm(s: str) -> str:
    t = (s or "").strip().lower().replace("ё", "е")
    t = t.replace("\u00a0", " ")
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _extract_ru_dates(text: str) -> list[datetime]:
    raw = (text or "").replace("\u00a0", " ")
    raw = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", raw)
    # Поддерживаем варианты с разделителями ".", "-", "/".
    matches = re.findall(r"(\d{2}[./-]\d{2}[./-]\d{4})", raw)
    out: list[datetime] = []
    for m in matches:
        try:
            m2 = m.replace("/", ".").replace("-", ".")
            out.append(datetime.strptime(m2, "%d.%m.%Y"))
        except Exception:
            continue
    return out


def _iter_values(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _iter_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_values(v)


def _extract_text(update: dict[str, Any], update_type: str = "") -> str:
    msg = update.get("message") if isinstance(update, dict) else None
    callback = update.get("callback") if isinstance(update, dict) else None
    utype = (update_type or "").strip().lower()

    if utype == "message_callback":
        for root in (callback, msg, update):
            if not isinstance(root, dict):
                continue
            for key in ("payload", "data", "text", "title"):
                value = root.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    for key2 in ("payload", "data", "text", "title"):
                        v2 = value.get(key2)
                        if isinstance(v2, str) and v2.strip():
                            return v2.strip()

    # Для пользовательских сообщений приоритет - реальный текст сообщения.
    for path in (
        ("message", "body", "text"),
        ("message", "body", "caption"),
        ("message", "text"),
        ("message", "caption"),
    ):
        v = _get_by_path(update, path)
        if isinstance(v, str) and v.strip():
            return v.strip()

    if isinstance(msg, dict):
        for key, value in _iter_values(msg):
            if key in {"text", "caption", "title"} and isinstance(value, str) and value.strip():
                return value.strip()

    # Последний fallback: ищем только текстовые поля, но без payload/data,
    # чтобы не схватить служебные значения вместо текста пользователя.
    if isinstance(update, dict):
        for key, value in _iter_values(update):
            if key in {"text", "caption", "title"} and isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _extract_chat_id(update: dict[str, Any]) -> int | None:
    msg = update.get("message") if isinstance(update, dict) else None
    for path in (
        ("message", "recipient", "chat_id"),
        ("message", "recipient", "chatId"),
        ("message", "chat", "chat_id"),
        ("message", "chat", "id"),
        ("callback", "chat_id"),
        ("callback", "chatId"),
    ):
        v = _get_by_path(update, path)
        try:
            iv = int(v)
            if iv > 0:
                return iv
        except Exception:
            pass

    roots = [msg, update]
    for root in roots:
        if not isinstance(root, dict):
            continue
        for key, value in _iter_values(root):
            if key in {"chat_id", "chatId", "dialog_id"}:
                try:
                    iv = int(value)
                    if iv > 0:
                        return iv
                except Exception:
                    continue
            if key == "chat" and isinstance(value, dict):
                for k2 in ("chat_id", "id"):
                    if k2 in value:
                        try:
                            iv = int(value[k2])
                            if iv > 0:
                                return iv
                        except Exception:
                            pass
    return None


def _extract_user_id(update: dict[str, Any]) -> int | None:
    msg = update.get("message") if isinstance(update, dict) else None
    for path in (
        ("message", "sender", "user_id"),
        ("message", "sender", "userId"),
        ("message", "sender", "id"),
        ("message", "author", "user_id"),
        ("message", "author", "id"),
        ("message", "from", "id"),
        ("callback", "user_id"),
        ("callback", "userId"),
    ):
        v = _get_by_path(update, path)
        try:
            iv = int(v)
            if iv > 0:
                return iv
        except Exception:
            pass

    roots = [msg, update]
    for root in roots:
        if not isinstance(root, dict):
            continue
        for key, value in _iter_values(root):
            if key in {"user_id", "userId", "from_id", "author_id"}:
                try:
                    iv = int(value)
                    if iv > 0:
                        return iv
                except Exception:
                    continue
            if key in {"user", "author", "from"} and isinstance(value, dict):
                for k2 in ("user_id", "id"):
                    if k2 in value:
                        try:
                            iv = int(value[k2])
                            if iv > 0:
                                return iv
                        except Exception:
                            pass
    return None


def _extract_callback_id(update: dict[str, Any]) -> str:
    v = _get_by_path(update, ("callback", "callback_id"))
    if isinstance(v, str) and v.strip():
        return v.strip()
    for key, value in _iter_values(update):
        if key in {"callback_id", "id"} and isinstance(value, str):
            if len(value) >= 8:
                return value
    callback = update.get("callback")
    if isinstance(callback, dict):
        v = callback.get("id")
        if isinstance(v, str):
            return v
    return ""


def _extract_callback_command(update: dict[str, Any]) -> str:
    for path in (
        ("callback", "payload"),
        ("callback", "data"),
        ("callback", "text"),
        ("message", "body", "callback", "payload"),
        ("message", "body", "callback", "data"),
    ):
        v = _get_by_path(update, path)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for k in ("payload", "data", "command", "intent", "text", "value"):
                v2 = v.get(k)
                if isinstance(v2, str) and v2.strip():
                    return v2.strip()
    # Fallback на общий текстовый экстрактор.
    return _extract_text(update, "message_callback")


def _keyboard() -> list[dict[str, Any]]:
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {"type": "callback", "text": "Получить мониторинг", "payload": "get_monitoring"},
                    ],
                    [
                        {
                            "type": "callback",
                            "text": "Получить отчёт в Excel за последнюю неделю",
                            "payload": "get_excel_weekly",
                        }
                    ],
                    [
                        {
                            "type": "callback",
                            "text": "Получить отчёт в Excel за период",
                            "payload": "get_excel_period",
                        }
                    ]
                ]
            },
        }
    ]


class MaxApiError(RuntimeError):
    pass


class MaxClient:
    def __init__(self, token: str, base_url: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token.strip(),
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        to = timeout if timeout is not None else self.timeout
        try:
            resp = self.session.request(method, url, params=params, json=payload, timeout=to)
        except Exception as exc:
            raise MaxApiError(f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise MaxApiError(f"{resp.status_code} {(resp.text or '')[:400]}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    def get_updates(
        self,
        marker: str,
        *,
        limit: int = 100,
        timeout: int = 25,
        types: str = "message_created,message_callback,bot_started",
    ) -> tuple[list[dict[str, Any]], str]:
        params: dict[str, Any] = {"limit": limit, "timeout": timeout, "types": types}
        if marker:
            params["marker"] = marker
        data = self._request("GET", "/updates", params=params, timeout=timeout + 10)
        updates: list[dict[str, Any]] = []
        new_marker = marker
        if isinstance(data, dict):
            updates = data.get("updates") or data.get("result") or []
            new_marker = str(data.get("marker") or marker or "")
        elif isinstance(data, list):
            updates = data
        if not new_marker and updates:
            last = updates[-1]
            new_marker = str(last.get("update_id") or last.get("id") or marker or "")
        return updates, new_marker

    def send_text(
        self,
        *,
        user_id: int | None,
        chat_id: int | None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        fmt: str | None = None,
    ) -> dict[str, Any]:
        if chat_id is None and user_id is None:
            raise MaxApiError("recipient missing: both chat_id and user_id are empty")
        params: dict[str, Any] = {}
        # В MAX всегда отвечаем в тот же чат, где пришло событие.
        if chat_id is not None:
            params["chat_id"] = chat_id
        elif user_id is not None:
            params["user_id"] = user_id
        payload: dict[str, Any] = {"text": text}
        if fmt:
            payload["format"] = fmt
        if attachments:
            payload["attachments"] = attachments
        attempts = 6 if attachments else 1
        for i in range(1, attempts + 1):
            try:
                return self._request("POST", "/messages", params=params, payload=payload) or {}
            except MaxApiError as exc:
                msg = str(exc).lower()
                attachment_not_ready = "attachment.not.ready" in msg or "not.processed" in msg
                if attachments and attachment_not_ready and i < attempts:
                    time.sleep(1.2 + random.random() * 0.4)
                    continue
                raise
        return {}

    def answer_callback(self, callback_id: str, notification: str = "Ок") -> None:
        if not callback_id:
            return
        self._request("POST", "/answers", payload={"callback_id": callback_id, "notification": notification})

    def _find_token(self, obj: Any) -> str:
        if isinstance(obj, dict):
            for k in ("token", "file_token", "media_token", "fileToken"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in obj.values():
                t = self._find_token(v)
                if t:
                    return t
        if isinstance(obj, list):
            for v in obj:
                t = self._find_token(v)
                if t:
                    return t
        return ""

    def upload_media(self, file_path: str, media_type: str = "file") -> dict[str, Any]:
        init = self._request("POST", "/uploads", params={"type": media_type})
        if not isinstance(init, dict):
            raise MaxApiError("upload init invalid")
        upload_url = init.get("url") or init.get("upload_url") or init.get("href")
        if not upload_url:
            raise MaxApiError("upload url missing")
        token = self._find_token(init)
        filename = Path(file_path).name
        data = Path(file_path).read_bytes()
        content_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if filename.lower().endswith(".xlsx")
            else "application/octet-stream"
        )

        for use_auth in (True, False):
            headers = {"Authorization": self.session.headers.get("Authorization", "")} if use_auth else None
            for field in ("data", "file", "document"):
                files = {field: (filename, data, content_type)}
                resp = requests.post(upload_url, files=files, headers=headers, timeout=max(120, self.timeout))
                if resp.status_code >= 400:
                    continue
                payload: Any
                try:
                    payload = resp.json() if resp.text else {}
                except Exception:
                    payload = {}
                payload_token = self._find_token(payload) if isinstance(payload, (dict, list)) else ""
                if payload_token:
                    return {"token": payload_token}
                if isinstance(payload, dict) and payload:
                    if token and "token" not in payload:
                        payload["token"] = token
                    return payload

        if token:
            return {"token": token}
        raise MaxApiError("upload payload empty")

    def send_file(self, *, user_id: int | None, chat_id: int | None, file_path: str, caption: str = "Excel-отчёт готов.") -> None:
        if chat_id is None and user_id is None:
            raise MaxApiError("recipient missing for file")
        payload = self.upload_media(file_path, media_type="file")
        attachments = [{"type": "file", "payload": payload}]
        self.send_text(user_id=user_id, chat_id=chat_id, text=caption, attachments=attachments)


class ForumMaxBot:
    def __init__(self) -> None:
        self.cfg = load_config()
        ensure_paths(self.cfg)
        self.engine = MonitoringEngine(self.cfg)
        asyncio.run(self.engine.init())

        self.project_code = (os.getenv("PROJECT_CODE") or "").strip().lower()
        if self.project_code not in self.cfg.projects:
            raise RuntimeError(f"PROJECT_CODE invalid: {self.project_code}")

        self.bot_token = (os.getenv("MAX_BOT_TOKEN") or "").strip()
        if not self.bot_token:
            raise RuntimeError("MAX_BOT_TOKEN is empty")
        self.max_api_base = (os.getenv("MAX_API_BASE") or "https://platform-api.max.ru").strip()
        self.updates_timeout = int(os.getenv("MAX_UPDATES_TIMEOUT") or 25)
        self.updates_limit = int(os.getenv("MAX_UPDATES_LIMIT") or 100)
        self.update_types = (os.getenv("MAX_UPDATE_TYPES") or "message_created,message_callback,bot_started").strip()
        self.bot_password = (os.getenv("BOT_PASSWORD") or "").strip()
        self.schedule_weekday_hour = int(os.getenv("SCHEDULE_WEEKDAY_HOUR_MSK") or 9)
        self.schedule_weekend_hour = int(os.getenv("SCHEDULE_WEEKEND_HOUR_MSK") or 12)
        self.schedule_minute = int(os.getenv("SCHEDULE_MINUTE_MSK") or 0)
        self.schedule_prepare_minutes = int(os.getenv("SCHEDULE_PREPARE_MINUTES") or 5)
        self.tz = ZoneInfo(self.cfg.timezone)

        self.client = MaxClient(self.bot_token, self.max_api_base, timeout=30)
        self.state_key_marker = f"max:marker:{self.project_code}"
        self.state_key_last_slot = f"max:last_scheduled_slot:{self.project_code}"
        self._recent_update_ids: dict[str, float] = {}
        self._recent_cmd_ids: dict[str, float] = {}
        self._prepared_slot_key = ""
        self._prepared_digest = ""
        self.min_period_date = datetime(2026, 3, 1, tzinfo=self.tz).date()
        self._last_ingest_ts = 0.0

    def _period_state_key(self, user_id: int | None, chat_id: int | None) -> str:
        # Состояние ввода периода должно жить в контексте чата:
        # callback и последующее сообщение с датой иногда приходят с разными user_id.
        actor = chat_id if chat_id is not None else (user_id if user_id is not None else 0)
        return f"max:excel_period_state:{self.project_code}:{actor}"

    def _period_state_keys(self, user_id: int | None, chat_id: int | None) -> list[str]:
        keys: list[str] = []
        if chat_id is not None:
            keys.append(f"max:excel_period_state:{self.project_code}:{chat_id}")
        if user_id is not None:
            keys.append(f"max:excel_period_state:{self.project_code}:{user_id}")
        if not keys:
            keys.append(f"max:excel_period_state:{self.project_code}:0")
        # keep order, remove duplicates
        out: list[str] = []
        seen: set[str] = set()
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    @staticmethod
    def _parse_ru_date(s: str) -> datetime | None:
        dates = _extract_ru_dates(s or "")
        return dates[0] if dates else None

    def _project_display_name(self) -> str:
        project = self.cfg.projects[self.project_code]
        if self.project_code == "puteshestvuy":
            return "Путешествуй"
        return project.name

    def _register_recent(self, store: dict[str, float], key: str, ttl_sec: float) -> bool:
        if not key:
            return False
        now = time.monotonic()
        # cheap cleanup
        stale = [k for k, ts in store.items() if now - ts > max(60.0, ttl_sec * 20)]
        for k in stale:
            store.pop(k, None)
        prev = store.get(key)
        store[key] = now
        return bool(prev is not None and now - prev <= ttl_sec)

    def _is_authed(self, user_id: int | None) -> bool:
        if not self.bot_password:
            return True
        if not user_id:
            return False
        v = asyncio.run(self.engine.storage.state_get(f"max:auth:{self.project_code}:{user_id}", "0"))
        return v == "1"

    def _set_authed(self, user_id: int) -> None:
        asyncio.run(self.engine.storage.state_set(f"max:auth:{self.project_code}:{user_id}", "1"))

    def _subscribe_chat(self, chat_id: int | None) -> None:
        if not chat_id:
            return
        key = f"max:subscriber:{self.project_code}:{chat_id}"
        asyncio.run(self.engine.storage.state_set(key, "1"))

    def _subscribed_chats(self) -> list[int]:
        prefix = f"max:subscriber:{self.project_code}:"
        rows = asyncio.run(self.engine.storage.state_items_by_prefix(prefix))
        out: list[int] = []
        for key, value in rows:
            if value != "1":
                continue
            tail = key.rsplit(":", 1)[-1]
            try:
                cid = int(tail)
                if cid > 0:
                    out.append(cid)
            except Exception:
                continue
        return sorted(set(out))

    def _scheduled_send_at(self, now_local: datetime) -> datetime:
        hour = self.schedule_weekend_hour if now_local.weekday() >= 5 else self.schedule_weekday_hour
        return now_local.replace(hour=hour, minute=self.schedule_minute, second=0, microsecond=0)

    def _slot_key(self, send_at_local: datetime) -> str:
        return send_at_local.strftime("%Y-%m-%d %H:%M")

    def _maybe_scheduled_dispatch(self) -> None:
        now_local = datetime.now(self.tz)
        send_at_local = self._scheduled_send_at(now_local)
        slot_key = self._slot_key(send_at_local)
        prepare_at_local = send_at_local - timedelta(minutes=max(1, self.schedule_prepare_minutes))
        last_sent = asyncio.run(self.engine.storage.state_get(self.state_key_last_slot, ""))

        if last_sent == slot_key:
            return

        if prepare_at_local <= now_local < send_at_local and self._prepared_slot_key != slot_key:
            try:
                digest = asyncio.run(
                    self.engine.build_digest(
                        self.project_code,
                        now_utc=send_at_local.astimezone(timezone.utc),
                    )
                )
                if digest:
                    self._prepared_slot_key = slot_key
                    self._prepared_digest = digest
                    print(f"[sched] prepared slot={slot_key}")
            except Exception as exc:
                print(f"[sched] prepare error: {type(exc).__name__}: {exc}")

        in_regular_window = send_at_local <= now_local < send_at_local + timedelta(minutes=20)
        in_late_catchup_window = (
            now_local >= send_at_local + timedelta(minutes=20)
            and now_local.date() == send_at_local.date()
            and now_local <= send_at_local + timedelta(hours=12)
        )
        if not in_regular_window and not in_late_catchup_window:
            return

        chats = self._subscribed_chats()
        if not chats:
            return

        digest = self._prepared_digest if self._prepared_slot_key == slot_key and self._prepared_digest else ""
        if not digest:
            try:
                digest = asyncio.run(
                    self.engine.build_digest(
                        self.project_code,
                        now_utc=send_at_local.astimezone(timezone.utc),
                    )
                )
            except Exception as exc:
                print(f"[sched] build error: {type(exc).__name__}: {exc}")
                return

        sent = 0
        for chat_id in chats:
            try:
                self._send_long(user_id=None, chat_id=chat_id, text=digest, with_keyboard=True, fmt="HTML")
                sent += 1
            except Exception as exc:
                print(f"[sched] send error chat={chat_id}: {type(exc).__name__}: {exc}")
        if sent:
            asyncio.run(self.engine.storage.state_set(self.state_key_last_slot, slot_key))
            self._prepared_slot_key = slot_key
            self._prepared_digest = digest
            mode = "late_catchup" if in_late_catchup_window and not in_regular_window else "regular"
            print(f"[sched] sent slot={slot_key} chats={sent} mode={mode}")

    def _send_long(
        self,
        *,
        user_id: int | None,
        chat_id: int | None,
        text: str,
        with_keyboard: bool = False,
        fmt: str | None = "HTML",
    ) -> None:
        if not user_id and not chat_id:
            return
        s = (text or "").strip()
        if not s:
            return
        chunk_size = 3300
        parts: list[str] = []
        blocks = [x.strip() for x in s.split("\n\n") if x.strip()]
        if not blocks:
            blocks = [s]
        cur = ""
        for block in blocks:
            candidate = block if not cur else f"{cur}\n\n{block}"
            if len(candidate) <= chunk_size:
                cur = candidate
                continue
            if cur:
                parts.append(cur)
                cur = ""
            if len(block) <= chunk_size:
                cur = block
                continue
            # hard split for very long block
            t = block
            while len(t) > chunk_size:
                cut = t.rfind("\n", 0, chunk_size)
                if cut < 1000:
                    cut = chunk_size
                parts.append(t[:cut].strip())
                t = t[cut:].strip()
            if t:
                cur = t
        if cur:
            parts.append(cur)
        for i, part in enumerate(parts):
            attachments = _keyboard() if (with_keyboard and i == len(parts) - 1) else None
            self.client.send_text(user_id=user_id, chat_id=chat_id, text=part, attachments=attachments, fmt=fmt)

    def _refresh_sources_before_manual(self, min_interval_sec: int = 90) -> None:
        now = time.monotonic()
        if now - self._last_ingest_ts < float(max(10, min_interval_sec)):
            return
        try:
            st = asyncio.run(self.engine.run_ingest_once())
            self._last_ingest_ts = now
            print(
                "[manual] ingest refreshed"
                f" rss_seen={st.rss_seen} rss_inserted={st.rss_inserted}"
                f" social_seen={st.social_seen} social_inserted={st.social_inserted}"
            )
        except Exception as exc:
            print(f"[manual] ingest refresh error: {type(exc).__name__}: {exc}")

    def _handle_command(self, *, cmd: str, user_id: int | None, chat_id: int | None, callback_id: str = "") -> None:
        c = _norm(cmd)
        monitor_cmds = {"получить мониторинг", "monitoring", "/monitoring", "get_monitoring"}
        excel_week_cmds = {
            "получить отчет в excel за последнюю неделю",
            "получить отчёт в excel за последнюю неделю",
            "excel",
            "/excel",
            "get_excel_weekly",
        }
        excel_period_cmds = {
            "получить отчет в excel за период",
            "получить отчёт в excel за период",
            "get_excel_period",
            "excel_period",
        }
        cmd_key = f"{user_id or 0}:{chat_id or 0}:{c}"
        if self._register_recent(self._recent_cmd_ids, cmd_key, ttl_sec=1.7):
            return

        if callback_id:
            try:
                self.client.answer_callback(callback_id, notification="Принято")
            except Exception:
                pass

        if c in {"/start", "start", "старт"}:
            self._subscribe_chat(chat_id)
            title = self._project_display_name()
            text = (
                f"Бот мониторинга: <b>{title}</b>\n\n"
                "Доступные действия:\n"
                "• Получить мониторинг\n"
                "• Получить отчёт в Excel за последнюю неделю\n"
                "• Получить отчёт в Excel за период"
            )
            self._send_long(user_id=user_id, chat_id=chat_id, text=text, with_keyboard=True, fmt="HTML")
            return

        if self.bot_password and not self._is_authed(user_id):
            if c == _norm(self.bot_password) and user_id is not None:
                self._set_authed(user_id)
                self._subscribe_chat(chat_id)
                self._send_long(user_id=user_id, chat_id=chat_id, text="Доступ открыт.", with_keyboard=True, fmt="HTML")
            else:
                self._send_long(user_id=user_id, chat_id=chat_id, text="Введите пароль.", fmt="HTML")
            return

        actor_keys = self._period_state_keys(user_id, chat_id)
        if user_id is not None or chat_id is not None:
            raw_state = ""
            state_key_used = actor_keys[0]
            for key_try in actor_keys:
                raw_try = asyncio.run(self.engine.storage.state_get(key_try, ""))
                if raw_try:
                    raw_state = raw_try
                    state_key_used = key_try
                    break
            if raw_state and (c in monitor_cmds or c in excel_week_cmds or c in excel_period_cmds):
                for key_try in actor_keys:
                    asyncio.run(self.engine.storage.state_set(key_try, ""))
                raw_state = ""
            if raw_state:
                try:
                    st = json.loads(raw_state)
                except Exception:
                    st = {}
                step = str(st.get("step") or "")
                if step == "await_start":
                    found_dates = _extract_ru_dates(cmd)
                    dt_start = found_dates[0] if found_dates else None
                    if not dt_start:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text="Неверный формат даты. Введите дату начала в формате ДД.ММ.ГГГГ.",
                            fmt="HTML",
                        )
                        return
                    d_start = dt_start.date()
                    if d_start < self.min_period_date:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text=(
                                f"Дата начала мониторинга доступна не раньше {self.min_period_date.strftime('%d.%m.%Y')}."
                            ),
                            fmt="HTML",
                        )
                        return
                    # Поддержка варианта, когда пользователь сразу прислал диапазон дат.
                    if len(found_dates) >= 2:
                        d_end = found_dates[1].date()
                        today = datetime.now(self.tz).date()
                        if d_end > today:
                            self._send_long(
                                user_id=user_id,
                                chat_id=chat_id,
                                text=f"Дата конца не может быть позже текущей даты ({today.strftime('%d.%m.%Y')}).",
                                fmt="HTML",
                            )
                            return
                        if d_end < d_start:
                            self._send_long(
                                user_id=user_id,
                                chat_id=chat_id,
                                text="Дата конца не может быть раньше даты начала. Введите даты корректно.",
                                fmt="HTML",
                            )
                            return
                        for key_try in actor_keys:
                            asyncio.run(self.engine.storage.state_set(key_try, ""))
                        self._subscribe_chat(chat_id)
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text=(
                                f"Формирую Excel-отчёт за период {d_start.strftime('%d.%m.%Y')}–{d_end.strftime('%d.%m.%Y')}…"
                            ),
                            fmt="HTML",
                        )
                        self._refresh_sources_before_manual()
                        start_local = datetime(d_start.year, d_start.month, d_start.day, 0, 0, 0, tzinfo=self.tz)
                        end_local = datetime(d_end.year, d_end.month, d_end.day, 23, 59, 59, tzinfo=self.tz)
                        path = asyncio.run(
                            self.engine.build_excel_for_period(
                                self.project_code,
                                start_utc=start_local.astimezone(timezone.utc),
                                end_utc=end_local.astimezone(timezone.utc),
                            )
                        )
                        self.client.send_file(user_id=user_id, chat_id=chat_id, file_path=path.as_posix())
                        return

                    st = {"step": "await_end", "start_date": d_start.strftime("%d.%m.%Y")}
                    for key_try in actor_keys:
                        asyncio.run(self.engine.storage.state_set(key_try, json.dumps(st, ensure_ascii=False)))
                    self._send_long(
                        user_id=user_id,
                        chat_id=chat_id,
                        text="Введите дату конца периода в формате ДД.ММ.ГГГГ.",
                        fmt="HTML",
                    )
                    return
                if step == "await_end":
                    dt_end = self._parse_ru_date(cmd)
                    if not dt_end:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text="Неверный формат даты. Введите дату конца в формате ДД.ММ.ГГГГ.",
                            fmt="HTML",
                        )
                        return
                    try:
                        dt_start = datetime.strptime(str(st.get("start_date") or ""), "%d.%m.%Y")
                    except Exception:
                        for key_try in actor_keys:
                            asyncio.run(self.engine.storage.state_set(key_try, ""))
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text="Сессия выбора периода сброшена. Нажмите «Получить отчёт в Excel за период» заново.",
                            with_keyboard=True,
                            fmt="HTML",
                        )
                        return
                    d_start = dt_start.date()
                    d_end = dt_end.date()
                    today = datetime.now(self.tz).date()
                    if d_end < self.min_period_date:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text=(
                                f"Дата начала мониторинга доступна не раньше {self.min_period_date.strftime('%d.%m.%Y')}."
                            ),
                            fmt="HTML",
                        )
                        return
                    if d_end > today:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text=f"Дата конца не может быть позже текущей даты ({today.strftime('%d.%m.%Y')}).",
                            fmt="HTML",
                        )
                        return
                    if d_end < d_start:
                        self._send_long(
                            user_id=user_id,
                            chat_id=chat_id,
                            text="Дата конца не может быть раньше даты начала. Введите дату конца ещё раз.",
                            fmt="HTML",
                        )
                        return
                    for key_try in actor_keys:
                        asyncio.run(self.engine.storage.state_set(key_try, ""))
                    self._subscribe_chat(chat_id)
                    self._send_long(
                        user_id=user_id,
                        chat_id=chat_id,
                        text=(
                            f"Формирую Excel-отчёт за период {d_start.strftime('%d.%m.%Y')}–{d_end.strftime('%d.%m.%Y')}…"
                        ),
                        fmt="HTML",
                    )
                    self._refresh_sources_before_manual()
                    start_local = datetime(d_start.year, d_start.month, d_start.day, 0, 0, 0, tzinfo=self.tz)
                    end_local = datetime(d_end.year, d_end.month, d_end.day, 23, 59, 59, tzinfo=self.tz)
                    path = asyncio.run(
                        self.engine.build_excel_for_period(
                            self.project_code,
                            start_utc=start_local.astimezone(timezone.utc),
                            end_utc=end_local.astimezone(timezone.utc),
                        )
                    )
                    self.client.send_file(user_id=user_id, chat_id=chat_id, file_path=path.as_posix())
                    return

        direct_dates = _extract_ru_dates(cmd)
        if len(direct_dates) >= 2 and (user_id is not None or chat_id is not None):
            d_start = direct_dates[0].date()
            d_end = direct_dates[1].date()
            today = datetime.now(self.tz).date()
            if d_start < self.min_period_date:
                self._send_long(
                    user_id=user_id,
                    chat_id=chat_id,
                    text=(f"Дата начала мониторинга доступна не раньше {self.min_period_date.strftime('%d.%m.%Y')}."),
                    fmt="HTML",
                )
                return
            if d_end > today:
                self._send_long(
                    user_id=user_id,
                    chat_id=chat_id,
                    text=f"Дата конца не может быть позже текущей даты ({today.strftime('%d.%m.%Y')}).",
                    fmt="HTML",
                )
                return
            if d_end < d_start:
                self._send_long(
                    user_id=user_id,
                    chat_id=chat_id,
                    text="Дата конца не может быть раньше даты начала. Введите даты корректно.",
                    fmt="HTML",
                )
                return
            for key_try in actor_keys:
                asyncio.run(self.engine.storage.state_set(key_try, ""))
            self._subscribe_chat(chat_id)
            self._send_long(
                user_id=user_id,
                chat_id=chat_id,
                text=(f"Формирую Excel-отчёт за период {d_start.strftime('%d.%m.%Y')}–{d_end.strftime('%d.%m.%Y')}…"),
                fmt="HTML",
            )
            self._refresh_sources_before_manual()
            start_local = datetime(d_start.year, d_start.month, d_start.day, 0, 0, 0, tzinfo=self.tz)
            end_local = datetime(d_end.year, d_end.month, d_end.day, 23, 59, 59, tzinfo=self.tz)
            path = asyncio.run(
                self.engine.build_excel_for_period(
                    self.project_code,
                    start_utc=start_local.astimezone(timezone.utc),
                    end_utc=end_local.astimezone(timezone.utc),
                )
            )
            self.client.send_file(user_id=user_id, chat_id=chat_id, file_path=path.as_posix())
            return

        if c in monitor_cmds:
            self._subscribe_chat(chat_id)
            self._send_long(
                user_id=user_id,
                chat_id=chat_id,
                text="Пожалуйста, подождите, формирую мониторинг…",
                fmt="HTML",
            )
            self._refresh_sources_before_manual()
            text = asyncio.run(self.engine.build_digest(self.project_code))
            self._send_long(user_id=user_id, chat_id=chat_id, text=text, with_keyboard=True, fmt="HTML")
            return

        if c in excel_week_cmds:
            self._subscribe_chat(chat_id)
            self._send_long(user_id=user_id, chat_id=chat_id, text="Формирую Excel-отчёт за прошедшую неделю…", fmt="HTML")
            self._refresh_sources_before_manual()
            path = asyncio.run(self.engine.build_excel(self.project_code))
            self.client.send_file(user_id=user_id, chat_id=chat_id, file_path=path.as_posix())
            return

        if c in excel_period_cmds:
            self._subscribe_chat(chat_id)
            if user_id is not None or chat_id is not None:
                for key_try in actor_keys:
                    asyncio.run(
                        self.engine.storage.state_set(
                            key_try,
                            json.dumps({"step": "await_start"}, ensure_ascii=False),
                        )
                    )
            self._send_long(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "Введите дату начала периода в формате ДД.ММ.ГГГГ.\n"
                    f"Дата начала мониторинга доступна не раньше {self.min_period_date.strftime('%d.%m.%Y')}."
                ),
                fmt="HTML",
            )
            return
        if "excel" in c and "период" in c:
            self._subscribe_chat(chat_id)
            if user_id is not None or chat_id is not None:
                for key_try in actor_keys:
                    asyncio.run(
                        self.engine.storage.state_set(
                            key_try,
                            json.dumps({"step": "await_start"}, ensure_ascii=False),
                        )
                    )
            self._send_long(
                user_id=user_id,
                chat_id=chat_id,
                text=(
                    "Введите дату начала периода в формате ДД.ММ.ГГГГ.\n"
                    f"Дата начала мониторинга доступна не раньше {self.min_period_date.strftime('%d.%m.%Y')}."
                ),
                fmt="HTML",
            )
            return

        self._send_long(
            user_id=user_id,
            chat_id=chat_id,
            text="Команда не распознана. Нажмите «Получить мониторинг».",
            with_keyboard=True,
            fmt="HTML",
        )

    def run(self) -> None:
        marker = asyncio.run(self.engine.storage.state_get(self.state_key_marker, ""))
        print(f"[max] start project={self.project_code} marker={bool(marker)}")
        while True:
            try:
                updates, new_marker = self.client.get_updates(
                    marker=marker,
                    limit=self.updates_limit,
                    timeout=self.updates_timeout,
                    types=self.update_types,
                )
                if new_marker and new_marker != marker:
                    marker = new_marker
                    asyncio.run(self.engine.storage.state_set(self.state_key_marker, marker))
                for upd in updates:
                    try:
                        raw_update_id = str(upd.get("update_id") or upd.get("id") or "").strip()
                        if raw_update_id and self._register_recent(self._recent_update_ids, raw_update_id, ttl_sec=8.0):
                            continue
                        utype = (upd.get("update_type") or upd.get("type") or "").strip().lower()
                        text = _extract_callback_command(upd) if utype == "message_callback" else _extract_text(upd, utype)
                        chat_id = _extract_chat_id(upd)
                        user_id = _extract_user_id(upd)
                        if chat_id == 0:
                            chat_id = None
                        if user_id == 0:
                            user_id = None
                        callback_id = _extract_callback_id(upd) if utype == "message_callback" else ""
                        if utype in {"bot_started"}:
                            self._handle_command(cmd="/start", user_id=user_id, chat_id=chat_id, callback_id=callback_id)
                            continue
                        if not user_id and not chat_id:
                            continue
                        if not text:
                            # Без команды с callback просто обновляем клавиатуру, чтобы пользователь видел реакцию.
                            if utype == "message_callback":
                                self._send_long(
                                    user_id=user_id,
                                    chat_id=chat_id,
                                    text="Команда кнопки не распознана. Нажмите кнопку ещё раз.",
                                    with_keyboard=True,
                                    fmt="HTML",
                                )
                            continue
                        self._handle_command(cmd=text, user_id=user_id, chat_id=chat_id, callback_id=callback_id)
                    except Exception as upd_exc:
                        print(f"[max] update error: {type(upd_exc).__name__}: {upd_exc}")
                        continue
                self._maybe_scheduled_dispatch()
            except Exception as exc:
                print(f"[max] poll error: {type(exc).__name__}: {exc}")
                if "invalid chatid: 0" in str(exc).lower():
                    marker = ""
                    try:
                        asyncio.run(self.engine.storage.state_set(self.state_key_marker, marker))
                        print("[max] marker reset due invalid chatId=0")
                    except Exception:
                        pass
                time.sleep(2)


def main() -> None:
    bot = ForumMaxBot()
    bot.run()


if __name__ == "__main__":
    main()
