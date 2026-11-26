"""``python -m rwi.ml`` - retrain, evaluate and list post-processing models.

    python -m rwi.ml retrain --variable ghi --cutoff 2027-08-01T00:00Z \\
        --target-kind bom_observation
    python -m rwi.ml eval --variable ghi --cutoff 2027-08-01T00:00Z
    python -m rwi.ml list

The archive is opened read-only. ``retrain`` writes a new immutable version
under ``--models-dir``; it never overwrites one. Pure functions (`retrain`,
`evaluate`) take pairs, so they are testable without an archive.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from rwi.contracts import ForecastObservationPair
from rwi.core.timebase import ensure_utc
from rwi.ml.artifacts import (
    TARGET_KINDS,
    ModelManifest,
    TargetKind,
    list_manifests,
    load_model,
    save_model,
)
from rwi.ml.constraints import ML_VARIABLES
from rwi.ml.dataset import dataset_from_pairs, split_by_cutoff
from rwi.ml.quantile import NUM_BOOST_ROUND, MlUnavailableError, train_quantile_model
from rwi.ml.scoring import ScoreRow, score_holdout, summary_metrics

__all__ = ["CALIBRATION_MIN_COVERAGE", "EvalReport", "evaluate", "main", "retrain"]

DEFAULT_MODELS_DIR = Path("data/models")
#: Holdout P10-P90 coverage below this marks the model uncalibrated (UNVALIDATED output).
CALIBRATION_MIN_COVERAGE = 0.70


class EvalReport(BaseModel):
    """Scores of one model version on one holdout."""

    model_config = ConfigDict(frozen=True)

    variable: str
    model_version: str | None
    n_holdout: int
    rows: list[ScoreRow]


def retrain(
    pairs: Sequence[ForecastObservationPair],
    *,
    variable: str,
    cutoff_utc: datetime,
    target_kind: TargetKind,
    models_dir: Path,
    num_boost_round: int = NUM_BOOST_ROUND,
    model_version: str | None = None,
) -> tuple[ModelManifest, list[ScoreRow]]:
    """Fit on rows known at ``cutoff_utc``, score on later rows, save a new version."""
    ds = dataset_from_pairs(pairs, variable=variable)
    split = split_by_cutoff(ds, cutoff_utc)
    if split.training_cutoff_utc is None or split.train.data_window is None:
        raise MlUnavailableError(f"{variable}: no training rows at or before {cutoff_utc}")
    model = train_quantile_model(split.train, num_boost_round=num_boost_round)
    rows = score_holdout(model, split.holdout)
    metrics = summary_metrics(rows)
    cov = metrics.get("coverage_80")
    window = split.train.data_window
    manifest = save_model(
        model,
        root=models_dir,
        target_kind=target_kind,
        training_cutoff_utc=split.training_cutoff_utc,
        data_window=window,
        n_train=len(split.train),
        n_holdout=len(split.holdout),
        metrics=metrics,
        calibrated=cov is not None and cov >= CALIBRATION_MIN_COVERAGE,
        model_version=model_version,
    )
    return manifest, rows


def evaluate(
    pairs: Sequence[ForecastObservationPair],
    *,
    variable: str,
    models_dir: Path,
    model_version: str | None = None,
    after_utc: datetime | None = None,
) -> EvalReport:
    """Score a saved model on rows issued after its training cutoff (or ``after_utc``)."""
    model, manifest = load_model(models_dir, variable, model_version)
    ds = dataset_from_pairs(pairs, variable=variable, source_vocabulary=model.source_vocabulary)
    cutoff = manifest.training_cutoff_utc
    if after_utc is not None:
        cutoff = max(cutoff, ensure_utc(after_utc))
    holdout = split_by_cutoff(ds, cutoff).holdout
    return EvalReport(
        variable=variable,
        model_version=manifest.model_version,
        n_holdout=len(holdout),
        rows=score_holdout(model, holdout),
    )


def _utc(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("time must carry a UTC offset, e.g. 2027-08-01T00:00Z")
    return ensure_utc(value)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m rwi.ml", description=__doc__.splitlines()[0])
    p.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    p.add_argument("--archive", type=Path, default=None, help="SQLite archive (read-only)")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("retrain", help="fit a new model version")
    r.add_argument("--variable", choices=ML_VARIABLES, required=True)
    r.add_argument("--cutoff", type=_utc, required=True, help="training cutoff (UTC)")
    r.add_argument("--target-kind", choices=TARGET_KINDS, required=True)
    r.add_argument("--rounds", type=int, default=NUM_BOOST_ROUND)
    e = sub.add_parser("eval", help="score a saved model on later rows")
    e.add_argument("--variable", choices=ML_VARIABLES, required=True)
    e.add_argument("--version", default=None)
    e.add_argument("--after", type=_utc, default=None)
    sub.add_parser("list", help="list saved model versions")
    return p


def _pairs(archive: Path | None, variable: str) -> list[ForecastObservationPair]:
    from rwi.archive.store import SqliteArchive, open_archive

    store = SqliteArchive(archive, read_only=True) if archive else open_archive()
    return store.pairs(variable=variable)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            for m in list_manifests(args.models_dir):
                print(json.dumps({
                    "model_id": m.model_id, "version": m.model_version,
                    "cutoff": m.training_cutoff_utc.isoformat(), "target": m.target_kind,
                    "n_train": m.n_train, "calibrated": m.calibrated, "metrics": m.metrics,
                }))
            return 0
        pairs = _pairs(args.archive, args.variable)
        if args.command == "retrain":
            manifest, rows = retrain(
                pairs,
                variable=args.variable,
                cutoff_utc=args.cutoff,
                target_kind=cast(TargetKind, args.target_kind),
                models_dir=args.models_dir,
                num_boost_round=args.rounds,
            )
            print(manifest.model_dump_json(indent=2))
        else:
            report = evaluate(
                pairs, variable=args.variable, models_dir=args.models_dir,
                model_version=args.version, after_utc=args.after,
            )
            rows = report.rows
        for row in rows:
            print(row.model_dump_json())
    except MlUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
