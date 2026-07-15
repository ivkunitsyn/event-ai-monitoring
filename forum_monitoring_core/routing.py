from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ProjectConfig
from .utils import normalize_for_match


@dataclass
class RoutingResult:
    project_primary: str
    project_secondary: list[str]
    rule_match: str
    confidence: float
    is_noise: bool
    noise_reason: str


_KIF_CONTEXT_RE = re.compile(
    r"(?iu)\b(инвест|форум|кавказ|минвод|минеральн|росконгресс|эконом|делов)"
)
_KIF_FULL_RE = re.compile(r"(?iu)кавказск\w+\s+инвестиц\w+\s+форум\w+")
_KIF_ABBR_CTX_RE = re.compile(r"(?iu)\bкиф\b.*\b(кавказ|инвестиц|форум)\b|\b(кавказ|инвестиц|форум)\b.*\bкиф\b")
_VNOT_FULL_RE = re.compile(r"(?iu)всероссийск\w+\s+недел\w+\s+охран\w+\s+труд\w+")
_VNOT_ABBR_CTX_RE = re.compile(r"(?iu)\bвнот\b.*\b(охран\w+\s+труд\w+|недел\w+\s+охран\w+\s+труд\w+)\b|\b(охран\w+\s+труд\w+|недел\w+\s+охран\w+\s+труд\w+)\b.*\bвнот\b")
_REN_FULL_RE = re.compile(r"(?iu)российск\w+\s+энергетическ\w+\s+недел\w+|russian\s+energy\s+week")
_REN_ABBR_CTX_RE = re.compile(r"(?iu)\bрэн\b.*\b(энергетическ|недел)\b|\b(энергетическ|недел)\b.*\bрэн\b")
_RKF_FULL_RE = re.compile(r"(?iu)российск\w+\s+космическ\w+\s+форум\w+")
_RKF_ABBR_CTX_RE = re.compile(r"(?iu)\bркф\b.*\b(космическ|форум)\b|\b(космическ|форум)\b.*\bркф\b")


def _project_score(project: ProjectConfig, text: str) -> tuple[int, str]:
    score = 0
    matches: list[str] = []

    for anti in project.anti_markers:
        anti_norm = normalize_for_match(anti)
        if anti_norm and anti_norm in text:
            return -100, f"anti:{anti}"

    for marker in project.strict_markers:
        marker_norm = normalize_for_match(marker)
        if marker_norm and marker_norm in text:
            score += 6
            matches.append(marker)

    for marker in project.markers:
        marker_norm = normalize_for_match(marker)
        if marker_norm and marker_norm in text:
            score += 3
            matches.append(marker)

    if project.code == "kif":
        if not (_KIF_FULL_RE.search(text) or _KIF_ABBR_CTX_RE.search(text)):
            score -= 8
            matches.append("no_kif_phrase_context")
        elif "киф" in text and not _KIF_CONTEXT_RE.search(text):
            score -= 3
            matches.append("abbr_without_context")
    if project.code == "vnot":
        if not (_VNOT_FULL_RE.search(text) or _VNOT_ABBR_CTX_RE.search(text)):
            score -= 8
            matches.append("no_vnot_phrase_context")
    if project.code == "ren":
        if not (_REN_FULL_RE.search(text) or _REN_ABBR_CTX_RE.search(text)):
            score -= 8
            matches.append("no_ren_phrase_context")

    if project.code == "puteshestvuy":
        has_forum_phrase = (
            ("форум путешествуй" in text)
            or ("международный туристический форум путешествуй" in text)
            or ("мтф путешествуй" in text)
        )
        if "путешествуй" in text and "форум" not in text:
            score -= 4
            matches.append("generic_travel_word")
        if not has_forum_phrase:
            score -= 8
            matches.append("no_forum_context")
        if any(x in text for x in ("поиск дешевых авиабилетов", "горящие туры", "бронирование отелей", "душанбеводоканал")):
            score -= 8
            matches.append("travel_spam")

    if project.code == "rkf":
        if not (_RKF_FULL_RE.search(text) or _RKF_ABBR_CTX_RE.search(text)):
            score -= 8
            matches.append("no_rkf_phrase_context")
        elif "ркф" in text and "космичес" not in text and "форум" not in text:
            score -= 4
            matches.append("ambiguous_rkf")

    return score, ",".join(matches)


def route_social_item(item: dict, projects: dict[str, ProjectConfig]) -> RoutingResult:
    text = normalize_for_match(
        " ".join(
            [
                str(item.get("text_clean") or ""),
                str(item.get("text_raw") or ""),
                str(item.get("source") or ""),
                str(item.get("author_name") or ""),
            ]
        )
    )

    if not text:
        return RoutingResult("", [], "empty", 0.0, True, "empty_text")

    scores: list[tuple[str, int, str]] = []
    for code, cfg in projects.items():
        score, reason = _project_score(cfg, text)
        scores.append((code, score, reason))

    scores.sort(key=lambda x: x[1], reverse=True)
    best_code, best_score, best_reason = scores[0]

    if best_score <= 0:
        return RoutingResult("", [], best_reason or "no_match", 0.0, True, "no_project_match")

    secondary: list[str] = []
    for code, score, _ in scores[1:]:
        if score >= max(1, best_score - 2):
            secondary.append(code)

    confidence = min(1.0, max(0.2, best_score / 12.0))
    return RoutingResult(
        project_primary=best_code,
        project_secondary=secondary,
        rule_match=best_reason or "marker",
        confidence=confidence,
        is_noise=False,
        noise_reason="",
    )
