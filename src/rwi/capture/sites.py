"""Where the platform builds its verification history.

A forecast can only be scored where an observation exists to score it against.
That makes the choice of capture sites a scientific decision, not a convenience:
sites are chosen so that the race route is represented AND a measuring station
is close enough that the pair is meaningful.

The BWSC control stops are the natural choice. They are real, named places on
the route, the vehicle stops at every one of them for 30 minutes, and several
sit at or near airports with Bureau of Meteorology stations.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from rwi.core.geo import GeoPoint


class CaptureSite(BaseModel):
    """One place where forecasts and observations are archived over time."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    point: GeoPoint
    route_distance_m: float | None = Field(
        default=None, description="chainage along the BWSC route, when the site is on it"
    )
    rationale: str = Field(description="why this site earns a provider call every cycle")


DARWIN = CaptureSite(
    id="darwin",
    name="Darwin",
    point=GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0),
    route_distance_m=0.0,
    rationale="Start. Darwin Airport is a BoM station, so pairs are available here.",
)
KATHERINE = CaptureSite(
    id="katherine",
    name="Katherine",
    point=GeoPoint(latitude_deg=-14.4652, longitude_deg=132.2635, elevation_m=108.0),
    route_distance_m=315_873.0,
    rationale="Control stop 1; first day's target. Tindal/Katherine stations nearby.",
)
TENNANT_CREEK = CaptureSite(
    id="tennant-creek",
    name="Tennant Creek",
    point=GeoPoint(latitude_deg=-19.6486, longitude_deg=134.1890, elevation_m=376.0),
    route_distance_m=986_600.0,
    rationale="Control stop 3; airport station on site. Mid-NT climate regime.",
)
ALICE_SPRINGS = CaptureSite(
    id="alice-springs",
    name="Alice Springs",
    point=GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0),
    route_distance_m=1_492_500.0,
    rationale=(
        "Control stop 5, the route's halfway point and its highest-quality station. "
        "Verified working: a BoM observation here paired with a forecast during development."
    ),
)
COOBER_PEDY = CaptureSite(
    id="coober-pedy",
    name="Coober Pedy",
    point=GeoPoint(latitude_deg=-29.0139, longitude_deg=134.7544, elevation_m=225.0),
    route_distance_m=2_176_900.0,
    rationale="Control stop 7; northern South Australia, a distinct regime from the NT.",
)
PORT_AUGUSTA = CaptureSite(
    id="port-augusta",
    name="Port Augusta",
    point=GeoPoint(latitude_deg=-32.4927, longitude_deg=137.7652, elevation_m=12.0),
    route_distance_m=2_714_600.0,
    rationale=(
        "Control stop 9, where the route meets the coast and the marine influence "
        "makes forecasts behave differently from the inland sites."
    ),
)
ADELAIDE = CaptureSite(
    id="adelaide",
    name="Adelaide",
    point=GeoPoint(latitude_deg=-34.9285, longitude_deg=138.6007, elevation_m=50.0),
    route_distance_m=3_021_167.0,
    rationale="Finish. Dense station coverage; the final day's weather decides the arrival.",
)

DEFAULT_SITES: tuple[CaptureSite, ...] = (
    DARWIN,
    KATHERINE,
    TENNANT_CREEK,
    ALICE_SPRINGS,
    COOBER_PEDY,
    PORT_AUGUSTA,
    ADELAIDE,
)
"""Seven sites spanning the route and its climate regimes.

Deliberately not more: every site costs a provider call per cycle, the free
Open-Meteo tier allows 10,000 calls a day, and sites without a nearby station
produce forecasts that can never be scored. Coordinates are the towns, not the
control-stop entrances; `docs/science/archive.md` records that difference.
"""

SITES_BY_ID: dict[str, CaptureSite] = {site.id: site for site in DEFAULT_SITES}


def sites_from_ids(ids: list[str] | None) -> tuple[CaptureSite, ...]:
    """Resolve site ids, failing loudly on an unknown one."""
    if not ids:
        return DEFAULT_SITES
    unknown = [i for i in ids if i not in SITES_BY_ID]
    if unknown:
        raise ValueError(
            f"unknown capture site(s): {', '.join(unknown)}; "
            f"known: {', '.join(sorted(SITES_BY_ID))}"
        )
    return tuple(SITES_BY_ID[i] for i in ids)
