"""Command handler for the WordPress migration CLI."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import SQLAlchemyError

from .config import cli_config
from .wp_import import retry_failed_wp_images, run_wp_import, verify_wp_run


async def _run(args, cfg) -> dict:
    engine = create_async_engine(cfg.database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        if args.verify:
            return await verify_wp_run(factory, args.run_id, media_dir=cfg.media_dir)
        if args.retry_images:
            return await retry_failed_wp_images(
                factory, args.run_id, media_dir=cfg.media_dir, report_path=args.report,
            )
        return await run_wp_import(
            factory,
            args.file,
            media_dir=cfg.media_dir,
            apply=args.apply or args.resume,
            resume=args.resume,
            report_path=args.report,
        )
    finally:
        await engine.dispose()


def run_import_command(args) -> int:
    if args.verify or args.retry_images:
        if not args.run_id or args.file:
            print("Use --verify or --retry-images with --run <id> and without --file.", file=sys.stderr)
            return 2
    elif not args.file or args.run_id:
        print("Use --file <export.xml> for a dry-run or import.", file=sys.stderr)
        return 2
    try:
        cfg = cli_config()
        result = asyncio.run(_run(args, cfg))
    except SQLAlchemyError:
        print("Database failure: could not complete the WordPress import. Check the DB settings and retry.", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"WordPress import failed: {exc}", file=sys.stderr)
        return 1
    # Windows terminals may still use a legacy code page (e.g. cp1252); keep
    # stdout JSON ASCII-only so evidence such as "bread → baking" never crashes.
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result.get("passed", result.get("status") != "failed") else 1
