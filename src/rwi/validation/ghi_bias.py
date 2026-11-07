"""Point-wise GHI bias lookup from the BWSC-2025 quadrangulation.

The quadrangulation in ``scripts/validation/bwsc_2025_analysis_vs_satellite.py``
produced a daily-mean bias of three reanalysis GHI products (ECMWF-IFS,
NASA POWER, MERRA-2) against BoM-anchored SILO at six route points over
race week. This module exposes that result so the live dashboard can
show a strategist, at the point they are at, whether gridded GHI
tends to read high or low against the BoM ground reference.

Not a dynamic correction. Not applied silently to any forecast. A chip
on the dashboard, nothing more. The measurement is one 7-day window in
late winter 2025, so it is a signal to be aware of, not a calibration.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["BiasPoint", "GhiBiasTable", "load_default_table"]

_DATA_RELATIVE = Path("data/validation/ghi_bias_route_points_bwsc_2025.json")


@dataclass(frozen=True, slots=True)
class BiasPoint:
    name: str
    lat: float
    lon: float
    bias_ecmwf: float
    bias_power: float
    bias_merra2: float
    extra: dict[str, Any]

    @property
    def bias_median(self) -> float:
        values = sorted([self.bias_ecmwf, self.bias_power, self.bias_merra2])
        return values[1]

    @property
    def bias_mean(self) -> float:
        return (self.bias_ecmwf + self.bias_power + self.bias_merra2) / 3.0


@dataclass(frozen=True, slots=True)
class GhiBiasTable:
    window_utc: tuple[str, str]
    truth: str
    unit: str
    note: str
    points: tuple[BiasPoint, ...]

    def nearest(self, lat: float, lon: float) -> tuple[BiasPoint, float]:
        """Return the nearest tabulated point and the great-circle distance in km."""
        if not self.points:
            raise ValueError("no bias points loaded")
        nearest = min(self.points, key=lambda p: _haversine_km(lat, lon, p.lat, p.lon))
        return nearest, _haversine_km(lat, lon, nearest.lat, nearest.lon)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise FileNotFoundError("could not locate repo root (no pyproject.toml above)")


@lru_cache(maxsize=1)
def load_default_table() -> GhiBiasTable:
    path = _repo_root() / _DATA_RELATIVE
    raw = json.loads(path.read_text(encoding="utf-8"))
    pts: list[BiasPoint] = []
    for p in raw["points"]:
        b = p["bias_vs_silo"]
        extra = {k: v for k, v in p.items() if k not in {"name", "lat", "lon", "bias_vs_silo"}}
        pts.append(BiasPoint(
            name=p["name"], lat=float(p["lat"]), lon=float(p["lon"]),
            bias_ecmwf=float(b["ecmwf"]), bias_power=float(b["power"]),
            bias_merra2=float(b["merra2"]),
            extra=extra,
        ))
    return GhiBiasTable(
        window_utc=(raw["window_utc"][0], raw["window_utc"][1]),
        truth=raw["truth"], unit=raw["unit"], note=raw["note"],
        points=tuple(pts),
    )
