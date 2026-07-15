# ИИ-мониторинг мероприятий

Единая система мониторинга информационного поля крупных форумов и мероприятий. Проект собирает материалы из RSS/IMAP Медиалогии, определяет релевантность, ранжирует публикации, формирует оперативные дайджесты и Excel-отчеты для разных проектных команд.

## Поддерживаемые проекты

В ядре предусмотрены несколько направлений:

- `kif` — Кавказский инвестиционный форум;
- `vnot` — Всероссийская неделя охраны труда;
- `ren` — Российская энергетическая неделя;
- `puteshestvuy` — форум «Путешествуй!»;
- `rkf` — Российский космический форум.

На сервере каждая команда была вынесена в отдельный MAX bot unit, но использовала единое ядро.

## Основные возможности

- ingest публикаций из RSS и IMAP;
- нормализация источников, дат, ссылок и текстов;
- фильтрация нерелевантного шума;
- оценка качества и значимости публикаций;
- группировка похожих материалов;
- генерация лидов и аналитики через LLM;
- формирование HTML/текстовых дайджестов;
- Excel-экспорт за период;
- MAX-боты для отдельных проектных команд.

## Архитектура

| Файл / модуль | Назначение |
| --- | --- |
| `run_forum_monitoring.py` | CLI-обертка для ingest/digest/excel. |
| `run_forum_monitoring_max_bot.py` | Запуск MAX-бота выбранного проекта. |
| `forum_monitoring_core/config.py` | Конфигурация проектов, RSS, IMAP и LLM. |
| `forum_monitoring_core/storage.py` | SQLite-хранилище публикаций и состояния. |
| `forum_monitoring_core/ranking.py` | Ранжирование и оценка материалов. |
| `forum_monitoring_core/grouping.py` | Кластеризация похожих материалов. |
| `forum_monitoring_core/leads.py` | Генерация кратких описаний и лидов. |
| `forum_monitoring_core/export_excel.py` | Выгрузка отчетов в Excel. |
| `forum_monitoring_core/render.py` | Рендеринг сообщений и отчетов. |

## Поток обработки

1. Система получает публикации из настроенных источников.
2. Материалы приводятся к единому формату.
3. Дубликаты и похожие тексты группируются.
4. Релевантность оценивается правилами и LLM-слоем.
5. Самые значимые материалы попадают в дайджест.
6. Команда получает краткий выпуск в MAX и при необходимости Excel-отчет.

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Примеры:

```bash
python run_forum_monitoring.py --mode ingest
python run_forum_monitoring.py --mode digest --project kif
python run_forum_monitoring.py --mode excel --project kif
python run_forum_monitoring_max_bot.py
```

## Конфигурация

Основные переменные:

- `OPENAI_API_KEY`;
- `OPENAI_HTTP_PROXY`;
- `USE_OPENAI`;
- `OPENAI_MODEL_SIMPLE`;
- `OPENAI_MODEL_COMPLEX`;
- `RSS_KIF`, `RSS_VNOT`, `RSS_REN`, `RSS_PUTESHESTVUY`, `RSS_RKF`;
- `MLG_IMAP_ENABLE`;
- `MLG_IMAP_HOST`, `MLG_IMAP_LOGIN`, `MLG_IMAP_PASSWORD`, `MLG_IMAP_MAILBOX`;
- `MONITOR_DATA_DIR`, `MONITOR_DB_PATH`, `MONITOR_REPORTS_DIR`;
- MAX bot token для каждого проекта.

## Production-схема

На сервере были запущены отдельные systemd services:

- `forum-monitoring-multi-max-kif`;
- `forum-monitoring-multi-max-vnot`;
- `forum-monitoring-multi-max-ren`;
- `forum-monitoring-multi-max-puteshestvuy`;
- `forum-monitoring-multi-max-rkf`;
- `forum-monitoring-multi-ingest` как периодический/one-shot ingest.

## Данные

Проект использует SQLite и локальные каталоги данных. Базы, отчеты и runtime-артефакты исключены из Git. Для production нужно отдельно настроить backup БД и отчетных файлов.

## Статус

Рабочая multi-project система мониторинга форумов. Репозиторий опубликован как очищенный server snapshot без секретов, баз, логов и виртуального окружения.
