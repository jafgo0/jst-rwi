"""Build the race-week climatology (ADR 0016) from ERA5 via the Open-Meteo archive.

Calls a live provider: one archive request per sample point per year. Run it
by hand, not from tests or CI.

Usage:
    uv run python -I scripts/build_climatology.py \
        --first-year 1991 --last-year 2025 --spacing-km 50 [--delay-s 1.0]

Resumable. Every point-year is saved under ``--parts-dir`` as soon as it arrives
and reused on the next run, so an interrupted or failed build is continued by
running the same command again. A point-year is retried ``--attempts`` times with
exponential backoff from ``--backoff-s``; one that still fails is listed and the
build carries on, unless ``--max-consecutive-failures`` fail in a row (provider down
or quota spent), which stops it early.

Writes ``<RWI_CLIMATOLOGY_DIR>/<route_id>-race-week.json`` only when every
point-year is present; otherwise exits with status 2 and writes nothing.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rwi.climatology.build import (
    IncompleteClimatologyError,
    build_race_week_climatology,
    climatology_path,
)
from rwi.config import get_settings
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.route.bwsc import load_bwsc_route


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--first-year", type=int, required=True)
    ap.add_argument("--last-year", type=int, required=True)
    ap.add_argument(
        "--spacing-km",
        type=float,
        required=True,
        help="sample spacing along each segment; ERA5 grid is ~28 km",
    )
    ap.add_argument("--delay-s", type=float, default=1.0, help="pause between requests")
    ap.add_argument("--attempts", type=int, default=5, help="tries per point-year")
    ap.add_argument(
        "--backoff-s", type=float, default=30.0, help="first retry wait; doubles each try"
    )
    ap.add_argument("--max-consecutive-failures", type=int, default=3)
    ap.add_argument(
        "--parts-dir",
        type=Path,
        default=None,
        help="checkpoint directory (default <RWI_CLIMATOLOGY_DIR>/parts)",
    )
    return ap.parse_args()


async def _main(args: argparse.Namespace) -> Path:
    settings = get_settings()
    route = load_bwsc_route()
    parts_dir = args.parts_dir or settings.climatology_dir / "parts"
    async with OpenMeteoProvider(settings) as provider:
        clim = await build_race_week_climatology(
            provider,
            route,
            first_year=args.first_year,
            last_year=args.last_year,
            spacing_km=args.spacing_km,
            delay_s=args.delay_s,
            parts_dir=parts_dir,
            attempts=args.attempts,
            backoff_s=args.backoff_s,
            max_consecutive_failures=args.max_consecutive_failures,
        )
    out = climatology_path(settings.climatology_dir, route.id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(clim.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        print(asyncio.run(_main(_args())))
    except IncompleteClimatologyError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
