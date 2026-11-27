# On-board sensors (`rwi.telemetry`): Airmar 200WX and EKO ML-01

Owner: telemetry. Code: `src/rwi/telemetry/{nmea2000,airmar,ml01,onboard,spool,ingest,onboard_cli}.py`.
Tests: `tests/unit/test_telemetry_{nmea2000,airmar,ml01,spool,ingest}.py`. Decision records:
`docs/adr/0009-nmea2000-decoding-via-canboat.md`, `docs/adr/0010-onboard-sensor-ingestion.md`.
Requirements: #29, #34, #73, #79.

Every value produced here is `DataKind.OBSERVATION` from provider `jst-onboard`, dataset
`airmar-200wx` or `eko-ml01`, `model_name` = the sensor serial. Nothing is interpolated,
clamped or defaulted.

## 1. Decoding (Airmar, ADR 0009)

The NGX-1 bus stream is decoded by canboat `analyzer -json -si`; `CanboatDecoder` reads its
JSON lines. The stream must start with the JSON banner and declare `-si` units, otherwise it is
refused. Angles arrive in radians and are converted to degrees (headings wrapped to [0, 360),
pitch/roll kept signed); temperature in K → °C; humidity fraction → %. Timestamps must carry a
zone (or `naive_is_utc` must be set explicitly) and are converted to UTC. Malformed lines,
unused PGNs and missing fields are counted, not fatal.

PGNs used: 130306 wind, 129026 COG/SOG, 127250 heading, 127257 attitude, 130310–130314
temperature, humidity and pressure, 129025/129029 position (129029 also method, satellites, HDOP).

## 2. True wind

The Airmar measures *apparent* wind. True wind is the vector sum of the apparent air motion,
rotated to true north, and the vehicle's velocity over ground:

```
dir_from_apparent = heading_true + AWA
(u_a, v_a) = met_to_uv(AWS, dir_from_apparent)
(u_c, v_c) = SOG · (sin COG, cos COG)
true       = uv_to_met(u_a + u_c, v_a + v_c)
```

Checks (science tests): calm air at 10 m/s north → 0 m/s; 25 m/s apparent on the bow at
20 m/s south → 5 m/s from 180°; 10√2 m/s at 45° while doing 10 m/s north → 10 m/s from 90°.

- Magnetic heading becomes true only with variation from the bus (`+ deviation` when present).
  Variation is never assumed to be 0; without it there is no true wind for that window.
- All inputs must fall inside the same 1 s window. Moving (SOG > 0) without COG is an error.
- Provenance: `processing` starts with `true_wind_from_apparent+cog_sog`, and notes carry the
  heading reference used and the mount.
- Archived as `wind_speed_sensor` (scalar mean of the 1 s speeds) and `wind_direction_sensor`
  (direction of the mean u/v vector).

## 3. Temperature, humidity, pressure

Temperature and RH are archived as `temperature_2m` / `relative_humidity_2m` only when
`RWI_AIRMAR_HEIGHT_M` is inside the WMO-No. 8 screen band, 1.25–2.0 m. Outside it they stay in
the spool. Known biases (motion aspiration, road-surface heating, array heat on the solar car)
are stated in the note, not corrected. Pressure is `surface_pressure` at sensor height, not
reduced to MSL.

## 4. EKO ML-01

- Conversion: `W/m² = mV · 1000 / sensitivity_µV_per_Wm2`. `RWI_ML01_SENSITIVITY_UV_PER_WM2`,
  `RWI_ML01_SERIAL`, `RWI_ML01_CALIBRATION_DATE` and `RWI_ML01_INPUT_UNIT` (`mv|wm2`) are
  required; start-up fails listing every missing key. The certificate value is the only valid
  sensitivity.
- Calibration age: more than 2 years before the observation date → warning in the log and a
  provenance note. Values are still produced; drift cannot be judged without a reference.
- Silicon note on every value: 400–1100 nm silicon pyranometer, spectral-mismatch error grows
  under cloud and at low sun, unquantified until compared with a thermopile reference.
- Output above the 100 mV manufacturer range → `OUT_OF_RANGE` plus a note. GHI outside the
  registry range → `OUT_OF_RANGE`. Never clamped; a negative night offset is kept.

### Level gate

GHI is only GHI from a level sensor. Tilt = `acos(cos pitch · cos roll)`, from the ML-01's
own pitch/roll columns or, with `RWI_ML01_MOUNT=shared_with_airmar`, from Airmar PGN 127257 in
the same 1 s window. A window is level when tilt ≤ `RWI_ML01_LEVEL_TOLERANCE_DEG`; with tilt
unknown it is level only if `RWI_ML01_ASSUME_LEVEL=true` (noted in provenance). The archived
interval mean uses level windows only and records `level windows=N/M`; an interval with no level
window is not archived as `ghi`.

Tilt known but no tolerance configured is a configuration error. If it is found only in the
data, the archive drain logs it, reports `failed`, and leaves those windows pending in the spool
so they backfill once the tolerance is set.

## 5. Windows, spool, archive

- 1 s windows aligned on the UTC second. A window closes when the newest input is past its end
  plus `RWI_SENSOR_LATE_TOLERANCE_S` (0.5 s); later readings are counted as late, never merged.
  Wind is averaged through u/v, scalars arithmetically.
- Every closed window goes to the SQLite spool (`RWI_SENSOR_SPOOL_PATH`), idempotent on
  (stream, window start), with a forwarded flag per sink. Flushes run oldest-first; a sink that
  raises leaves its rows pending. Nothing is lost when the archive or the uplink is down.
- Live sink: windows younger than 60 s feed the `TelemetryCoordinator` as
  `LIVE_ONBOARD_SENSOR`; older ones go to the archive only and are never shown as live. With
  `RWI_AIRMAR_DRIVE_POSITION=true` a GNSS fix drives `live_gps` through the coordinator's gate.
- Missing 1 s windows become `TelemetryGap` records.
- Archive: every `RWI_SENSOR_ARCHIVE_INTERVAL_S` (60 s) the closed windows are averaged into one
  `WeatherSeries` (`temporal_resolution_s` = interval, coverage `windows=N/60` in the notes) at
  the mean position, via `SqliteArchive.record_series`. That gives `rwi.ml` forecast/observation
  pairs through `pairs()`.

## 6. Replay

`python -m rwi.telemetry.onboard_cli replay --canboat <jsonl> --ml01 <csv> --spool <file>` runs recorded data
through the same windowing, spool and archive code, with its own spool so it never mixes with the live one. Source is `FILE_REPLAY` and fixes are
`HISTORICAL_REPLAY`, so a replayed drive never passes for live.

## 7. Limitations

- An ML-01 on a support car with no co-located Airmar has no position: its windows stay in the
  spool as `no_position` until a position source exists.
- The live path carries position only; sensor weather values reach the platform via the archive.
- No HTTP uplink sink yet (car → server); next step.
- `run_live` does not restart a pump process that dies.
- Silicon spectral error and the thermal biases of a vehicle-mounted Airmar are documented, not
  corrected.
