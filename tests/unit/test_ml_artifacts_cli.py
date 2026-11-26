"""rwi.ml.artifacts, rwi.ml.postprocess and the ``python -m rwi.ml`` functions."""

import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from ml_synth import START, ghi_pairs, require_lightgbm, upstream_ghi

from rwi.core.provenance import DataKind
from rwi.ml.__main__ import evaluate, main, retrain
from rwi.ml.artifacts import ModelArtifactError, list_manifests, load_model
from rwi.ml.features import feature_schema_hash
from rwi.ml.postprocess import ML_DATASET, ML_PROVIDER, MlPostprocessor
from rwi.ml.quantile import MlUnavailableError

require_lightgbm()

CUTOFF = START + timedelta(days=24)


@pytest.fixture(scope="module")
def models_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("models")
    retrain(
        ghi_pairs(30),
        variable="ghi",
        cutoff_utc=CUTOFF,
        target_kind="bom_observation",
        models_dir=root,
        num_boost_round=40,
        model_version="v1",
    )
    return root


def test_manifest_records_cutoff_window_schema_and_metrics(models_dir: Path) -> None:
    (m,) = list_manifests(models_dir)
    assert m.model_id == "pp-ghi" and m.model_version == "v1"
    assert m.training_cutoff_utc <= CUTOFF
    assert m.data_window_end_utc <= CUTOFF
    assert m.feature_schema_hash == feature_schema_hash()
    assert m.n_train > 0 and m.n_holdout > 0
    assert m.metrics["crps"] is not None
    assert (models_dir / "pp-ghi" / "v1" / "q10.txt").is_file()


def test_versions_are_immutable(models_dir: Path) -> None:
    with pytest.raises(ModelArtifactError):
        retrain(
            ghi_pairs(30), variable="ghi", cutoff_utc=CUTOFF, target_kind="bom_observation",
            models_dir=models_dir, num_boost_round=5, model_version="v1",
        )


def test_reload_predicts_like_the_saved_model_and_rejects_schema_drift(
    models_dir: Path, tmp_path: Path
) -> None:
    _, manifest = load_model(models_dir, "ghi")
    assert manifest.model_version == "v1"
    report = evaluate(ghi_pairs(30), variable="ghi", models_dir=models_dir)
    assert report.n_holdout > 0
    ml_all = next(r for r in report.rows if r.system == "ml" and r.group_kind == "all")
    assert ml_all.crps == pytest.approx(manifest.metrics["crps"])

    bad = tmp_path / "pp-ghi" / "v1"
    bad.mkdir(parents=True)
    for f in (models_dir / "pp-ghi" / "v1").iterdir():
        (bad / f.name).write_bytes(f.read_bytes())
    data = json.loads((bad / "manifest.json").read_text())
    data["feature_schema_hash"] = "0" * 16
    (bad / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ModelArtifactError):
        load_model(tmp_path, "ghi")
    with pytest.raises(ModelArtifactError):
        load_model(tmp_path, "dni")


def test_retrain_without_training_rows_refuses(tmp_path: Path) -> None:
    with pytest.raises(MlUnavailableError):
        retrain(
            ghi_pairs(3), variable="ghi", cutoff_utc=START - timedelta(days=1),
            target_kind="telemetry", models_dir=tmp_path,
        )


def test_postprocessor_output_is_a_labelled_forecast_with_bands(models_dir: Path) -> None:
    pp = MlPostprocessor({"ghi": load_model(models_dir, "ghi")})
    out = pp.apply(upstream_ghi(), now_utc=START + timedelta(days=27))
    assert out is not None
    assert out.provider == ML_PROVIDER and out.dataset == ML_DATASET
    assert out.kind is DataKind.FORECAST
    for step in out.steps:
        m = step.values["ghi"]
        assert m.provenance.kind is DataKind.FORECAST
        assert m.provenance.model_version == "v1"
        assert m.provenance.training_cutoff_utc is not None
        assert "open-meteo/best_match" in (m.provenance.processing or "")
        assert m.uncertainty is not None
        assert m.uncertainty.lower <= m.value <= m.uncertainty.upper
    # Night steps are emitted as exactly zero, never dropped as "out of range".
    night = [s for s in out.steps if s.values["ghi"].value == 0.0]
    assert night and all(s.values["ghi"].uncertainty.upper == 0.0 for s in night)  # type: ignore[union-attr]


def test_postprocessor_refuses_observations_and_unknown_sources(models_dir: Path) -> None:
    pp = MlPostprocessor({"ghi": load_model(models_dir, "ghi")})
    up = upstream_ghi()
    assert pp.apply(up.model_copy(update={"kind": DataKind.OBSERVATION})) is None
    assert pp.apply(up.model_copy(update={"provider": "met-norway"})) is None


def test_cli_list_and_missing_model(models_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--models-dir", str(models_dir), "list"]) == 0
    line = json.loads(capsys.readouterr().out.strip())
    assert line["model_id"] == "pp-ghi" and line["version"] == "v1"
    assert np.isfinite(line["metrics"]["crps"])
