# ADR 0010 — On-board sensor ingestion: Airmar 200WX and EKO ML-01

Status: **Accepted** 2026-11-27. The owner approved (a) `TelemetrySource.LIVE_ONBOARD_SENSOR` in `rwi.contracts` and (b) the sensor-height wind variables in `rwi.core.variables`.
Date: 2026-11-27
Requirements: #29 Solar Irradiance Measurement, #34 Sensor Calibration, #73 Data Assimilation, #79 Real-Time Data Processing
Related: ADR 0007 (SQLite archive), ADR 0008 (ML post-processing needs observations), ADR 0009 (NMEA 2000 decoding)

## Context

The platform has no measured irradiance and no wind measured at the car (REQUIREMENTS §gaps). ADR 0008 found that the archive cannot train an irradiance model because it holds no irradiance observations. The team is fitting two sensors:

- **Airmar 200WX** (NMEA 2000 via an Actisense NGX-1, decoded per ADR 0009). It measures apparent wind, air temperature, humidity and pressure, and has its own GNSS, heading and attitude.
- **EKO ML-01.** A silicon-photodiode pyranometer, 400–1100 nm, with an output of about 50 µV per W/m² over 0–100 mV. An external ADC reads it and delivers either mV or W/m².

Whether either sensor may sit on the competition vehicle is still open (regulations question #9). The mounting location must therefore be configuration, and it must be carried in provenance.

The car's uplink is Starlink, and an outage must neither lose data nor fill it in.

## Decision

### 1. Contract and registry changes (owner-approved)

- **`TelemetrySource.LIVE_ONBOARD_SENSOR`.** A new enum value meaning "read live from a sensor on the car or an escort". The coordinator treats it as live: freshness-gated, and a GNSS fix goes through `assess_live_fix`. Nothing else in `rwi.contracts` changes. The enum is not on the product surface, so the product schema does not move.
- **Observation-only variables:** `wind_speed_sensor` (m/s) and `wind_direction_sensor` (°, from).
  - They are defined in a separate `OBSERVATION_ONLY_VARIABLES` registry in `rwi.core.variables`, and `spec_for` looks in both registries.
  - `CANONICAL_VARIABLES` stays the set that forecasts can serve. `rwi.providers.openmeteo` asserts that its mapping covers exactly that set, so adding the new names there would have forced a fake Open-Meteo mapping.
  - Sensor height is required config and goes in provenance. Correcting the wind to 10 m is a later phase; until then the sensor wind is never labelled `wind_speed_10m`.

### 2. True wind

The Airmar reports *apparent* wind: speed AWS, and angle AWA clockwise from the bow. True wind is computed only from records whose Reference is "Apparent":

```
dir_from_apparent = heading_true + AWA                 (° from true north)
(u_a, v_a)        = met_to_uv(AWS, dir_from_apparent)  (air motion)
(u_c, v_c)        = SOG · (sin COG, cos COG)           (vehicle motion over ground)
(u_t, v_t)        = (u_a + u_c, v_a + v_c)
speed, dir_from   = uv_to_met(u_t, v_t)
```

- The check case: driving north at 10 m/s through calm air gives AWS = 10 m/s and AWA = 0°, and the result is a true wind of 0.
- **Heading:**
  - heading_true is the PGN 127250 heading when its reference is True;
  - when the reference is Magnetic, heading_true = heading + variation, and only when variation is present in the same or a recent record;
  - otherwise no true wind is produced. Variation is never assumed to be 0.
- COG/SOG come from PGN 129026.
- Every input must sit inside the same 1 s window. A missing input means no true wind for that window, never one carried over from an older window.
- Provenance records `processing="true_wind_from_apparent+cog_sog"`, the heading reference that was used, and the mount.

### 3. Thermodynamics and pressure

- **Temperature and humidity** are archived as `temperature_2m` / `relative_humidity_2m` only when the configured sensor height is within the WMO-No. 8 screen-height band of 1.25–2.0 m. Otherwise they are counted and kept in the spool but not archived as screen-level values.
- The note on these values states the known biases: the sensor is aspirated by the vehicle's own motion, there is road-surface heating, and on the solar car there is array heat.
- **Pressure** is archived as `surface_pressure`, with the sensor height in the note. It is not reduced to MSL.

### 4. ML-01 calibration and level gate

- **Conversion:** W/m² = mV × 1000 / sensitivity_µV_per_Wm2.
  - `RWI_ML01_SENSITIVITY_UV_PER_WM2` is required and has no default. The ~50 µV per W/m² figure is nominal, and the real value is on the individual sensor's certificate.
  - `RWI_ML01_SERIAL` and `RWI_ML01_CALIBRATION_DATE` are also required.
  - A missing key raises `SensorConfigError`, which lists every missing key at once.
- **Calibration age:** a calibration more than 2 years old logs a warning and is noted in provenance. Values are still produced, because the platform cannot judge drift without a reference.
- **Provenance:** ML-01 values carry `dataset="eko-ml01"` and `model_name=<serial>`, and the note says "silicon pyranometer, 400–1100 nm". It also records the calibration date and that silicon sensors have a spectral-mismatch error that grows under cloud and at low sun. Its size is not quantified until the sensor has been compared with a thermopile reference.
- **Level gate:**
  - GHI is only GHI if the sensor is level. Tilt comes from optional pitch/roll columns in the ML-01 input, or from Airmar attitude (PGN 127257) when `ml01_mount` says the two share a mount.
  - If tilt exceeds `ml01_level_tolerance_deg`, or tilt is unknown and `ml01_assume_level` is not set, the value is flagged `SENSOR_SUSPECT` and not archived as `ghi`.
  - `ml01_level_tolerance_deg` is required when tilt is available; there is no guessed default.
- **Range:** values outside the registry range of `ghi` are flagged `OUT_OF_RANGE` and never clamped. A negative night offset is kept as measured.

### 5. Ingest, buffering and backfill

- Readings are aggregated into **1 s windows** aligned on the UTC second. Wind is vector-averaged through u/v; scalars use the arithmetic mean.
  - A window closes once the newest input time is at least the window end plus a late tolerance.
  - A reading that arrives after its window has closed is counted as late and never merged.
- Each closed window is written to an **edge spool**: SQLite at `RWI_SENSOR_SPOOL_PATH`, default `data/spool/sensors.sqlite3`, which is gitignored.
  - Writes are idempotent on (stream, window start). Every row carries a forwarded flag for each sink.
  - Flushing goes oldest-first. A sink that raises leaves its rows pending.
- **Live:**
  - Rows fresh enough for the coordinator's freshness limit go to the live sink: a queue-backed `TelemetryReader` that the `TelemetryCoordinator` consumes.
  - Older rows are backfilled to the archive only and are never presented as live.
  - If the Airmar GNSS has a fix and `airmar_drive_position=true`, it drives `live_gps` through the coordinator's existing gate.
- **Gaps:** missing 1 s windows are reported as `TelemetryGap` records. No value is ever interpolated across an outage.
- **Archive:** every `sensor_archive_interval_s` (default 60 s), the closed windows are averaged into one OBSERVATION `WeatherSeries` and written to `SqliteArchive.record_series`.
  - Provider is `jst-onboard`, dataset is `airmar-200wx` or `eko-ml01`, and model_name is the serial number.
  - The point is the mean position over the interval.
  - This gives `rwi.ml` forecast/observation pairs through the existing `pairs()` path.

### 6. Replay

- Recorded canboat JSONL and ML-01 CSV (`time_utc,value[,pitch_deg,roll_deg]`) replay through the same aggregation, spool and archive code.
- In replay, the source is `FILE_REPLAY` and position fixes use `HISTORICAL_REPLAY`, so a replayed drive can never pass for live.

## Consequences

- Measured GHI and in-situ wind become available to verification (`rwi.verification`) and to ML training (ADR 0008) as soon as the hardware runs.
- With the Airmar on a support car, the measured wind and the GNSS position are the support car's, not the solar car's. The mount is in provenance so that every consumer can tell. Driving `live_gps` from it is a separate explicit flag.
- A remote HTTP sink, which pushes the spool from the car to a ground station over Starlink, is the next step and is not part of this ADR. The spool's per-sink forwarded flags are designed for it.
- No height correction for wind, and no thermopile cross-calibration, until a later phase.

## Alternatives considered

- **Put the sensor wind in `wind_speed_10m`.** Rejected. It would be wrong by the wind-profile factor at a height of about 1.5 m, and it would silently bias verification.
- **Interpolate across short outages.** Rejected by the prompt and by hard rule 1: an interpolated value is not an observation.
- **Ingest directly into the in-process `StateStore` without a spool.** Rejected. A process restart or a Starlink outage would lose data.
- **Default the ML-01 sensitivity to the nominal 50 µV per W/m².** Rejected. Individual sensors differ by several percent, and a guessed constant produces plausible wrong irradiance.

## Status

Accepted.
