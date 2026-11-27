"""Build the race-week climatology (ADR 0016) from ERA5 via the Open-Meteo archive.

Calls a live provider: one archive request per sample point per year. Run it
by hand, not from tests or CI.

Usage:
    uv run python -I scripts/build_climatology.py \
        --first-year 1995 --last-year 2025 --spacing-km 50 [--delay-s 1.0]

Writes ``<RWI_CLIMATOLOGY_DIR>/<route_id>-race-week.json``. On any provider
error nothing is written.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rwi.climatology.build import build_race_week_climatology, climatology_path
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
    return ap.parse_args()


async def _main(args: argparse.Namespace) -> Path:
    settings = get_settings()
    route = load_bwsc_route()
    async with OpenMeteoProvider(settings) as provider:
        clim = await build_race_week_climatology(
            provider,
            route,
            first_year=args.first_year,
            last_year=args.last_year,
            spacing_km=args.spacing_km,
            delay_s=args.delay_s,
        )
    out = climatology_path(settings.climatology_dir, route.id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(clim.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(out)
    return out


if __name__ == "__main__":
    print(asyncio.run(_main(_args())))
