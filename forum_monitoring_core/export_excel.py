from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .utils import parse_any_dt


def _fmt_dt(raw: object) -> str:
    dt = parse_any_dt(raw)
    if not dt:
        return ""
    return dt.strftime("%d.%m.%Y %H:%M")


def build_project_excel(
    project_name: str,
    media_items: list[dict],
    social_items: list[dict],
    output_path: Path,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()

    ws_media = wb.active
    ws_media.title = "СМИ"
    media_headers = [
        "Дата публикации",
        "Дата попадания в мониторинг",
        "Источник",
        "Заголовок",
        "Ссылка",
    ]
    ws_media.append(media_headers)
    for c in ws_media[1]:
        c.font = Font(bold=True)

    for item in sorted(media_items, key=lambda x: parse_any_dt(x.get("published_at")) or datetime.min, reverse=True):
        ws_media.append(
            [
                _fmt_dt(item.get("published_at")),
                _fmt_dt(item.get("last_seen_at") or item.get("updated_at")),
                str(item.get("source_title") or ""),
                str(item.get("title") or ""),
                str(item.get("canonical_url") or item.get("link") or ""),
            ]
        )

    ws_social = wb.create_sheet("Социальные сети")
    social_headers = [
        "Дата публикации",
        "Дата попадания в мониторинг",
        "Платформа",
        "Источник / автор",
        "Текст / лид / фрагмент",
        "Подписчики / аудитория",
        "Ссылка на пост",
    ]
    ws_social.append(social_headers)
    for c in ws_social[1]:
        c.font = Font(bold=True)

    for item in sorted(social_items, key=lambda x: parse_any_dt(x.get("published_at")) or datetime.min, reverse=True):
        ws_social.append(
            [
                _fmt_dt(item.get("published_at")),
                _fmt_dt(item.get("ingested_at") or item.get("updated_at")),
                str(item.get("platform") or ""),
                str(item.get("author_name") or item.get("source") or ""),
                str(item.get("lead_clean") or item.get("text_clean") or ""),
                int(item.get("audience") or 0),
                str(item.get("canonical_url") or item.get("url") or ""),
            ]
        )

    for ws in (ws_media, ws_social):
        ws.freeze_panes = "A2"
        widths = {
            1: 20,
            2: 24,
            3: 36,
            4: 80,
            5: 65,
            6: 20,
            7: 65,
        }
        for idx, width in widths.items():
            ws.column_dimensions[chr(ord("A") + idx - 1)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
