"""Download MERRA-2 hourly surface shortwave (M2T1NXRAD SWGDN) at one point (ADR 0017).

Calls NASA's Giovanni time-series service through ``pvlib.iotools.get_merra2`` with the
Earthdata account from ``RWI_EARTHDATA_USERNAME`` / ``RWI_EARTHDATA_PASSWORD``. Run it
by hand, not from tests.

Usage:
    uv run python -I scripts/truth/fetch_merra2.py --latitude -23.798 --longitude 133.888 \
        --first-year 2015 --last-year 2025 [--months 8] [--name asp]

One file per year: ``<RWI_TRUTH_DIR>/merra2/<name>-<year>.json`` holding the
``WeatherSeries`` (ANALYSIS, labelled at the end of each hour). Existing years are skipped.
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import pvlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rwi.config import get_settings
from rwi.core.geo import GeoPoint
from rwi.core.timebase import utc_now
from rwi.providers.merra2 import DATASET, hourly_ghi_series

log = logging.getLogger("fetch_merra2")


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--latitude", type=float, required=True)
    ap.add_argument("--longitude", type=float, required=True)
    ap.add_argument("--first-year", type=int, required=True)
    ap.add_argument("--last-year", type=int, required=True)
    ap.add_argument("--months", type=int, nargs="+", default=[8])
    ap.add_argument("--name", default="asp", help="file prefix for this point")
    return ap.parse_args()


def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    user, password = settings.earthdata_username, settings.earthdata_password
    if not user or password is None:
        print("set RWI_EARTHDATA_USERNAME and RWI_EARTHDATA_PASSWORD", file=sys.stderr)
        return 2
    point = GeoPoint(latitude_deg=args.latitude, longitude_deg=args.longitude)
    out = settings.truth_dir / "merra2"
    out.mkdir(parents=True, exist_ok=True)
    failed: list[int] = []
    for year in range(args.first_year, args.last_year + 1):
        path = out / f"{args.name}-{year}.json"
        if path.exists():
            continue
        frames = []
        try:
            for month in args.months:
                start = pd.Timestamp(year=year, month=month, day=1, tz="UTC")
                end = start + pd.offsets.MonthEnd(0) + pd.Timedelta(hours=23)
                frame, meta = pvlib.iotools.get_merra2(
                    args.latitude,
                    args.longitude,
                    start,
                    end,
                    user,
                    password.get_secret_value(),
                    dataset=DATASET,
                    variables=["SWGDN"],
                )
                frames.append(frame)
        except Exception as exc:  # one failed year must not stop the rest
            log.warning("%d: %s", year, exc)
            failed.append(year)
            continue
        series = hourly_ghi_series(
            pd.concat(frames), meta, requested_point=point, retrieved_at_utc=utc_now()
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(series.model_dump_json(), encoding="utf-8")
        tmp.replace(path)
        log.info("%s: %d hours", path.name, len(series.steps))
    if failed:
        print(f"not downloaded: {failed}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main(_args()))
