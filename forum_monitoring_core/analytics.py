from __future__ import annotations

import json
import re
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

from .grouping import GroupedTopic
from .utils import normalize_for_match, to_sentence


RISK_HINTS = (
    "скандал",
    "срыв",
    "отмена",
    "перенос",
    "перенес",
    "перенесен",
    "перенесена",
    "перенесли",
    "критик",
    "жалоб",
    "негатив",
    "проблем",
    "конфликт",
    "не состоится",
    "не готов",
    "низк",
)

OPP_HINTS = (
    "подписал",
    "соглашени",
    "участи",
    "старт",
    "запуск",
    "инвести",
    "делегац",
    "поддержк",
    "преми",
    "регистрац",
    "прием заяв",
    "прием заявок",
    "делов",
    "программ",
    "секци",
    "участник",
    "волонтер",
)


_ANALYTICS_SYSTEM = (
    "Ты аналитик пресс-службы форума. "
    "Сформируй краткий и прикладной блок: коммуникационные риски и коммуникационные возможности. "
    "Пиши только по входным данным, без домыслов. "
    "Каждый пункт — законченное предложение на русском. "
    "Не повторяй одинаковые формулировки."
)


def _extract_response_text(resp: Any) -> str:
    out = str(getattr(resp, "output_text", "") or "").strip()
    if out:
        return out

    # Резервное извлечение из структуры output/content.
    chunks: list[str] = []
    output = getattr(resp, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for c in content:
                    txt = str(getattr(c, "text", "") or "").strip()
                    if txt:
                        chunks.append(txt)
            elif isinstance(content, str) and content.strip():
                chunks.append(content.strip())
            txt2 = str(getattr(item, "text", "") or "").strip()
            if txt2:
                chunks.append(txt2)
    if chunks:
        return "\n".join(chunks).strip()
    return ""


def _signal_from_text(text: str) -> tuple[str, str, str]:
    s = normalize_for_match(text)
    risk_hits = sum(1 for x in RISK_HINTS if x in s)
    opp_hits = sum(1 for x in OPP_HINTS if x in s)

    if risk_hits > opp_hits and risk_hits >= 1:
        lvl = "high" if risk_hits >= 3 else "medium" if risk_hits == 2 else "low"
        return "risk", lvl, "none"
    if opp_hits > risk_hits and opp_hits >= 1:
        lvl = "high" if opp_hits >= 3 else "medium" if opp_hits == 2 else "low"
        return "opportunity", "none", lvl
    return "neutral", "none", "none"


def _strip_json_fences(s: str) -> str:
    t = (s or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _normalize_llm_level(v: str, kind: str) -> str:
    s = normalize_for_match(v)
    if "high" in s or "выс" in s:
        return "high"
    if "med" in s or "сред" in s:
        return "medium"
    if "low" in s or "низ" in s:
        return "low"
    return "low" if kind == "risk" else "medium"


def _parse_plain_analytics(raw_text: str) -> dict[str, Any] | None:
    t = _strip_json_fences(raw_text or "")
    if not t:
        return None
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if not lines:
        return None

    overall = "neutral"
    risks: list[dict[str, str]] = []
    opps: list[dict[str, str]] = []
    mode = ""
    for ln in lines:
        low = normalize_for_match(ln)
        if low.startswith("оценка:") or low.startswith("overall:"):
            if any(x in low for x in ("tense", "напр", "крит")):
                overall = "tense"
            elif "mixed" in low or "смеш" in low:
                overall = "mixed"
            elif "moderately_positive" in low or "умеренно" in low:
                overall = "moderately_positive"
            elif "positive" in low or "позит" in low:
                overall = "positive"
            else:
                overall = "neutral"
            continue
        if low.startswith("риски"):
            mode = "risk"
            continue
        if low.startswith("возможности"):
            mode = "opp"
            continue
        if ln.startswith(("-", "•")):
            text = ln.lstrip("-• ").strip()
            if not text:
                continue
            if mode == "risk":
                risks.append({"title": text, "why_it_matters": "", "urgency": "medium"})
            elif mode == "opp":
                opps.append({"title": text, "why_it_matters": "", "priority": "medium"})

    if not risks and not opps:
        return None
    return {"overall_assessment": overall, "risks": risks[:3], "opportunities": opps[:3]}


def _call_openai_analytics(
    *,
    project_name: str,
    media_daily_count: int,
    social_daily_count: int,
    topics: list[GroupedTopic],
    api_key: str,
    proxy: str,
    model: str,
    base_url: str = "",
    http_referer: str = "",
    x_title: str = "",
) -> dict[str, Any] | None:
    if not api_key or OpenAI is None:
        print("[analytics] openai unavailable -> fallback")
        return None
    req_model = (model or "gpt-5-mini").strip()
    # На текущем прокси gpt-5-* часто возвращает пустой output_text для structured-аналитики.
    # Для стабильности и качества аналитического блока используем проверенный fallback-модель.
    if req_model.startswith("gpt-5"):
        req_model = "gpt-4.1"

    try:
        default_headers: dict[str, str] = {}
        if http_referer:
            default_headers["HTTP-Referer"] = http_referer
        if x_title:
            default_headers["X-Title"] = x_title
        if httpx is not None:
            kwargs: dict[str, Any] = {"timeout": 45}
            if proxy:
                kwargs["proxy"] = proxy
            client = OpenAI(
                api_key=api_key,
                base_url=(base_url or None),
                default_headers=(default_headers or None),
                http_client=httpx.Client(**kwargs),
            )
        else:
            client = OpenAI(
                api_key=api_key,
                base_url=(base_url or None),
                default_headers=(default_headers or None),
            )
    except Exception:
        print("[analytics] openai client init failed -> fallback")
        return None

    compact_topics = []
    for t in topics[:8]:
        lead = str((t.representative or {}).get("lead_clean") or "").strip()
        if not lead:
            continue
        compact_topics.append(
            {
                "kind": t.source_kind,
                "lead": lead[:180],
            }
        )

    user_prompt = (
        f"Форум: {project_name}\n"
        f"Сообщений за сутки в СМИ: {media_daily_count}\n"
        f"Сообщений за сутки в соцсетях: {social_daily_count}\n\n"
        "Темы выпуска:\n"
        f"{json.dumps(compact_topics, ensure_ascii=False)}\n\n"
        "Правила:\n"
        "1) Дай до 3 рисков и до 3 возможностей.\n"
        "2) Если суммарно за сутки <=3 сообщений, обязательно укажи риск низкой видимости и возможность активизировать продвижение.\n"
        "3) Если в СМИ 0, а соцсети >0, укажи риск дисбаланса каналов и возможность усилить выход в СМИ.\n"
        "4) Не копируй лиды как есть; делай управленческие формулировки.\n"
        "5) Без воды, без повторов.\n\n"
        "Верни только JSON:\n"
        "{\n"
        '  "overall_assessment":"positive|moderately_positive|neutral|mixed|tense",\n'
        '  "risks":[{"title":"","why_it_matters":"","urgency":"low|medium|high"}],\n'
        '  "opportunities":[{"title":"","why_it_matters":"","priority":"low|medium|high"}]\n'
        "}"
    )

    def _try_parse(raw_text: str) -> dict[str, Any] | None:
        raw_local = _strip_json_fences(raw_text)
        if not raw_local or raw_local.startswith("resp_"):
            return None
        if not raw_local.lstrip().startswith("{"):
            m = re.search(r"\{[\s\S]*\}", raw_local)
            if m:
                raw_local = m.group(0).strip()
        try:
            data_local = json.loads(raw_local)
        except Exception:
            return None
        return data_local if isinstance(data_local, dict) else None

    raw = ""
    used_method = ""
    data: dict[str, Any] | None = None
    try:
        resp = client.responses.create(
            model=req_model,
            input=[
                {"role": "system", "content": _ANALYTICS_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=700,
        )
        raw = _strip_json_fences(_extract_response_text(resp))
        if raw and not raw.startswith("resp_"):
            used_method = "responses"
            data = _try_parse(raw)
    except Exception as exc:
        print(f"[analytics] responses json call failed: {type(exc).__name__}: {exc}")
        raw = ""

    if not data:
        # Резервный путь через chat.completions в JSON Schema режиме.
        try:
            resp2 = client.chat.completions.create(
                model=req_model,
                messages=[
                    {"role": "system", "content": _ANALYTICS_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "analytics_output",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "overall_assessment": {
                                    "type": "string",
                                    "enum": ["positive", "moderately_positive", "neutral", "mixed", "tense"],
                                },
                                "risks": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string"},
                                            "why_it_matters": {"type": "string"},
                                            "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
                                        },
                                        "required": ["title", "why_it_matters", "urgency"],
                                    },
                                },
                                "opportunities": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string"},
                                            "why_it_matters": {"type": "string"},
                                            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                                        },
                                        "required": ["title", "why_it_matters", "priority"],
                                    },
                                },
                            },
                            "required": ["overall_assessment", "risks", "opportunities"],
                        },
                    },
                },
                max_completion_tokens=500,
            )
            raw = (
                str(resp2.choices[0].message.content or "").strip()
                if getattr(resp2, "choices", None)
                else ""
            )
            raw = _strip_json_fences(raw)
            if raw:
                used_method = "chat.completions"
                data = _try_parse(raw)
        except Exception as exc:
            print(f"[analytics] chat.completions json call failed: {type(exc).__name__}: {exc}")
            raw = ""

    if not data:
        # Третий путь: короткий текстовый формат с простым парсером.
        try:
            resp3 = client.responses.create(
                model=req_model,
                input=[
                    {"role": "system", "content": _ANALYTICS_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            user_prompt
                            + "\n\nЕсли JSON не получается, верни только такой формат:\n"
                            + "Оценка: mixed\nРиски:\n- ...\nВозможности:\n- ...\n"
                        ),
                    },
                ],
                max_output_tokens=450,
            )
            raw = _extract_response_text(resp3)
            plain = _parse_plain_analytics(raw)
            if plain:
                data = plain
                used_method = "responses-plain"
        except Exception as exc:
            print(f"[analytics] responses plain call failed: {type(exc).__name__}: {exc}")
            pass

    if not data:
        preview = raw.replace("\n", " ")[:420] if raw else ""
        if preview:
            print(f"[analytics] openai json parse failed -> fallback preview={preview!r}")
        else:
            print("[analytics] openai empty output -> fallback")
        return None

    if not raw or raw.startswith("resp_"):
        print("[analytics] openai empty output -> fallback")
        return None

    out: dict[str, Any] = {
        "overall_assessment": str(data.get("overall_assessment") or "neutral").strip() or "neutral",
        "risks": [],
        "opportunities": [],
    }
    if out["overall_assessment"] not in {"positive", "moderately_positive", "neutral", "mixed", "tense"}:
        out["overall_assessment"] = "neutral"

    for x in (data.get("risks") or [])[:3]:
        if not isinstance(x, dict):
            continue
        title = str(x.get("title") or "").strip()
        why = str(x.get("why_it_matters") or "").strip()
        if not title:
            continue
        out["risks"].append(
            {
                "title": title,
                "why_it_matters": why,
                "urgency": _normalize_llm_level(str(x.get("urgency") or ""), "risk"),
            }
        )

    for x in (data.get("opportunities") or [])[:3]:
        if not isinstance(x, dict):
            continue
        title = str(x.get("title") or "").strip()
        why = str(x.get("why_it_matters") or "").strip()
        if not title:
            continue
        out["opportunities"].append(
            {
                "title": title,
                "why_it_matters": why,
                "priority": _normalize_llm_level(str(x.get("priority") or ""), "opportunity"),
            }
        )

    if used_method:
        print(f"[analytics] openai={used_method} model={req_model}")
    return out


def _add_risk(risks: list[dict[str, str]], seen: set[str], title: str, why: str, urgency: str) -> None:
    clean_title = to_sentence((title or "").strip(), max_len=200)
    key = normalize_for_match(clean_title)
    if not key or key in seen:
        return
    seen.add(key)
    risks.append(
        {
            "title": clean_title,
            "why_it_matters": why.strip(),
            "urgency": urgency,
        }
    )


def _add_opp(opps: list[dict[str, str]], seen: set[str], title: str, why: str, priority: str) -> None:
    clean_title = to_sentence((title or "").strip(), max_len=200)
    key = normalize_for_match(clean_title)
    if not key or key in seen:
        return
    seen.add(key)
    opps.append(
        {
            "title": clean_title,
            "why_it_matters": why.strip(),
            "priority": priority,
        }
    )


def analyze_topics(
    topics: list[GroupedTopic],
    *,
    media_daily_count: int = 0,
    social_daily_count: int = 0,
    project_name: str = "",
    use_openai: bool = False,
    openai_api_key: str = "",
    openai_proxy: str = "",
    openai_model: str = "",
    openai_base_url: str = "",
    openai_http_referer: str = "",
    openai_x_title: str = "",
) -> dict[str, Any]:
    llm_result: dict[str, Any] | None = None
    if use_openai and openai_api_key:
        llm_result = _call_openai_analytics(
            project_name=project_name or "Форум",
            media_daily_count=media_daily_count,
            social_daily_count=social_daily_count,
            topics=topics,
            api_key=openai_api_key,
            proxy=openai_proxy,
            model=openai_model or "gpt-5-mini",
            base_url=openai_base_url,
            http_referer=openai_http_referer,
            x_title=openai_x_title,
        )

    if llm_result:
        llm_risks = list(llm_result.get("risks") or [])
        llm_opps = list(llm_result.get("opportunities") or [])
        overall = str(llm_result.get("overall_assessment") or "neutral")
    else:
        llm_risks = []
        llm_opps = []
        overall = "neutral"

    risks: list[dict[str, str]] = []
    opps: list[dict[str, str]] = []
    risk_seen: set[str] = set()
    opp_seen: set[str] = set()
    total_daily = max(0, media_daily_count) + max(0, social_daily_count)

    for x in llm_risks[:3]:
        if isinstance(x, dict):
            _add_risk(
                risks,
                risk_seen,
                str(x.get("title") or ""),
                str(x.get("why_it_matters") or ""),
                _normalize_llm_level(str(x.get("urgency") or ""), "risk"),
            )
    for x in llm_opps[:3]:
        if isinstance(x, dict):
            _add_opp(
                opps,
                opp_seen,
                str(x.get("title") or ""),
                str(x.get("why_it_matters") or ""),
                _normalize_llm_level(str(x.get("priority") or ""), "opportunity"),
            )

    # Базовые коммуникационные выводы по суточной активности.
    if total_daily == 0:
        _add_risk(
            risks,
            risk_seen,
            "За последние сутки не зафиксировано упоминаний форума в СМИ и соцсетях.",
            "Низкая видимость повестки снижает узнаваемость форума и интерес аудитории.",
            "high",
        )
        _add_opp(
            opps,
            opp_seen,
            "Необходимо активизировать продвижение форума в СМИ и соцсетях.",
            "Рекомендуется усилить регулярные публикации: анонсы, спикеры, деловая программа, практические кейсы.",
            "high",
        )
    elif total_daily <= 3:
        _add_risk(
            risks,
            risk_seen,
            "Суточный объём упоминаний форума остаётся низким.",
            "Ограниченный информационный поток может ослаблять присутствие форума в публичной повестке.",
            "medium",
        )
        _add_opp(
            opps,
            opp_seen,
            "Есть потенциал для наращивания охвата через дополнительные инфоповоды.",
            "Имеет смысл усилить частоту и разнообразие публикаций по форумной повестке.",
            "medium",
        )

    if media_daily_count == 0 and social_daily_count > 0:
        _add_risk(
            risks,
            risk_seen,
            "За сутки нет публикаций в СМИ при наличии обсуждения в соцсетях.",
            "Дисбаланс каналов ограничивает охват деловой и институциональной аудитории.",
            "medium",
        )
        _add_opp(
            opps,
            opp_seen,
            "Текущие соцсетевые сюжеты можно конвертировать в публикации в СМИ.",
            "Подготовка комментариев, колонок и новостных поводов поможет выровнять медийное покрытие.",
            "medium",
        )

    if social_daily_count == 0 and media_daily_count > 0:
        _add_risk(
            risks,
            risk_seen,
            "За сутки нет релевантных упоминаний в соцсетях.",
            "Отсутствие соцсетевой дискуссии снижает вовлечённость и органическое распространение повестки.",
            "medium",
        )
        _add_opp(
            opps,
            opp_seen,
            "Публикации в СМИ можно усилить через соцсети официальных и партнёрских площадок.",
            "Кросс-постинг и короткие адаптации материалов увеличат вовлечённость аудитории.",
            "medium",
        )

    for topic in topics:
        lead = str(topic.representative.get("lead_clean") or "").strip()
        if not lead:
            continue
        signal, risk_level, opp_level = _signal_from_text(lead)
        lead_norm = normalize_for_match(lead)

        if any(x in lead_norm for x in ("перенос", "перенес", "перенесен", "перенесли", "отмена", "отмен")):
            _add_risk(
                risks,
                risk_seen,
                "Повестка о переносах и изменениях дат может снижать доверие к оргготовности форума.",
                "Такие сюжеты требуют проактивного разъяснения причин и текущего статуса подготовки.",
                "high" if any(x in lead_norm for x in ("отмена", "отмен")) else "medium",
            )

        if any(x in lead_norm for x in ("регистрац", "прием заяв", "прием заявок", "набор", "волонтер", "участник")):
            _add_opp(
                opps,
                opp_seen,
                "Сюжеты о регистрации и приёме заявок можно использовать для роста конверсии участия.",
                "Усиление call-to-action и дедлайнов помогает переводить интерес аудитории в заявки.",
                "high",
            )

        if any(x in lead_norm for x in ("программ", "делов", "секци", "тем")):
            _add_opp(
                opps,
                opp_seen,
                "Публикации о программе форума усиливают восприятие его практической ценности.",
                "Имеет смысл подсвечивать конкретные треки, спикеров и ожидаемые прикладные результаты.",
                "medium",
            )

        if any(x in lead_norm for x in ("губернатор", "правительств", "делегац", "поддержк", "мин")):
            _add_opp(
                opps,
                opp_seen,
                "Упоминания участия регионов и институтов власти можно использовать для усиления статуса форума.",
                "Такие сюжеты формируют доверие к площадке и повышают её репутационный вес.",
                "medium",
            )

        if signal == "risk" and len(risks) < 3:
            _add_risk(
                risks,
                risk_seen,
                "В суточном массиве присутствуют сюжеты с потенциально негативным коммуникационным эффектом.",
                "Нужен оперативный мониторинг реакции аудитории и проактивные разъяснения по чувствительным темам.",
                risk_level,
            )
        elif signal == "opportunity" and len(opps) < 3:
            _add_opp(
                opps,
                opp_seen,
                "В текущем массиве есть позитивные инфоповоды для усиления коммуникации форума.",
                "Рекомендуется использовать эти сюжеты в официальных каналах и партнёрских публикациях.",
                opp_level,
            )

    if not risks:
        risks = [
            {
                "title": "Явных значимых рисков в текущем массиве не выявлено.",
                "why_it_matters": "",
                "urgency": "low",
            }
        ]
    if not opps:
        opps = [
            {
                "title": "Явных выраженных возможностей в текущем массиве не выявлено.",
                "why_it_matters": "",
                "priority": "low",
            }
        ]

    overall = overall if overall in {"positive", "moderately_positive", "neutral", "mixed", "tense"} else "neutral"
    first_risk_default = bool(risks and risks[0]["title"].startswith("Явных"))
    first_opp_default = bool(opps and opps[0]["title"].startswith("Явных"))
    if total_daily == 0:
        overall = "tense"
    elif first_risk_default and not first_opp_default:
        overall = "moderately_positive"
    elif first_opp_default and not first_risk_default:
        overall = "mixed"
    elif not first_risk_default and not first_opp_default:
        overall = "mixed"

    return {
        "overall_assessment": overall,
        "risks": risks[:3],
        "opportunities": opps[:3],
    }
