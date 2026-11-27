"""Replay recorded on-board sensor logs through the live pipeline (ADR 0010 §6).

    .venv/bin/python -m rwi.telemetry.onboard_cli replay \\
        --canboat airmar.jsonl --ml01 ml01.csv --spool /tmp/replay.sqlite3

Sensor settings (serial, mount, height, ML-01 calibration) come from the
same ``RWI_*`` environment as live ingest; a missing one fails loudly. The
replay uses its own spool, so it never mixes with the live one. Positions
become HISTORICAL_REPLAY fixes and are counted, not pushed into the API state.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rwi.archive.store import open_archive
from rwi.config import get_settings
from rwi.telemetry.ingest import SINK_ARCHIVE, SINK_LIVE, OnboardConfig, OnboardPipeline
from rwi.telemetry.onboard import SensorConfigError
from rwi.telemetry.spool import SensorSpool


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m rwi.telemetry.onboard_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="feed recorded logs through spool and sinks")
    replay.add_argument("--canboat", type=Path, help="canboat analyzer -json output (JSONL)")
    replay.add_argument("--ml01", type=Path, help="ML-01 logger CSV")
    replay.add_argument("--spool", type=Path, required=True, help="spool file for this replay")
    replay.add_argument("--no-archive", action="store_true", help="spool only, skip the archive")
    return parser.parse_args(argv)


async def _count_live(pipeline: OnboardPipeline) -> int:
    if pipeline.live is None:
        return 0
    return sum([1 async for _ in pipeline.live.stream()])


def replay(args: argparse.Namespace) -> dict[str, object]:
    """Run one replay; return the counts that are printed."""
    if args.canboat is None and args.ml01 is None:
        raise SensorConfigError("give --canboat and/or --ml01")
    settings = get_settings()
    config = OnboardConfig.from_settings(settings)
    spool = SensorSpool(args.spool)
    try:
        archive = None if args.no_archive else open_archive(settings)
        pipeline = OnboardPipeline(config, spool, archive, replay=True)
        if args.canboat is not None:
            with args.canboat.open(encoding="utf-8") as f:
                for line in f:
                    pipeline.feed_canboat(line)
        if args.ml01 is not None:
            with args.ml01.open(encoding="utf-8") as f:
                for line in f:
                    pipeline.feed_ml01(line)
        report = pipeline.finish()
        live_samples = asyncio.run(_count_live(pipeline))
        return {
            "windows_new": pipeline.stats.windows_new,
            "windows_repeated": pipeline.stats.windows_repeated,
            "gaps": pipeline.stats.gaps,
            "spool_windows": spool.count(),
            "live_outcomes": spool.outcomes(SINK_LIVE),
            "live_samples": live_samples,
            "archive_outcomes": spool.outcomes(SINK_ARCHIVE),
            "series_written": report.series_written if report else 0,
            "values_written": report.values_written if report else 0,
            "archive_failed": report.failed if report else False,
        }
    finally:
        spool.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = replay(args)
    except SensorConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["archive_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
