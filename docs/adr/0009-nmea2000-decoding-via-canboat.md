# ADR 0009 — NMEA 2000 decoding through canboat, not an in-house decoder

Status: **Accepted** 2026-11-27
Date: 2026-11-27
Requirements: #29 Solar Irradiance Measurement, #34 Sensor Calibration, #79 Real-Time Data Processing

## Context

The team will fit an Airmar 200WX ultrasonic weather station. It speaks NMEA 2000 and reaches a computer through an Actisense NGX-1 USB gateway. We need these PGNs:

| PGN | Content |
|---|---|
| 130306 | Wind data: speed, angle, reference (apparent/true) |
| 130310 | Environmental parameters: air and water temperature, pressure |
| 130311 | Environmental parameters: temperature, humidity, pressure with source codes |
| 130312 / 130313 / 130314 | Temperature / humidity / actual pressure |
| 127250 | Vessel heading: heading, deviation, variation, reference |
| 127257 | Attitude: yaw, pitch, roll |
| 129025 / 129026 / 129029 | Position rapid update, COG/SOG rapid update, GNSS position data |

NMEA 2000 framing has several hard parts: fast-packet reassembly across CAN frames, per-PGN bit layouts, reserved and "data not available" sentinels, and the Actisense BST wire format. canboat (Apache-2.0) does all of this. It ships `actisense-serial` to read the NGX-1 and `analyzer -json` to decode. With `-si` it emits SI units (K, Pa, rad, m/s) and puts a `{"version":…,"units":"si"}` banner first.

## Decision

1. **canboat runs as an external process.** The live pipeline is `actisense-serial -r <device> | analyzer -json -si`. The command line is configuration, never hardcoded. RWI reads its stdout one JSON line at a time. Recording that stdout to a file gives the replay format for free.
2. **RWI decodes canboat JSON, not CAN frames.** `rwi.telemetry.nmea2000` maps canboat field names, as of canboat.json v10.0.0, to typed records. Field names are matched exactly. A record missing a field gets `None` for that value; a guessed alias is never used.
3. **The SI banner is required.** A stream whose first non-empty line is not a banner with `"units":"si"` is rejected. Without `-si`, temperatures arrive in °C or °F and the numbers would be silently wrong.
4. **Units convert at the boundary only:** K → °C, rad → °, a 0–1 ratio → %, and Pa → hPa where the canonical unit is hPa. Inside RWI every value already has its canonical unit.
5. **Timestamps must be timezone-aware.** canboat's `timestamp` is the host clock. A naive timestamp is rejected unless `airmar_canboat_naive_utc=true` declares the host clock to be UTC. GNSS time from PGN 129029, when present, is stored next to the host time and does not replace it.
6. **Undecodable lines are counted, not fatal.** Malformed JSON, unknown PGNs and PGNs we do not use each increment their own counter. The stream keeps going.

## Consequences

- canboat must be installed on the capture host. It is a system tool built from source or from Homebrew, not a Python dependency, so `pyproject.toml` does not change. Tests never call it: they use small JSONL fixtures in canboat output format.
- A canboat release that renames a field makes that value `None`, and the decoder counts it as "field missing". That is loud and visible rather than silently wrong. The decoder is pinned to the v10.0.0 field names, and the version string from the banner is recorded in provenance.
- Any other NMEA 2000 gateway that canboat supports (Yacht Devices, iKonvert, SocketCAN) works by changing the configured command line.

## Alternatives considered

- **Write our own fast-packet and PGN decoder.** Rejected. It means weeks of work to rebuild something canboat has already validated against thousands of devices, and a bit-layout mistake would produce plausible wrong numbers.
- **The Python `nmea2000` package or python-can.** Rejected. Its coverage of the 1303xx environmental PGNs is incomplete, and it would add a dependency (prompt rule: no installs).
- **Use the NGX-1's NMEA 0183 conversion.** Rejected. 0183 sentences (MWV, MDA, XDR) drop the source and instance codes and the attitude data, and the conversion settings live in the gateway, invisible to RWI.

## Status

Accepted.
