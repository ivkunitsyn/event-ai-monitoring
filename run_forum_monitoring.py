from __future__ import annotations

import argparse
import asyncio

from forum_monitoring_core import MonitoringEngine, ensure_paths, load_config


async def _run(args: argparse.Namespace) -> None:
    cfg = load_config()
    ensure_paths(cfg)
    engine = MonitoringEngine(cfg)
    await engine.init()

    if args.mode in {"ingest", "all"}:
        stats = await engine.run_ingest_once()
        print(
            "[ingest]"
            f" rss_seen={stats.rss_seen} rss_inserted={stats.rss_inserted}"
            f" social_seen={stats.social_seen} social_inserted={stats.social_inserted}"
        )

    if args.mode in {"digest", "all"}:
        projects = [args.project] if args.project else list(cfg.projects.keys())
        for code in projects:
            digest = await engine.build_digest(code)
            print(f"\n===== DIGEST {code} =====\n")
            print(digest)

    if args.mode in {"excel", "all"}:
        projects = [args.project] if args.project else list(cfg.projects.keys())
        for code in projects:
            path = await engine.build_excel(code)
            print(f"[excel] project={code} file={path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-project forum monitoring")
    parser.add_argument(
        "--mode",
        choices=["ingest", "digest", "excel", "all"],
        default="all",
        help="What to run",
    )
    parser.add_argument(
        "--project",
        default="",
        help="Single project code (kif, vnot, ren, puteshestvuy, rkf)",
    )

    args = parser.parse_args()
    asyncio.run(_run(args))
