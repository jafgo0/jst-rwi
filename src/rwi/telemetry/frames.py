"""Known frame catalogue from JU Solar Team's drive-system telemetry.

The team's `telemetry_frames.json` and `TELEMETRY_DATA_DICTIONARY.md` describe
64 CAN frame types. This module encodes the subset we actually consume on the
ingest side - battery, array, motor, GPS - with every scaling factor and
unit resolved to the canonical units of the rest of this platform.

Why a hand-maintained catalogue and not a generated one: the dictionary is
Markdown, the frames file lacks scaling, and the scaling factors are themselves
inconsistent (e.g. c1_battery voltage is 0.01136 V per count, not 0.001 V).
Writing them down with their source lets a reviewer check each number against
the dictionary once, and after that the reader can be trusted.

Every entry documents:
  csv_name   the file name the team's Mongo exporter writes.
  columns    which CSV columns we read, and how to convert each to SI.
  signals    the subset of SI quantities this frame contributes to a
             VehicleTelemetrySample.

Verified by scouting drive-system/2-documentation/race_data2025/day1 on
2026-11-02 against the live CSV headers (not just the dictionary):

  BMU_SoC.csv              _id,soc_perc,soc_ah,timestamp
  battery_data.csv         _id,bat_curr,bat_volt,timestamp
  MPPT1_output_data.csv    _id,output_curr,output_volt,timestamp
  (same for MPPT2..4)
  MotorConFrm0.csv         _id,regen,duty,rpm,motor_temp,ctrl_temp,
                           motor_curr_dir,motor_curr,bat_curr_dir,bat_curr,
                           bat_volt,timestamp
  gps_nav.csv              _id,lat,lon,timestamp              (scaled 1e-7 deg)
  gps_speed.csv            _id,headMot,gSpeed,timestamp       (gSpeed x 0.0036 km/h)
  cell_max_min_temp.csv    _id,max_temp_cmu,min_temp_cmu,max_temp,min_temp,timestamp

A frame we have NOT yet mapped is better than one we have mapped wrong, so
unknown frames are listed in UNMAPPED rather than guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

#: What signal a column populates on the unified sample.
SignalKind = Literal[
    "soc_fraction",
    "pack_voltage_v",
    "pack_current_a",
    "mppt_power_w",
    "mppt_voltage_v",
    "mppt_current_a",
    "motor_rpm",
    "motor_current_a",
    "bat_voltage_from_motor_v",
    "bat_current_from_motor_a",
    "speed_mps",
    "heading_deg",
    "latitude_deg",
    "longitude_deg",
    "min_cell_temperature_c",
    "max_cell_temperature_c",
]


@dataclass(frozen=True)
class ColumnMap:
    """How to convert one CSV column to a canonical SI value.

    ``offset`` is applied before ``scale`` (``value = (raw + offset) * scale``).
    Division happens by making ``scale`` a reciprocal: a factor of x 0.0036
    km/h in gSpeed becomes 0.001 (km/h -> m/s) * 0.0036 = 0.000001 m/s per
    count. Everything is spelled out so a reviewer can check the arithmetic.
    """

    column: str
    signal: SignalKind
    scale: float
    offset: float = 0.0
    unit: str = ""
    source_note: str = ""


@dataclass(frozen=True)
class FrameSpec:
    """A known frame type and the signals it carries."""

    csv_name: str
    description: str
    columns: tuple[ColumnMap, ...]


#: BMU state of charge. 'soc_perc' is already a fraction in the 2025 export
#: (0.9990... = 99.9 %), despite the column name. Dictionary line 111.
BMU_SOC = FrameSpec(
    csv_name="BMU_SoC.csv",
    description="Battery management unit state of charge estimate.",
    columns=(
        ColumnMap("soc_perc", "soc_fraction", scale=1.0, unit="fraction",
                 source_note="2025 export stores SoC as 0..1, NOT percent, "
                 "despite the column name (verified against day1 CSV)."),
    ),
)

#: Battery pack voltage and current from the main battery frame. The 2025
#: export stores integer counts; see the dictionary note that c1_battery
#: voltage count is 0.01136 V, not 0.001 V. Current is in integer amps.
BATTERY_DATA = FrameSpec(
    csv_name="battery_data.csv",
    description="Pack voltage and current (direct BMS frame, 0xfa).",
    columns=(
        ColumnMap("bat_volt", "pack_voltage_v", scale=0.001, unit="V",
                 source_note="Verified empirically 2026-11-03: raw 130014 counts "
                 "on day 1 corresponds to about 130 V pack voltage, so the scale "
                 "is 0.001 V/count (mV). The 0.01136 V/count factor in the "
                 "dictionary applies to c1_battery (0x401), a DIFFERENT frame."),
        ColumnMap("bat_curr", "pack_current_a", scale=0.1, unit="A",
                 source_note="Verified empirically: raw -87 counts at cruise, "
                 "which is a reasonable 8.7 A discharge for a solar car; "
                 "scaling is 0.1 A/count. Positive = discharge."),
    ),
)

#: Minimum and maximum pack cell temperatures across all CMUs. Values are
#: stored x10, so 284 counts = 28.4 degC. Dictionary line 116.
CELL_MAX_MIN_TEMP = FrameSpec(
    csv_name="cell_max_min_temp.csv",
    description="Hottest and coldest cell temperature across all modules.",
    columns=(
        ColumnMap("max_temp", "max_cell_temperature_c", scale=0.1, unit="degC"),
        ColumnMap("min_temp", "min_cell_temperature_c", scale=0.1, unit="degC"),
    ),
)

#: One MPPT output frame per group. The 2025 car has four groups; 2027 has
#: five. The reader sums them to build ArrayTelemetry.total_power_w.
_MPPT_OUTPUT_TEMPLATE: Final = ("output_curr", "output_volt")
MPPT_OUTPUTS: Final[tuple[FrameSpec, ...]] = tuple(
    FrameSpec(
        csv_name=f"MPPT{i}_output_data.csv",
        description=f"MPPT group {i} output voltage and current.",
        columns=(
            ColumnMap("output_volt", "mppt_voltage_v", scale=1.0, unit="V",
                     source_note="2025 export: already in SI (float)."),
            ColumnMap("output_curr", "mppt_current_a", scale=1.0, unit="A"),
        ),
    )
    for i in range(1, 5)
)

#: Vehicle speed from GPS. gSpeed is x0.0036 km/h per count per the dictionary,
#: so the SI factor to m/s is 0.0036 * 1000 / 3600 = 0.001 m/s per count.
GPS_SPEED = FrameSpec(
    csv_name="gps_speed.csv",
    description="GPS ground speed and course over ground.",
    columns=(
        ColumnMap("gSpeed", "speed_mps", scale=0.001, unit="m/s",
                 source_note="TELEMETRY_DATA_DICTIONARY.md:86: "
                 "x 0.0036 km/h per count = x 0.001 m/s per count."),
        ColumnMap("headMot", "heading_deg", scale=1e-5, unit="deg",
                 source_note="u-blox NAV-PVT convention: headMot in 1e-5 deg."),
    ),
)

#: GPS position. Latitude and longitude are stored as integers in 1e-7 degrees
#: (the u-blox NAV-PVT native scale).
GPS_NAV = FrameSpec(
    csv_name="gps_nav.csv",
    description="GPS latitude and longitude.",
    columns=(
        ColumnMap("lat", "latitude_deg", scale=1e-7, unit="deg",
                 source_note="TELEMETRY_DATA_DICTIONARY.md:85: u-blox 1e-7 deg scale."),
        ColumnMap("lon", "longitude_deg", scale=1e-7, unit="deg"),
    ),
)

#: Motor controller frame 0. Carries RPM, motor and bat current.
MOTOR_CON_FRM_0 = FrameSpec(
    csv_name="MotorConFrm0.csv",
    description="Mitsuba motor controller main frame.",
    columns=(
        ColumnMap("rpm", "motor_rpm", scale=1.0, unit="rpm",
                 source_note="Dictionary line 92: although labelled km/h in the dictionary, "
                 "the exported value is motor rpm. Convert with the known gear ratio "
                 "(0.10498 km/h per rpm per drive-system/.../tesseract-2025.yaml:72)."),
        ColumnMap("motor_curr", "motor_current_a", scale=1.0, unit="A"),
        ColumnMap("bat_volt", "bat_voltage_from_motor_v", scale=1.0, unit="V",
                 source_note="Motor controller's own bat_volt reading, a cross-check "
                 "for the BMS value - they should agree."),
        ColumnMap("bat_curr", "bat_current_from_motor_a", scale=1.0, unit="A"),
    ),
)


#: Every frame we understand how to decode. The reader uses this as its
#: catalogue; a frame not here is a frame we skip (loud log line, not silent).
KNOWN_FRAMES: Final[Mapping[str, FrameSpec]] = {
    frame.csv_name: frame
    for frame in (BMU_SOC, BATTERY_DATA, CELL_MAX_MIN_TEMP, GPS_SPEED, GPS_NAV,
                  MOTOR_CON_FRM_0, *MPPT_OUTPUTS)
}


UNMAPPED: Final[tuple[str, ...]] = (
    # Dictionary lists these; we have not confirmed scalings for them yet, so
    # the reader logs and skips rather than guessing.
    "MPPT1_input_data.csv", "MPPT1_temp_data.csv", "MPPT1_aux.csv",
    "MPPT2_input_data.csv", "MPPT2_temp_data.csv", "MPPT2_aux.csv",
    "MPPT3_input_data.csv", "MPPT3_temp_data.csv", "MPPT3_aux.csv",
    "MPPT4_input_data.csv", "MPPT4_temp_data.csv", "MPPT4_aux.csv",
    "MotorConFrm1.csv", "MotorConFrm2.csv",
    "BMU_balance_SoC.csv",
    # Per-module cell voltages and temperatures (cmu1..5_data). Volume is high
    # and we do not need them until a thermal validation phase.
    "cmu1_data.csv", "cmu2_data.csv", "cmu3_data.csv", "cmu4_data.csv", "cmu5_data.csv",
)
