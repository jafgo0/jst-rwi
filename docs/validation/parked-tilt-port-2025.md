# Parked-tilt port and what it uncovered (2026-11-03)

Follow-up to `retrospective-5-days-2025.md`. The previous retrospective found
the shadow matrix was mysteriously hurting the modelled power on long
averages. Two hypotheses were ruled out (the matrix is the 2025 car, not the
2027 car; the sign of the vehicle-frame azimuth doesn't matter). This
retrospective ports the third: the team's own simulator has two separate
solar-intake paths - one while the car is moving, one while it is parked -
and we only had the moving path.

## What was ported

Read-only from the team's `drive-system` repo (we touch nothing there):

* `_parked_relative_sun_position` from `intake/p_intake_model.py`
* `_moving_relative_sun_position` from the same file
* The four constants in `config.py`:
  * `tilt_eff_control_stop = 0.95` (precise 30-min aim)
  * `tilt_eff_morning_evening = 0.90` (periodic re-aim)
  * `env_derating = 0.90` (shade, dust, site quality)
  * `parked_tilt_enabled = True`

Our port lives in `rwi.validation.pv` because the heuristic is retrospective
analysis: it needs ground-truth movement data to decide which path. The
central `rwi.energy.array` stays flat-panel; production can wire in a
`parked` flag later, from telemetry rather than guessed from GPS speed.

Each `HourlyComparison` now carries `vehicle_speed_mps` and `activity`
(`"moving"`, `"parked"`, or `"control_stop"`), and `build_report` picks
between the paths with `ParkedTiltModel.moving_speed_threshold_mps = 2.0`.
A parked hour between 09:30 and 15:30 ACST is a control stop; outside that
band it is morning charge or evening camp.

## What came out

Hour-by-hour over all five race days, matrix enriched, parked-tilt active:

| Day     | Hour UTC | Speed km/h | Activity     | Model   | Measured | Ratio   |
|---------|---------:|-----------:|--------------|--------:|---------:|--------:|
| day1    |  22:00   |    92.4    | moving       |    1.7W |  1034.3W |  0.00x  |
| day1    |  23:00   |    95.5    | moving       |   76.2W |   623.7W |  0.12x  |
| day1    |  00:00   |     0.2    | control_stop |  470.6W |  1031.1W |  0.46x  |
| day1    |  01:00   |     0.2    | control_stop |  706.4W |   555.1W |  1.27x  |
| day3    |  23:00   |    75.0    | moving       |   12.3W |   524.9W |  0.02x  |
| day3    |  00:00   |    73.0    | moving       |  166.7W |   337.7W |  0.49x  |
| day3    |  01:00   |    44.0    | moving       |  357.1W |   229.6W |  1.56x  |
| day3    |  02:00   |     0.0    | control_stop |  732.0W |    38.3W | 19.13x  |
| day4    |  23:00   |    40.1    | moving       |   32.4W |  1032.0W |  0.03x  |
| day4    |  00:00   |    83.2    | moving       |  167.0W |   523.3W |  0.32x  |
| day4    |  01:00   |    54.8    | moving       |  320.2W |   219.1W |  1.46x  |
| day4    |  02:00   |     0.0    | control_stop |  652.0W |   176.2W |  3.70x  |
| day5    |  23:00   |    36.2    | moving       |    8.4W |   506.9W |  0.02x  |

Aggregates with the ported logic active:

| Class            | n | Model     | Measured  | MAE     | Ratio  |
|------------------|---|-----------|-----------|---------|--------|
| ALL              | 13|   284.8W  |   525.6W  | 479.1W  | 0.54x  |
| MOVING           |  9|   126.9W  |   559.1W  | 483.0W  | 0.23x  |
| CONTROL_STOP     |  4|   640.2W  |   450.2W  | 470.3W  | 1.42x  |

## What this reveals

The port is a correct translation of the team's own algorithm. It did not
move the aggregate toward 1.0 because MPPT power is the wrong ground truth
for this question.

Look at `day3 02:00 UTC` (11:30 ACST, mid-day, parked): the sun is strong,
the panel is manually aimed at it, and the car measured **38 W**. Look at
`day1 00:00 UTC` (09:30 ACST, morning charge, parked): the sun is lower, the
panel aim is less precise, and the car measured **1031 W**. The two parked
hours differ by 27x in measured power. No physical model of panel capability
will land both.

The explanation sits one layer up from the panel: MPPT-measured power is
*battery acceptance*, not panel capability. At the mid-day control stop the
pack is likely close to full, so the MPPTs curtail; in the morning the pack
is near empty, so the MPPTs pull everything the panel can give. Our model
predicts *what the panel can deliver*; the data we're comparing against
measures *what the pack would take*.

For moving hours it is more subtle: ratios 0.02x-1.56x within a single day
indicate something else is still off. Candidates: hourly aggregation lumping
short stops with driving; our DEFAULT_ARRAY still too close to the 2027
build and not to the 2025 panel; wind-tunnel-grade POA model not matching
the simplistic GHI-only path the team uses while moving. We don't know yet.

## What is now true in the repo

* `rwi.validation.pv.ParkedTiltModel`: dataclass holding the team's four
  constants. Default values are the team's own.
* `rwi.validation.pv.read_hourly_speeds`: mean ground speed per hour from
  `gps_speed.csv`.
* `rwi.validation.pv._is_control_stop_hour`: ACST window helper.
* `rwi.validation.pv._parked_hour_power_w`: direct port of the team's
  parked-tilt power formula, GHI x area x efficiency x matrix-at-az-0 x
  temperature x env_derating.
* `HourlyComparison` carries `vehicle_speed_mps` and `activity`.
* `build_report` dispatches between moving and parked paths.

## What stays wrong

No competency moves. Row #115 "Solar Energy Production Forecasting" stays at
its earlier status, and the parked path does not retrospectively validate
the energy model, because the measurement it is being validated against
cannot answer the question.

## What a useful next step would be

Not more code against this dataset. One of:

1. Get battery SOC per hour from the team's data (there is a MongoDB export
   somewhere - the parent folder has `battery_bms_master.csv`). An hour
   where SOC was 20-80 % is one where MPPT power equals panel capability.
2. Get pre-MPPT panel current/voltage logs. Those are panel capability
   directly, independent of pack state.
3. Validate against the team's own simulator output instead of raw MPPT
   data - that compares two models with the same assumptions and isolates
   our port.

Option 1 is the smallest step; option 2 is the cleanest; option 3 is the
cheapest.
