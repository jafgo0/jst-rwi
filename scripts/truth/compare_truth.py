"""Clear-sky check and ERA5 / MERRA-2 bias against BSRN Alice Springs (ADR 0017).

Reads the BSRN files from ``fetch_bsrn.py`` and, if present, the MERRA-2 files from
``fetch_merra2.py``. With ``--era5`` it also fetches ERA5 at the station through the
Open-Meteo archive (a live provider). Run it by hand, not from tests.

Usage:
    uv run python -I scripts/truth/compare_truth.py --station asp [--era5] [--linke 3.0]

Writes ``<RWI_TRUTH_DIR>/report-<station>.json``: one clear-sky check over all August
minutes, and per candidate the hourly bias over all overlapping daylight hours.
"""

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rwi.config import get_settings
from rwi.contracts import WeatherSeries
from rwi.core.timebase import TimeWindow
from rwi.providers.bsrn import hourly_series, quality_control, read_bsrn_files
from rwi.providers.openmeteo import OpenMeteoProvider
from rwi.validation.truth import bias_statistics, clear_sky_check


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--station", default="asp")
    ap.add_argument("--merra2-name", default="asp")
    ap.add_argument("--era5", action="store_true", help="fetch ERA5 at the station (network)")
    ap.add_argument("--linke", type=float, default=None, help="fixed Linke turbidity")
    ap.add_argument("--min-minutes", type=int, default=60, help="minutes required per hour")
    return ap.parse_args()


async def _era5(reference: WeatherSeries) -> list[WeatherSeries]:
    settings = get_settings()
    out = []
    async with OpenMeteoProvider(settings) as provider:
        for year in sorted({t.year for t in reference.times}):
            times = [t for t in reference.times if t.year == year]
            window = TimeWindow(start_utc=times[0] - timedelta(hours=1), end_utc=times[-1])
            out.append(
                await provider.fetch_analysis(reference.point, window, ["ghi"], models="era5")
            )
    return out


def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    files = sorted((settings.truth_dir / "bsrn" / args.station.lower()).glob("*.dat.gz"))
    if not files:
        print("no BSRN files; run scripts/truth/fetch_bsrn.py first", file=sys.stderr)
        return 2
    record = read_bsrn_files(args.station, files)
    minutes = quality_control(record)
    august = minutes[pd.DatetimeIndex(minutes.index).month == 8]
    report: dict[str, object] = {"station": args.station, "files": list(record.files)}
    if not august.empty:
        check = clear_sky_check(august, record.point, linke_turbidity=args.linke)
        report["clear_sky_august"] = check.model_dump(mode="json")
    reference = hourly_series(record, min_minutes=args.min_minutes)
    candidates: list[WeatherSeries] = [
        WeatherSeries.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((settings.truth_dir / "merra2").glob(f"{args.merra2_name}-*.json"))
    ]
    if args.era5:
        candidates += asyncio.run(_era5(reference))
    report["bias"] = [bias_statistics(c, reference).model_dump(mode="json") for c in candidates]
    out = settings.truth_dir / f"report-{args.station.lower()}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(_args()))
