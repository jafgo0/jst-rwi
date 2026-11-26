"""Run the capture worker as its own process.

    .venv/bin/python -m rwi.capture --once
    .venv/bin/python -m rwi.capture            # loop on the configured interval

Separate from the API on purpose: capture must keep running while the API is
restarted, and a crash in one must not take the other down. SQLite in WAL mode
lets the API read while this process writes (ADR 0007).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rwi.capture.sites import DEFAULT_SITES, sites_from_ids
from rwi.capture.worker import (
    CAPTURE_MODELS,
    GriddedCapture,
    aifs_capture,
    capture_once,
    enforce_retention,
    run_forever,
)
from rwi.config import get_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rwi.capture",
        description="Archive forecasts and observations so they can later be verified.",
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument(
        "--sites",
        nargs="*",
        default=None,
        help=f"site ids to capture (default: all {len(DEFAULT_SITES)})",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=list(CAPTURE_MODELS),
        help="named forecast models to capture separately",
    )
    parser.add_argument("--hours", type=float, default=None, help="forecast horizon to archive")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="apply the retention policy after the cycle (ADR 0007)",
    )
    parser.add_argument(
        "--aifs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also archive ECMWF AIFS open data (default: RWI_CAPTURE_AIFS)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    from rwi.archive import open_archive
    from rwi.providers.registry import available_providers, close_providers

    settings = get_settings()
    sites = sites_from_ids(args.sites)
    providers = available_providers(settings)
    archive = open_archive(settings)
    use_aifs = settings.capture_aifs if args.aifs is None else args.aifs
    gridded: list[GriddedCapture] = [aifs_capture(settings, sites)] if use_aifs else []
    try:
        if args.once:
            result = await capture_once(
                archive=archive,
                providers=providers,
                sites=sites,
                models=args.models,
                forecast_hours=args.hours or settings.capture_forecast_hours,
                gridded=gridded,
            )
            print(result.summary())
            for note in result.notes:
                print(f"note: {note}")
            for failure in result.failures:
                print(f"failure: {failure}")
            if args.prune:
                removed = enforce_retention(archive, settings)
                print(f"retention: {removed} row(s) removed")
            return 0 if result.succeeded else 1
        await run_forever(
            archive=archive,
            providers=providers,
            settings=settings,
            sites=sites,
            gridded=gridded,
        )
        return 0
    finally:
        await close_providers(providers)
        archive.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
