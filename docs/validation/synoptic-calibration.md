# Front detector calibration, step B (#10, #14) — 2026-11-25

**Bottom line:** on raw MSLP the front detector was mostly picking up the
atmospheric tide, not fronts. Its trough test now runs on the 24 h pressure
difference `p(t) - p(t-24h)`, which removes that locking. Agreement between the
forecast and ERA5 at event level is low (CSI ≤ 0.22), and the sample is too
small to tune thresholds on. `SynopticAnalysis.calibrated` therefore stays
`False`.

Script: `scripts/validation/synoptic_calibration.py` (reads
`data/archive/rwi.sqlite3` read-only and fetches ERA5 through the existing
providers). Scoring helpers: `rwi.alerts.synoptic_calibration`, tested in
`tests/unit/test_alerts_synoptic_calibration.py`. The numbers below come from the
run after the last detector change (whole-window trough test,
`dp_rise_pa` = raw MSLP rise).

## Method

**Reference.** ERA5 (reanalysis) at the same points, run through the same
detector rules. User decision: ERA5 is the reference, and 500 hPa / steering
flow is out of scope.

**1. Phase locking (is the detector tracking the diurnal cycle?).** Detections
come from ERA5 at the 11 named route points, 2025-09-25 to 2025-11-05. Each
detection is converted to mean local solar time (UTC + lon/15) and given a
Rayleigh test of uniformity on the 24 h circle. Real fronts are not tied to
local time.
- First harmonic (R1, p1): diurnal locking.
- Second harmonic (R2, p2; period 12 h): bimodal locking to the semidiurnal
  tide. The first harmonic misses this case because its two modes 12 h apart
  cancel.

The p-values use the approximation in Zar, *Biostatistical Analysis* (5th ed.),
§27.4.

**2. Forecast vs ERA5.** Inputs: 219 archived `ecmwf_ifs025` runs at 7 sites,
scored against ERA5 up to 2026-11-19 23 UTC.
- History: archived runs start at lead ~12 h with no history before that. The
  24 h history the detrended test needs is stitched in from earlier runs (27 h).
- Matching: greedy one-to-one event matching within ±6 h, nearest pair first.
  POD, FAR and CSI are pooled per lead bucket (Wilks, *Statistical Methods in
  the Atmospheric Sciences*, 3rd ed., §8.2).
- Edge handling: a reference event counts in a bucket if it lies within that
  bucket widened by the tolerance. Misses outside the bucket itself are dropped,
  so edge events are not counted as misses.
- Variants: each filter is applied to both the forecast and the reference.

## Results

### 1. Phase locking, ERA5, 11 route points, 2025-09-25..11-05

| Variant | n | Per point (N→S) | Signature | R1 | p1 | R2 | Mode (12 h) | p2 |
|---|---|---|---|---|---|---|---|---|
| raw | 179 | 10 8 8 14 16 23 22 21 24 19 14 | 24 | 0.16 | 1.1e-2 | **0.67** | 3.2 h | **5.5e-40** |
| detrended | 109 | 3 6 6 5 14 15 13 12 11 16 8 | 13 | 0.12 | 2.1e-1 | 0.09 | 6.5 h | 3.9e-1 |
| detrended + wind_shift | 47 | 1 0 0 2 5 6 8 6 7 8 4 | 7 | 0.34 | 3.6e-3 | 0.27 | 4.8 h | 2.8e-2 |
| detrended + signature | 13 | 0 0 0 0 0 2 4 2 2 2 1 | 13 | 0.29 | 3.3e-1 | 0.16 | 10.7 h | 7.2e-1 |

- **Raw:** R2 = 0.67 means two thirds of the detections cluster around about
  03 and 15 local time. That is the semidiurnal tide. The northernmost points,
  where the tide is largest and fronts are rare, still produce 8–14 detections
  each.
- **Detrended:** no significant locking on either harmonic. A 12 h-periodic
  signal cancels exactly in a 24 h difference, and the 24 h part largely does.
- **Requiring a wind shift:** this brings back diurnal locking (p1 = 0.004).
  That fits sea-breeze and nocturnal wind turning, so it is not a cleaner front
  filter.

### 2. `ecmwf_ifs025` archived runs vs ERA5 (±6 h)

Distinct ERA5 events per site (N→S): raw 2 4 7 9 7 7 6 · detrended 0 4 4 7 4 3 2
· detrended + wind_shift 0 0 1 4 1 1 1 · detrended + signature 0 0 0 1 1 0 0.

| Variant | Lead h | Hits | Misses | False alarms | POD | FAR | CSI | Mean dt h | MAE dt h |
|---|---|---|---|---|---|---|---|---|---|
| raw | 0–24 | 14 | 15 | 13 | 0.48 | 0.48 | 0.33 | −0.21 | 0.64 |
| raw | 24–48 | 33 | 33 | 52 | 0.50 | 0.61 | 0.28 | −0.12 | 1.09 |
| raw | 48–72 | 10 | 27 | 25 | 0.27 | 0.71 | 0.16 | 0.40 | 1.40 |
| detrended | 0–24 | 4 | 22 | 16 | 0.15 | 0.80 | 0.10 | 1.75 | 1.75 |
| detrended | 24–48 | 14 | 39 | 25 | 0.26 | 0.64 | 0.18 | 1.50 | 2.64 |
| detrended | 48–72 | 3 | 15 | 9 | 0.17 | 0.75 | 0.11 | 0.33 | 1.67 |
| detrended + wind_shift | 0–24 | 1 | 5 | 6 | 0.17 | 0.86 | 0.08 | 1.00 | 1.00 |
| detrended + wind_shift | 24–48 | 6 | 9 | 12 | 0.40 | 0.67 | 0.22 | 0.17 | 1.83 |
| detrended + wind_shift | 48–72 | 0 | 5 | 4 | 0.00 | 1.00 | 0.00 | n/a | n/a |
| detrended + signature | 0–24 | 0 | 5 | 0 | 0.00 | n/a | 0.00 | n/a | n/a |
| detrended + signature | 24–48 | 0 | 7 | 4 | 0.00 | 1.00 | 0.00 | n/a | n/a |
| detrended + signature | 48–72 | 1 | 0 | 0 | 1.00 | 0.00 | 1.00 | 2.00 | 2.00 |

- **Raw scores look better, but that is an artefact.** The forecast and ERA5
  contain the same tide, so they agree on the same false "fronts". High
  agreement on a diurnal signal says nothing about fronts.
- **Detrended:** CSI is 0.10–0.18 and the forecast is about +1.5 h late on
  average. Agreement on individual events is poor, as expected for a pointwise
  surface detector run on the model's own fields.

## Decisions

1. **Keep `pressure_detrend_24h = True`** as the default. Evidence: p2 goes from
   5.5e-40 to 0.39. Test: `test_detrend_rejects_semidiurnal_tide_trough`.
2. **Do not require a wind shift.** The difference from plain detrended (CSI 0.22
   vs 0.18) rests on 15–28 events per bucket and cannot be told apart from
   noise, and the filter brings back diurnal locking.
3. **Do not require "signature" strength.** It gives 0–1 ERA5 events per site,
   which is too rare to use.
4. **Run the trough test on the whole window only.** A truncated window made its
   first hour a spurious minimum. Without 24 h of history the criterion is *not
   evaluated*, which is not the same as no trough. `dp_rise_pa` reports the raw
   MSLP rise, so the briefing cites a physical number.
5. **Leave the thresholds unchanged and keep `calibrated = False`.** There are
   about 3 weeks of archived November runs with 0–7 events per site. Tuning on
   that would be fitting noise.

## Limits

- **ERA5 is a reanalysis, not an observation.** These results show how well the
  forecast reproduces what the same rules find in the analysis. They do not show
  that the rules find real fronts. That needs analysed charts (BoM MSLP analyses)
  or station data, so the rows stay at *tested*, not *validated against real
  data*.
- **Pooled counts are not independent.** Consecutive runs overlap in valid time,
  so the same ERA5 event is scored by several runs. The effective sample is much
  smaller than the hit/miss totals suggest.
- **History is stitched.** The 24 h history comes from earlier runs, not the run
  under test. This matches what the router does with its 24 h look-back.
- **Coverage is short.** The phase test covers the Sept–Nov 2025 race season.
  The forecast scoring covers November 2026 only, with no SA-end winter fronts.
- **Surface only.** There is no 500 hPa trough or steering flow, by decision.
