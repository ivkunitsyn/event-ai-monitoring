from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

from .utils import collapse_spaces, strip_allcaps_prefix, to_sentence


_LEAD_SYSTEM = (
    "Ты редактор мониторинга. Сформулируй один аккуратный и законченный лид на русском. "
    "Только факты из входного текста, без домыслов. "
    "Максимум 220 символов. Без обрывков. Верни только готовое предложение."
)

_FORUM_FOCUS_RE = re.compile(
    r"(?iu)\b(форум|киф|внот|рэн|путешествуй|космическ|энергетическ|охраны труда)\b"
)

_JUNK_RE = re.compile(
    r"(?iu)\b(поиск дешевых авиабилетов|горящие туры|бронирование отел[ея]|туризм, путевки|читать на площадке)\b"
)
_SOCIAL_SPAM_RE = re.compile(
    r"(?iu)\b(youtube|instagram|inslagram|последние\s+новости\s+г2|душанбеводоканал|t\.me/niatkhovar)\b"
)
_SOCIAL_TAIL_RE = re.compile(
    r"(?iu)\b(амит\s*«?ховар»?|статья|новости\s+душанбе|события\s+таджикистана\s+сегодня)\b"
)
_WEAK_LEAD_RE = re.compile(
    r"(?iu)^(в\s+округе|вниманию\s+руководител|магнит\s+для\s+бизнеса|что\s+такое|ранее\s+сообщалось|в\s+конце\s+апреля|подготовка\s+к)\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\s+[•·]\s+")


def _dedupe_repeated_fragment(text: str) -> str:
    s = text or ""
    # Убираем подряд идущий повтор длинного фрагмента.
    s = re.sub(r"(?iu)^(.{8,140}?)\s+\1(\b|[,.!?:;])", r"\1\2", s)
    # Повтор темы с тире, например "... пройдёт в Москве ... - АМИТ «Ховар»"
    s = re.sub(r"(?iu)(международн\w*\s+туристическ\w*\s+форум\w*[^.]{0,140}?)(?:\s*-\s*[^.]{1,120})$", r"\1", s)
    return s


def _lead_has_forum_focus(text: str) -> bool:
    return bool(_FORUM_FOCUS_RE.search(text or ""))


def _looks_like_bad_lead(text: str) -> bool:
    s = collapse_spaces(text or "")
    if not s:
        return True
    if s.startswith("resp_"):
        return True
    if "http://" in s or "https://" in s:
        return True
    if _JUNK_RE.search(s) or _SOCIAL_SPAM_RE.search(s):
        return True
    if _WEAK_LEAD_RE.search(s) and not _lead_has_forum_focus(s):
        return True
    words = s.split()
    if len(words) < 4:
        return True
    if len(s) < 32 and not _lead_has_forum_focus(s):
        return True
    if re.fullmatch(r"(?iu)[а-яёa-z0-9 -]{1,28}", s) and not _lead_has_forum_focus(s):
        return True
    if re.search(r"(?iu)^(.{4,80}?)\s+\1$", s):
        return True
    return False


def _best_sentence(text: str) -> str:
    raw = collapse_spaces(text or "")
    if not raw:
        return ""
    candidates = [collapse_spaces(x) for x in _SENTENCE_SPLIT_RE.split(raw) if collapse_spaces(x)]
    best = ""
    best_score = -10_000
    for part in candidates:
        score = 0
        if len(part) < 24:
            score -= 30
        if len(part) > 240:
            score -= 10
        if _lead_has_forum_focus(part):
            score += 40
        if _WEAK_LEAD_RE.search(part):
            score -= 18
        if "http://" in part or "https://" in part:
            score -= 25
        if _JUNK_RE.search(part) or _SOCIAL_SPAM_RE.search(part):
            score -= 25
        score += min(len(part), 180) // 6
        if re.search(r"(?iu)\b(пройдет|пройд[её]т|состоится|открыта|открыт|стартует|регистрация|форум)\b", part):
            score += 12
        if score > best_score:
            best_score = score
            best = part
    return best


def _fix_mojibake(text: str) -> str:
    s = text or ""
    if not s:
        return s
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


def _cleanup_ru_punct(text: str) -> str:
    s = text or ""
    s = re.sub(r'\s*"\s*([^"]+?)\s*"\s*', r" «\1» ", s)
    s = re.sub(r"«\s+", "«", s)
    s = re.sub(r"\s+»", "»", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return collapse_spaces(s)


def _normalize_common_forum_patterns(text: str) -> str:
    s = text or ""
    s = re.sub(
        r"(?iu)^подготовка к [^.]{0,120}?\bпоявилась новая рубрика, посвященн[а-я]+\s+подготовке [^.]{0,160}?(кавказск\w+\s+инвестиц\w+\s+форум\w+)",
        r"В сообществе появилась новая рубрика, посвященная подготовке к \1",
        s,
    )
    s = re.sub(
        r"(?iu)^в минеральных водах уже на следующей неделе стартует ([^.]{0,120}?форум\w*)\s+новые даты проведения\s+([^.]{0,120}?форум\w+)\s+были объявлены организаторами",
        r"В Минеральных Водах уже на следующей неделе стартует \1",
        s,
    )
    s = re.sub(
        r"(?iu)^открыта онлайн-регистрация(?:\s+сми)?[^.]{0,220}?\bна\s+(кавказск\w+\s+инвестиц\w+\s+форум\w+(?:\s*-\s*2026)?)\b.*",
        r"Открыта онлайн-регистрация представителей СМИ и блогосферы на \1",
        s,
    )
    if re.search(r"(?iu)^открыта онлайн-регистрация", s) and re.search(r"(?iu)средств\s+массовой\s+информации\s+и\s+блогосферы", s):
        s = "Открыта онлайн-регистрация представителей СМИ и блогосферы на Кавказский инвестиционный форум"
    return collapse_spaces(s)


@dataclass
class LeadEditor:
    enabled: bool
    model_simple: str
    model_complex: str
    api_key: str
    proxy: str = ""
    base_url: str = ""
    http_referer: str = ""
    x_title: str = ""

    def __post_init__(self) -> None:
        self._client: OpenAI | None = None
        if not self.enabled or not self.api_key or OpenAI is None:
            return
        try:
            default_headers: dict[str, str] = {}
            if self.http_referer:
                default_headers["HTTP-Referer"] = self.http_referer
            if self.x_title:
                default_headers["X-Title"] = self.x_title
            if httpx is not None:
                kwargs: dict[str, Any] = {"timeout": 8}
                if self.proxy:
                    kwargs["proxy"] = self.proxy
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=(self.base_url or None),
                    default_headers=(default_headers or None),
                    http_client=httpx.Client(**kwargs),
                )
            else:
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=(self.base_url or None),
                    default_headers=(default_headers or None),
                )
        except Exception:
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def needs_rewrite(self, lead: str) -> bool:
        s = collapse_spaces(lead or "")
        if not s:
            return True
        if s.startswith("resp_"):
            return True
        if re.search(r"(?iu)\bновые сообщения в отчете\b", s):
            return True
        if "читать на площадке" in s.lower():
            return True
        if "…" in s:
            return True
        if re.search(r"\b[A-ZА-ЯЁ]{6,}\b", s):
            return True
        if len(s.split()) < 4:
            return True
        if not re.search(r"[.!?…]$", s):
            return True
        return False

    def _call(self, model: str, source_text: str, source_kind: str) -> str:
        if not self._client:
            raise RuntimeError("openai client disabled")
        prompt = "Сделай один завершенный лид. Не обрывай предложение. "
        if source_kind == "media":
            prompt += (
                "Если заголовок слишком общий или короткий, опирайся на описание и текст, "
                "чтобы получить конкретный и понятный факт о форуме. "
                "Не дублируй короткий заголовок как есть. "
            )
        else:
            prompt += (
                "Сожми пост до одного главного факта о форуме без канцелярита и без повтора исходной формулировки. "
            )
        prompt += (
            "Если вход слишком длинный — возьми главный факт и закончи его грамматически.\n\n"
            f"Текст:\n{source_text.strip()}"
        )
        resp = self._client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": _LEAD_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_output_tokens=220,
        )

        text = (resp.output_text or "").strip()
        if not text:
            raise RuntimeError("empty output_text")
        clean = text.strip().strip("`")
        if clean.startswith("resp_"):
            raise RuntimeError("invalid response id placeholder")
        try:
            parsed = json.loads(clean)
            lead = str(parsed.get("lead") or parsed.get("text") or "").strip()
            if lead:
                return lead
        except Exception:
            pass

        return clean

    def make_lead(self, item: dict[str, Any], source_kind: str, allow_openai: bool = True) -> str:
        if source_kind == "social":
            seed_parts = [
                str(item.get("title") or ""),
                str(item.get("description") or ""),
                str(item.get("text_clean") or ""),
            ]
        else:
            seed_parts = [
                str(item.get("title") or ""),
                str(item.get("description") or ""),
                str(item.get("text_raw") or ""),
                str(item.get("text_clean") or ""),
            ]
        base_text = collapse_spaces(" ".join(seed_parts))
        base_text = re.sub(r"(?iu)\bплощадка:\s*[^|]+\|\s*написал\s*[^.]+", " ", base_text)
        base_text = re.sub(r"(?iu)\bаудитория\s+автора:\s*[^.]+", " ", base_text)
        base_text = re.sub(r"(?iu)\bвы\s+получили\s+это\s+письмо\b.*$", " ", base_text)
        base_text = re.sub(r"https?://\\S+", " ", base_text)
        base_text = re.sub(r"\s+\([^)]*https?://[^)]*\)", "", base_text)
        base_text = strip_allcaps_prefix(base_text)
        base_text = collapse_spaces(base_text)

        if allow_openai and self.available:
            for model in dict.fromkeys((self.model_simple, self.model_complex)):
                try:
                    lead = self._call(model=model, source_text=base_text[:2400], source_kind=source_kind)
                    lead = self._postprocess(lead)
                    if lead:
                        return lead
                except Exception:
                    continue

        return self._fallback(base_text, source_kind=source_kind)

    def _postprocess(self, lead: str) -> str:
        s = collapse_spaces(_fix_mojibake(lead))
        s = strip_allcaps_prefix(s)
        s = re.sub(r"^(?:[•·\-\s]+)+", "", s)
        s = re.sub(r"^[^А-Яа-яA-Za-z0-9]+", "", s)
        s = re.sub(r"(?iu)^(?:ru|en|fr|es)\s*(?:\([^)]{0,160}\))?\s*", "", s)
        s = re.sub(r"(?iu)^\((?:en|fr|es|de)[^)]{0,160}\)\s*", "", s)
        s = re.sub(r"^\s*\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*", "", s)
        s = re.sub(r"(?iu)^в публикации сообщается, что\s+", "", s)
        s = re.sub(r"(?iu)^источник\s*[—:-]\s*", "", s)
        s = re.sub(r"\s+\([^)]*https?://[^)]*\)", "", s)
        s = re.sub(r"https?://\\S+", "", s)
        s = re.sub(r"\b(?:web\.)?telegram\.org/\S+", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\b[a-z0-9.-]+\.[a-z]{2,}/\S*", "", s, flags=re.IGNORECASE)
        s = _SOCIAL_SPAM_RE.sub(" ", s)
        s = _SOCIAL_TAIL_RE.sub(" ", s)
        s = re.sub(r"(?:\.\s*){2,}", "… ", s)
        s = _JUNK_RE.sub(" ", s)
        s = _dedupe_repeated_fragment(s)
        s = _cleanup_ru_punct(s)
        s = _normalize_common_forum_patterns(s)
        s = re.sub(r"(?iu)\bмеждународный туристический форум «?путешествуй!?»?\s+пройд[её]т в москве\b.*$", "Международный туристический форум «Путешествуй!» пройдёт в Москве", s)
        s = re.sub(r"(?iu)\bв\s+г\b(?:\s*[.,:;!?…])?\s*$", "", s).strip(" ,;:-")
        s = s.strip(" -–—")
        if not s or s.startswith("resp_"):
            return ""
        parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", s) if p.strip()]
        focus_parts = [p for p in parts if _FORUM_FOCUS_RE.search(p or "")]
        if focus_parts:
            s = focus_parts[0]
        s = to_sentence(s, max_len=220)
        if _looks_like_bad_lead(s):
            return ""
        return s

    def _fallback(self, text: str, source_kind: str) -> str:
        s = collapse_spaces(_fix_mojibake(text))
        s = strip_allcaps_prefix(s)
        s = re.sub(r"^(?:[•·\-\s]+)+", "", s)
        s = re.sub(r"^[^А-Яа-яA-Za-z0-9]+", "", s)
        s = re.sub(r"(?iu)^(?:ru|en|fr|es)\s*(?:\([^)]{0,160}\))?\s*", "", s)
        s = re.sub(r"(?iu)^\((?:en|fr|es|de)[^)]{0,160}\)\s*", "", s)
        s = re.sub(r"^\s*\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*", "", s)
        if source_kind == "social":
            s = re.sub(r"(?iu)^в публикации сообщается, что\s+", "", s)
            s = re.sub(r"\b(?:web\.)?telegram\.org/\S+", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\b[a-z0-9.-]+\.[a-z]{2,}/\S*", "", s, flags=re.IGNORECASE)
            s = _SOCIAL_TAIL_RE.sub(" ", s)
        s = _SOCIAL_SPAM_RE.sub(" ", s)
        s = re.sub(r"(?:\.\s*){2,}", "… ", s)
        s = _JUNK_RE.sub(" ", s)
        s = _dedupe_repeated_fragment(s)
        s = _cleanup_ru_punct(s)
        s = _normalize_common_forum_patterns(s)
        s = re.sub(r"(?iu)\bв\s+г\b(?:\s*[.,:;!?…])?\s*$", "", s).strip(" ,;:-")

        best = _best_sentence(s)
        if best:
            best = to_sentence(best, max_len=220)
            if not _looks_like_bad_lead(best):
                return best

        full = to_sentence(s, max_len=220)
        if _looks_like_bad_lead(full):
            return "Публикация по теме форума."
        return full
