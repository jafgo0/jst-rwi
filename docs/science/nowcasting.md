# Satellite cloud-motion nowcasting (`rwi.satellite.motion`, `rwi.satellite.nowcast`)

Owner: satellite-nowcast. Code: `src/rwi/satellite/motion.py`, `src/rwi/satellite/nowcast.py`.
Tests: `tests/unit/test_satellite_motion.py`, `tests/unit/test_satellite_nowcast.py`.
Contract: `docs/CONTRACTS.md` section 3c.

**The skill of this nowcast is unknown.** There is no ground-truth irradiance measurement anywhere
in this platform, so nothing here has been validated against real data, and no function below is
marked "validated against real data". The tests use synthetic fields built from a seeded random
generator; they show that the arithmetic, the signs, the unit conversions and the refusals are
correct, and they say nothing about whether real clouds translate the way a synthetic field does.
Whether advection beats the persistence baseline (section 9) is an open question the verification
module has to answer with real data. Until it has, treat every number as a plausible-looking
guess with a documented method.

Why it exists anyway: a numerical weather model updated every six hours on a 9-25 km grid cannot
tell a race strategist that a cloud band will cross the car in twenty-five minutes. Satellite
imagery at 0.5 km every 10 minutes can, in principle. The limits below are severe and are encoded
in the code (refusals, a horizon cap, a `limitations` list on every result), not only written here.

## Validation status per function

Exactly one of: not started / researching / implementing / implemented, not validated / tested /
validated against real data / operationally ready. "Tested" below means synthetic unit tests only.

| Function | Status | Evidence |
|---|---|---|
| `estimate_motion` | tested | Recovers known synthetic shifts to about 0.03 px, correct sign and compass direction; no real-data check. |
| `estimate_motion_blocks` (extra) | implemented, not validated | One smoke test on a uniform translation. |
| `is_usable_motion` / confidence threshold | implemented, not validated | Threshold chosen from synthetic noise experiments (section 4). |
| `advect` | tested | Exact against `np.roll`, a linear ramp, and the NaN edge rules. |
| `nowcast_point` | implemented, not validated | Mechanics and arithmetic tested; skill unknown. |
| `persistence_nowcast` | implemented, not validated | Mechanics tested; it is a baseline, not a claim. |
| `nowcast_with_baseline` | implemented, not validated | Returns both on one time axis. |
| `parallax_displacement_km` | implemented, not validated | Geometry only (spherical Earth); true cloud height is unknown. |
| `default_clear_sky_index` | implemented, not validated | Heliosat-2 style piecewise map written from memory of the published form; not cross-checked against the paper or any data. |

Every `SatelliteNowcast` also carries `validation_status` starting "NOT VALIDATED".

## 1. What the source can and cannot promise

Verified facts (probed 2026-11-02, see `CONTRACTS.md` 3c): Himawari-9 L2 tiles, cadence 10 minutes,
latency about 14 minutes (slot 17:50Z created 18:04Z), 0.5 km pixel pitch at the sub-satellite
point, geostationary at 140.7 E. Three consequences drive the design:

* The newest image is already about 14 minutes old when it can be used (section 7).
* Two images ten minutes apart give one displacement per pixel pair of order a few pixels at cloud
  speeds of 5-20 m/s (3-12 px at 0.5 km). Sub-pixel accuracy therefore matters.
* The image is a view from 36,000 km at an oblique angle; it is not a map of what shades the ground
  (section 8).

## 2. Motion by phase correlation

Given earlier frame `a` and later frame `b` on the same grid, if the content translated by `d`
pixels, `b(x) = a(x - d)`, then their Fourier transforms satisfy `B(w) = A(w) exp(-i w.d)`. The
normalised cross-power spectrum is

    R(w) = B(w) conj(A(w)) / |B(w) conj(A(w))| = exp(-i w.d)

and its inverse transform is a delta function at `d`. `estimate_motion` finds that peak with
`numpy.fft` (OpenCV is not installed and is not needed). Peak position in array index
space is `(d_row, d_col)`: positive `d_col` means the content moved to larger column index.

Pixels to velocity, with `Dt` the interval between the frames and `P` the pixel pitch from
`grid.resolution_km`:

    u_east  = s_col * d_col * P / Dt        v_north = s_row * d_row * P / Dt
    speed   = hypot(u, v)                   direction_to = atan2(u, v) mod 360   (compass, 0 = north)

`s_col` / `s_row` are +1 if the grid's longitudes / latitudes increase with the column / row index
and -1 if they decrease. The sign comes from the grid coordinates, never from an assumed image
orientation: the same array shift means "north" on a north-up grid and "south" on a south-up one,
and a test asserts both. `direction_to_deg` is where the field moves TOWARDS (90 = moving east,
i.e. arriving from the west). A sign error here would tell the team a cloud is leaving when it is
arriving; the tests assert the compass direction for eastward, northward, south-westward and flipped
grids.

Exact conversion check (asserted in the tests): 8 px x 0.5 km = 4000 m over 800 s = 5.0 m/s;
3 px x 2 km = 6000 m over 900 s = 6.667 m/s.

Pixel pitch uses `resolution_km` for both axes. The grid coordinates are used only to confirm it is
not wildly wrong (a ratio outside 0.6-1.7 raises). On a regular lat/lon grid the east-west and
north-south pitches differ by a factor of order `cos(lat)`; using one number is a simplification
worth up to roughly 10 percent in speed at the latitudes of this race. It is not corrected.

### Windowing

The FFT treats the image as periodic. Without tapering, the mismatch between opposite edges produces
a spurious zero-shift peak and wraps cloud across the boundary. Both frames are multiplied by a 2-D
Hann window (outer product of 1-D Hann windows with non-zero end points). Cost: clouds near the
border get little weight.

### NaN handling

Satellite fields have gaps (cloud-mask holes, off-disk pixels, missing tiles). A single NaN in an FFT
input turns the whole transform to NaN, so NaN is never passed in. Instead:

1. A pixel is valid only if it is finite in *both* frames (the intersection prevents a gap in one
   frame from looking like structure).
2. The mean of the valid pixels is removed from each frame.
3. Invalid pixels are set to zero after mean removal and carry zero window weight.

Fewer than 25 percent valid pixels, or no overlap at all, raises `ValueError`.

### Spectral low-pass

Whitening gives every frequency equal weight, so noise-dominated high frequencies swamp the signal.
The whitened cross-power spectrum is multiplied by `exp(-(|f|/0.1)^2)` (`f` in cycles per pixel).
In a synthetic experiment with noise sigma 0.2 (field standard deviation about 0.3) this cut the median
shift error from 0.86 px to 0.2 px. Cost: structure smaller than about 10 px (5 km) hardly
contributes to the motion estimate.

### Sub-pixel refinement

The integer peak is refined by evaluating the cross-power correlation on a 0.02 px grid within +/-1
px of the integer peak with a matrix-multiply (local, upsampled) DFT, as in Guizar-Sicairos et al.
(2008). The resolution is 0.02 px; the accuracy is set by noise and the window. On the synthetic
field, noise-free:

| True shift (rows, cols) px | Recovered px | Speed m/s |
|---|---|---|
| (0, +6) east | (0.00, +5.98) | 4.9833 (exact: 5.0) |
| (-6, 0) north | (-5.98, 0.00) | 4.9833, direction 0.00 deg |
| (+5, -5) south-west | (+4.98, -5.00) | 5.88, direction 225.11 deg |
| (+3.4, -2.7) sub-pixel | (+3.38, -2.70) | |
| (+12, +12) | (+11.98, +11.98) | |

Typical error is 0.02-0.03 px (0.3 percent of a 6 px shift) on clean synthetic data, rising with noise
(next section). Real clouds also change shape, so real errors will be larger; unknown.

## 3. Refusals

`estimate_motion` raises `ValueError` (subclasses where useful) rather than guessing for: arrays not
2-D, shapes that differ from each other or from `grid.shape`, arrays smaller than 16 px a side,
non-monotonic or wrongly sized grid coordinates, a `resolution_km` that contradicts the coordinates,
naive datetimes, an interval that is not positive, shorter than 120 s (a 0.1 px error becomes a speed
error of order 0.4 m/s and Himawari does not deliver frames that fast), or longer than 3600 s (the
field will have changed shape); fewer than 25 percent mutually valid pixels; and
`InsufficientStructureError` for a uniform field (standard deviation below 1e-3). A uniform field -
clear everywhere or overcast everywhere - has no defined motion; returning 0 m/s for it would be a lie.
Identical frames *with* structure are a legitimate answer: speed 0 with confidence about 1. Speeds
above 80 m/s are returned with confidence 0 and `REJECTED` in `method`.

## 4. Confidence and its threshold

The confidence is built from the peak-to-sidelobe ratio (PSR) of the integer-pixel correlation
surface:

    psr        = (peak - mean(sidelobes)) / std(sidelobes)       sidelobes: > 5 px (wrapped) from the peak
    confidence = clip(1 - NOISE_PSR / psr, 0, 1),   NOISE_PSR = 8

A field that translated gives a sharp isolated peak (PSR in the hundreds, confidence above 0.95). A
field that changed shape, or noise, gives a surface whose highest point is only the largest of many
noise samples. `NOISE_PSR = 8` is empirical: over 40 trials of 256 x 256 white noise and independent
cloud-like fields the largest PSR seen was 7.5 and the mean 5.4 (the textbook `sqrt(2 ln N)` = 4.7
underestimates it because the low-passed surface is spatially correlated). It is a heuristic score,
not a probability.

**Threshold: `MIN_USABLE_CONFIDENCE = 0.5`** (PSR >= 16, twice the noise ceiling).
`is_usable_motion(motion)` applies it. Below it the vector must not be used; `nowcast_point` falls
back to persistence and says so.

Why 0.5, from synthetic experiments (12 trials per row, random true shift of up to 8 px, additive
Gaussian noise on both frames, field standard deviation about 0.3):

| Noise sigma | Median shift error (px) | Median confidence | Usable |
|---|---|---|---|
| 0 | 0.01 | 0.98 | 12/12 |
| 0.1 | 0.09 | 0.87 | 12/12 |
| 0.2 | 0.23 | 0.76 | 12/12 |
| 0.4 | 0.73 | 0.50 | 6/12 |
| 0.6 | 1.3 | 0.27 | 0/12 |
| 0.8 | 2.8 | 0.04 | 0/12 |

The threshold sits where the median error reaches about 0.7 px, about 0.6 m/s at 0.5 km and 10
minutes: tolerable next to cloud speeds of 5-20 m/s. Pure noise gave confidence 0 in every trial, and
two unrelated cloud fields gave 0. For a field that is a blend of the translated frame and an
unrelated one, the estimate stays usable up to about 60 percent unrelated content (confidence 0.59)
and is rejected at 80 percent. **These are synthetic conditions.** The threshold should be re-tuned
once real frames and a verification score exist; this is exactly the kind of number that real
imagery will move. A sharp peak also proves only that *some* consistent pattern translated; it cannot
tell whether the clouds (as opposed to, say, a stationary-with-moving-shadow artefact or a layer
moving differently from the one that matters) are what moved.

## 5. Advection model and its central assumption

`advect(values, grid, motion, *, seconds)` shifts the field by `(u, v) * seconds`: pixel
displacement `(s_row v seconds / P, s_col u seconds / P)`, new field `F'(x) = F(x - d)`.

* Whole-pixel shifts are exact copies. Fractional shifts use bilinear interpolation, which smooths
  sharp cloud edges slightly.
* **Edge handling:** where the upstream source lies outside the domain the answer is unknown, not
  zero. Those pixels are NaN. Zero would be "clear sky" and would be a fabricated answer.
* NaN gaps in the input stay NaN and, under bilinear interpolation, spread to the neighbours that
  would have used them.
* `seconds = 0` returns an unchanged copy. Negative seconds back-advect.

**Central assumption: clouds translate but do not grow, decay, or change speed.** The vector is one
number for the whole domain and applies to the whole depth of the atmosphere. This breaks down:

* during convective build-up over central Australia on a hot afternoon - cumulus appears and
  vanishes in place, which advection cannot represent at all, and this is precisely the situation
  that costs the car power most;
* when thin cirrus dissipates or thickens;
* when low and high cloud layers move at different speeds and in different directions;
* near fronts, where the field deforms rather than translates;
* around sunrise and sunset (section 8).

`nowcast_point` samples the field at the *upstream* location, `point - d(age + lead)`, with bilinear
interpolation, which is identical to reading the advected array at the point. A test asserts the two
agree. If the upstream location leaves the image, or lands on a gap, the series is cut at that sample
(`domain_truncated = True`); the first sample being unknown raises `NowcastRefusedError`.

Cloud index to GHI: `default_clear_sky_index` maps cloud index `n` to clear-sky index `k` (1.2 for
`n < -0.2`; `1 - n` up to 0.8; a quadratic to 1.1; 0.05 beyond), then `GHI = k * clear_sky_GHI`, with
clear-sky GHI from the caller (`rwi.solar.clearsky` or any callable or sequence). Pass
`to_clear_sky_index=` to use the retrieval module's own mapping; neither is calibrated against any
ground measurement.

## 6. Horizon cap

**`MAX_EFFECTIVE_LEAD_S = 7200`** (2 hours), applied to the *effective* lead, image age plus
requested lead (section 7). A longer request raises `NowcastRefusedError` giving the longest request
allowed; it is not silently truncated. With the nominal 14-minute image age the longest request is
106 minutes.

Why: advection skill decays with lead time because clouds change on timescales of tens of minutes,
and it is usually poor beyond about two hours. Two hours is the *ceiling*, not a claim of skill out
to two hours. Real skill is probably useful only for a fraction of that - and that fraction is
exactly what has not been measured. Do not read the cap as a recommendation.

## 7. Effective lead time

A frame with nominal slot time `T` becomes available about `L = 840 s` (14 min) later. Nominal slot
time is the scan start; the disk is scanned over minutes, so the pixels over the route may be
observed some minutes later than their label. That is not corrected.

A nowcast issued at `T + 840 s` for "30 minutes from now" must advect the cloud field by
`840 + 1800 = 2640 s`:

    effective lead = image age + requested lead = 14 min + 30 min = 44 min

It is a 30-minute forecast from a 14-minute-old observation, and its skill belongs to a 44-minute
forecast. The first valid time (requested lead 0) is already a 14-minute forecast, not a
measurement. For the default 600 s step and 1800 s horizon:

| Requested lead (s) | Image age (s) | Effective lead (s) |
|---|---|---|
| 0 | 840 | 840 |
| 600 | 840 | 1440 |
| 1200 | 840 | 2040 |
| 1800 | 840 | 2640 |

`SatelliteNowcast` (a subclass of the contract's `IrradianceNowcast`; `isinstance` holds) exposes
`image_time_utc`, `image_age_s`, `requested_lead_s`, `effective_lead_s` (aligned with
`valid_times_utc`) and `max_effective_lead_s`, because the contract type has no field for them and a
strategist acting on a nowcast must see them. `issued_at_utc` defaults to the slot time plus 840 s;
pass the real issue time and the age grows accordingly.

## 8. Parallax and the dusk problem

**Parallax.** A satellite at 140.7 E and 35,786 km height sees a cloud top at height `h` displaced
from the ground point it is above by `h tan(theta)`, where `theta` is the satellite zenith angle at
the ground point (`tan(theta) = sin(g) / (cos(g) - R/(R+H))`, with `g` the central angle from the
sub-satellite point). The displacement is directed away from the sub-satellite point. Spherical
Earth, flat-layer approximation, perhaps 10 percent accurate for a few kilometres of height - much
better than the unknown cloud height. Computed by `parallax_displacement_km`:

| Location | Cloud top 2 km | 10 km | 12 km | Direction (bearing of displacement) |
|---|---|---|---|---|
| Darwin | 0.7 km | 3.4 km | 4.0 km | 218 deg |
| Alice Springs | 1.1 km | 5.5 km | 6.6 km | 195 deg |
| Adelaide | 1.7 km | 8.6 km | 10.3 km | 183 deg |

So along the route a high cloud is kilometres from where it actually is, which is many pixels at
0.5 km. A second, separate displacement is the shadow: it falls `h tan(solar zenith)` away from the
Sun, which exceeds the satellite parallax whenever the solar zenith exceeds the satellite zenith
(roughly 20-40 degrees along the route), and is not modelled either. Neither is corrected in the retrieval or the nowcast, which
treat each cloud pixel as shading the ground pixel below it. Cloud height is unknown, so the true
offset is bracketed, not known; the result's `limitations` quote the 2 km and 10 km figures for the
point in question. For a car-sized target and a narrow cloud band, an error of kilometres is the
difference between shaded and not.

**Dusk.** A visible-band cloud index depends on reflected sunlight. It degrades towards sunset and is
meaningless after it. The race's last hour of driving is therefore when the retrieval is least
reliable. `SatelliteScene.is_usable_visible` is false once the minimum solar zenith reaches 80
degrees, or when the zenith is unknown; `nowcast_point` and `persistence_nowcast` refuse such scenes
(`NowcastRefusedError`) rather than return a number. Caveat: the check uses the *latest scene*. A
valid time that lies after sunset is not checked beyond the caller's clear-sky GHI, which is zero
at night; the clear-sky *index* returned for such a time is meaningless and is not flagged
per-sample. Moreover the motion estimate at low sun is made from the reflectance of increasingly
oblique illumination, which is a further unquantified error source.

## 9. Persistence: the baseline to beat

`persistence_nowcast` holds the latest frame's cloud index (and so the clear-sky index) at the
point fixed; GHI follows the clear-sky curve. This is "smart persistence" in the clear-sky-index
sense: cheap, hard to beat at short lead times, and the bar any advection nowcast must clear.
An advection nowcast that cannot beat it has earned nothing, and for the first minutes it often will
not - persistence is already right when nothing is changing. `nowcast_with_baseline` returns both on
the same time axis (`NowcastComparison`: `advection`, `persistence`, `ghi_difference_w_m2`) so the
verification module can score them against each other once ground truth exists.
`nowcast_point` also *falls back to persistence*, labelled in `method` and `limitations`, whenever
the motion is unusable (refused, below threshold, or only one frame supplied).

## 10. The `limitations` list

Every result lists at least: not validated; clouds neither grow nor dissipate (and where that fails);
the effective-lead arithmetic with this image's age; the horizon cap and why; single motion vector;
unvalidated cloud-index mapping; parallax (with the point's 2 km and 10 km figures); the visible-band
dusk problem; nominal-versus-true scan time; the motion actually used with its confidence, or why
none was; and, when applicable, that the series was cut short at the edge of the image.

## 11. Known gaps (not done)

* No per-sample dusk flag.
* No parallax correction, shadow-offset correction, or cloud-height estimate.
* One motion vector for the whole domain (`estimate_motion_blocks` returns local vectors but nothing
  uses them yet).
* Pixel pitch is one number for both axes.
* No ensemble or uncertainty band: the result is a single deterministic trace with no error bars,
  because there is no data to calibrate one from.
* Thresholds (`MIN_USABLE_CONFIDENCE`, `NOISE_PSR`, low-pass width, horizon cap) come from
  synthetic experiments and judgement, not from real imagery.
* No comparison with real irradiance. Until the verification module has scored this against
  observations, nothing here may be called validated.
