# forum_monitoring_core

Единое ядро мультипроектного мониторинга форумов.

## Проекты

- `kif` — Кавказский инвестиционный форум
- `vnot` — Всероссийская неделя охраны труда
- `ren` — Российская энергетическая неделя
- `puteshestvuy` — форум «Путешествуй!»
- `rkf` — Российский космический форум

## Запуск

```bash
cd "/Users/Acer/Desktop/Автоматизации/Мониторинг форумов"
python run_forum_monitoring.py --mode ingest
python run_forum_monitoring.py --mode digest --project kif
python run_forum_monitoring.py --mode excel --project kif
```

## Основные env

- `OPENAI_API_KEY`
- `OPENAI_HTTP_PROXY` (опционально)
- `USE_OPENAI=1|0`
- `OPENAI_MODEL_SIMPLE` (по умолчанию `gpt-5-nano`)
- `OPENAI_MODEL_COMPLEX` (по умолчанию `gpt-5-mini`)
- `RSS_KIF`, `RSS_VNOT`, `RSS_REN`, `RSS_PUTESHESTVUY`, `RSS_RKF`
- `MLG_IMAP_ENABLE=1|0`
- `MLG_IMAP_HOST`, `MLG_IMAP_PORT`, `MLG_IMAP_LOGIN`, `MLG_IMAP_PASSWORD`, `MLG_IMAP_MAILBOX`
- `MLG_IMAP_SENDER` (по умолчанию `noreply@mlg.ru,notify@mlg.ru`)
- `MLG_IMAP_FORCE_FULL_REREAD=1|0`
- `MONITOR_DATA_DIR`, `MONITOR_DB_PATH`, `MONITOR_REPORTS_DIR`
