"""Download BSRN station-to-archive files (LR0100, 1-minute) for given months (ADR 0017).

Calls a live FTP server (ftp.bsrn.awi.de) with the account from
``RWI_BSRN_FTP_USERNAME`` / ``RWI_BSRN_FTP_PASSWORD``. Run it by hand, not from tests.

Usage:
    uv run python -I scripts/truth/fetch_bsrn.py --station asp \
        --first-year 2015 --last-year 2025 [--months 8]

Files land under ``<RWI_TRUTH_DIR>/bsrn/<station>/`` as published (``asp0824.dat.gz``);
months already there are skipped, so a rerun continues an interrupted download.
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
import pvlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rwi.config import get_settings

log = logging.getLogger("fetch_bsrn")


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--station", default="asp", help="BSRN station code (asp = Alice Springs)")
    ap.add_argument("--first-year", type=int, required=True)
    ap.add_argument("--last-year", type=int, required=True)
    ap.add_argument("--months", type=int, nargs="+", default=[8])
    return ap.parse_args()


def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    user, password = settings.bsrn_ftp_username, settings.bsrn_ftp_password
    if not user or password is None:
        print("set RWI_BSRN_FTP_USERNAME and RWI_BSRN_FTP_PASSWORD", file=sys.stderr)
        return 2
    out = settings.truth_dir / "bsrn" / args.station.lower()
    out.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for year in range(args.first_year, args.last_year + 1):
        for month in args.months:
            name = f"{args.station.lower()}{month:02d}{year % 100:02d}.dat.gz"
            if (out / name).exists():
                continue
            start = pd.Timestamp(year=year, month=month, day=1)
            end = start + pd.offsets.MonthEnd(0)
            try:
                pvlib.iotools.get_bsrn(
                    args.station.lower(),
                    start,
                    end,
                    user,
                    password.get_secret_value(),
                    logical_records=("0100",),
                    save_path=str(out),
                )
            except Exception as exc:  # one missing month must not stop the rest
                log.warning("%s: %s", name, exc)
                failed.append(name)
                continue
            log.info("%s", name)
    if failed:
        print("not downloaded: " + ", ".join(failed), file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main(_args()))
