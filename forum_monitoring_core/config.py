from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    code: str
    name: str
    rss_url: str
    markers: tuple[str, ...]
    strict_markers: tuple[str, ...]
    anti_markers: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    data_dir: Path
    db_path: Path
    reports_dir: Path
    openai_api_key: str
    openai_base_url: str
    openai_http_referer: str
    openai_x_title: str
    openai_proxy: str
    use_openai: bool
    model_simple: str
    model_complex: str
    model_analytics: str
    rss_proxy: str
    rss_poll_seconds: int
    imap_enable: bool
    imap_host: str
    imap_port: int
    imap_login: str
    imap_password: str
    imap_mailbox: str
    imap_sender_allowlist: tuple[str, ...]
    imap_poll_seconds: int
    imap_bootstrap_limit: int
    imap_force_full_reread: bool
    social_export_paths: tuple[str, ...]
    projects: dict[str, ProjectConfig]


DEFAULT_PROJECTS: dict[str, ProjectConfig] = {
    "kif": ProjectConfig(
        code="kif",
        name="Кавказский инвестиционный форум",
        rss_url=os.getenv(
            "RSS_KIF",
            "https://pr.mlg.ru/Report.mlg/GetRss?p1=ZQThVabuJkSHWm6SBzGXnw%253d%253d&p2=1w5Rxu3MgDaQK0VMF3GbdA%253d%253d&p3=wRikcLTv4ofp9LlTX%252faD5Q%253d%253d",
        ).strip(),
        markers=(
            "кавказский инвестиционный форум",
            "caucasus investment forum",
            "киф",
        ),
        strict_markers=(
            "кавказский инвестиционный форум",
            "caucasus investment forum",
        ),
        anti_markers=(),
    ),
    "vnot": ProjectConfig(
        code="vnot",
        name="Всероссийская неделя охраны труда",
        rss_url=os.getenv(
            "RSS_VNOT",
            "https://pr.mlg.ru/Report.mlg/GetRss?p1=ZQThVabuJkSHWm6SBzGXnw%253d%253d&p2=1w5Rxu3MgDaQK0VMF3GbdA%253d%253d&p3=lnmjoRMFA4T%252bkDp8E4iZgQ%253d%253d",
        ).strip(),
        markers=(
            "всероссийская неделя охраны труда",
            "внот",
        ),
        strict_markers=("всероссийская неделя охраны труда",),
        anti_markers=(),
    ),
    "ren": ProjectConfig(
        code="ren",
        name="Российская энергетическая неделя",
        rss_url=os.getenv(
            "RSS_REN",
            "https://pr.mlg.ru/Report.mlg/GetRss?p1=ZQThVabuJkSHWm6SBzGXnw%253d%253d&p2=1w5Rxu3MgDaQK0VMF3GbdA%253d%253d&p3=IO4XLMeihvCcYsi%252b93uqbQ%253d%253d",
        ).strip(),
        markers=(
            "российская энергетическая неделя",
            "russian energy week",
            "рэн",
        ),
        strict_markers=("российская энергетическая неделя", "russian energy week"),
        anti_markers=(),
    ),
    "puteshestvuy": ProjectConfig(
        code="puteshestvuy",
        name="Международный туристический форум «Путешествуй!»",
        rss_url=os.getenv(
            "RSS_PUTESHESTVUY",
            "https://pr.mlg.ru/Report.mlg/GetRss?p1=ZQThVabuJkSHWm6SBzGXnw%253d%253d&p2=1w5Rxu3MgDaQK0VMF3GbdA%253d%253d&p3=IBF52VZsfKrqFvIULiH7jA%253d%253d",
        ).strip(),
        markers=(
            "международный туристический форум путешествуй",
            "форум путешествуй",
            "мтф путешествуй",
        ),
        strict_markers=(
            "международный туристический форум путешествуй",
            "форум путешествуй",
            "мтф путешествуй",
        ),
        anti_markers=(
            "путешествуй по",
            "путешествуй с",
            "турагент",
            "турпутев",
            "промокод",
            "горящий тур",
            "поиск дешевых авиабилетов",
            "бронирование отелей",
        ),
    ),
    "rkf": ProjectConfig(
        code="rkf",
        name="Российский космический форум",
        rss_url=os.getenv(
            "RSS_RKF",
            "https://pr.mlg.ru/Report.mlg/GetRss?p1=ZQThVabuJkSHWm6SBzGXnw%253d%253d&p2=1w5Rxu3MgDaQK0VMF3GbdA%253d%253d&p3=TcnaEZ9jIwV6A7x5k3n1mg%253d%253d",
        ).strip(),
        markers=(
            "российский космический форум",
            "ркф",
        ),
        strict_markers=("российский космический форум",),
        anti_markers=(
            "кинолог",
            "собак",
            "собаковод",
            "выставка собак",
            "питомник",
            "дрессиров",
        ),
    ),
}


def _bool_env(name: str, default: bool) -> bool:
    value = (os.getenv(name, "") or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name, "") or "").strip() or default)
    except Exception:
        return default


def load_config() -> AppConfig:
    data_dir = Path(os.getenv("MONITOR_DATA_DIR", "./forum_monitoring_data")).resolve()
    db_path = Path(os.getenv("MONITOR_DB_PATH", str(data_dir / "forum_monitoring.db"))).resolve()
    reports_dir = Path(os.getenv("MONITOR_REPORTS_DIR", str(data_dir / "reports"))).resolve()

    projects: dict[str, ProjectConfig] = {}
    for code, project in DEFAULT_PROJECTS.items():
        env_name = f"RSS_{code.upper()}"
        rss_url = (os.getenv(env_name, "") or project.rss_url).strip()
        projects[code] = ProjectConfig(
            code=project.code,
            name=project.name,
            rss_url=rss_url,
            markers=project.markers,
            strict_markers=project.strict_markers,
            anti_markers=project.anti_markers,
        )

    default_model = (os.getenv("OPENAI_MODEL", "") or "").strip()

    return AppConfig(
        timezone=os.getenv("TIMEZONE_MSK", "Europe/Moscow").strip(),
        data_dir=data_dir,
        db_path=db_path,
        reports_dir=reports_dir,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_base_url=(
            os.getenv("OPENAI_BASE_URL", "").strip()
            or "https://openrouter.ai/api/v1"
        ),
        openai_http_referer=(
            os.getenv("OPENAI_HTTP_REFERER", "").strip()
            or os.getenv("HTTP-Referer", "").strip()
        ),
        openai_x_title=(
            os.getenv("OPENAI_X_TITLE", "").strip()
            or os.getenv("X-Title", "").strip()
        ),
        openai_proxy=os.getenv("OPENAI_HTTP_PROXY", "").strip(),
        use_openai=_bool_env("USE_OPENAI", True),
        model_simple=os.getenv("OPENAI_MODEL_SIMPLE", default_model or "gpt-5-nano").strip() or (default_model or "gpt-5-nano"),
        model_complex=os.getenv("OPENAI_MODEL_COMPLEX", default_model or "gpt-5-mini").strip() or (default_model or "gpt-5-mini"),
        model_analytics=os.getenv("OPENAI_MODEL_ANALYTICS", default_model or "gpt-4.1").strip() or (default_model or "gpt-4.1"),
        rss_proxy=os.getenv("RSS_HTTP_PROXY", os.getenv("OPENAI_HTTP_PROXY", "")).strip(),
        rss_poll_seconds=_int_env("RSS_POLL_SECONDS", 90),
        imap_enable=_bool_env("MLG_IMAP_ENABLE", True),
        imap_host=os.getenv("MLG_IMAP_HOST", "imap.yandex.ru").strip(),
        imap_port=_int_env("MLG_IMAP_PORT", 993),
        imap_login=os.getenv("MLG_IMAP_LOGIN", "").strip(),
        imap_password=os.getenv("MLG_IMAP_PASSWORD", "").strip(),
        imap_mailbox=os.getenv("MLG_IMAP_MAILBOX", "INBOX").strip() or "INBOX",
        imap_sender_allowlist=tuple(
            x.strip().lower()
            for x in (os.getenv("MLG_IMAP_SENDER", "noreply@mlg.ru,notify@mlg.ru").split(","))
            if x.strip()
        ),
        imap_poll_seconds=_int_env("MLG_IMAP_POLL_SECONDS", 120),
        imap_bootstrap_limit=_int_env("MLG_IMAP_BOOTSTRAP_LIMIT", 400),
        imap_force_full_reread=_bool_env("MLG_IMAP_FORCE_FULL_REREAD", True),
        social_export_paths=tuple(
            x.strip()
            for x in re.split(r"[;,]", os.getenv("SOCIAL_EXPORT_JSON", "").strip())
            if x.strip()
        ),
        projects=projects,
    )


def ensure_paths(cfg: AppConfig) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
