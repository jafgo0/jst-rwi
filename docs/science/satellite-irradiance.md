# Satellite irradiance retrieval (Heliosat-style)

Modules: `rwi.satellite.cloudindex`, `rwi.satellite.retrieval`.

## 0. What this is, and what it is not

This platform has **no measurement of irradiance at all**. No configured
provider measures GHI, DNI or DHI. The variables that decide the energy
strategy are the only ones the platform cannot check itself against.

A satellite retrieval is the closest thing available, but it is a
**retrieval**: a model applied to radiances measured from orbit. Nothing on the
ground measured the number. Therefore:

* it is always `DataKind.DERIVED`, never `DataKind.OBSERVATION`;
* its quality flag is `UNVALIDATED` and `Measured.uncertainty` is left `None`
  rather than filled with an invented error bar;
* **its error is unknown.** There is no ground truth anywhere in the platform
  to compare it with, so no bias, MAE or RMSE for this method can be quoted,
  and none is. Any literature figure for Heliosat-2 describes European and
  African sites with ground pyranometers, not Himawari-9 over central
  Australia.

## 1. The chain

```
radiance -> reflectance -> cloud index n -> clear-sky index k* -> clip -> GHI
```

### 1.1 Radiance to reflectance (done by the ingest module, not here)

The Himawari-9 AHI band 3 (0.64 um) L2 product delivers a *reflectance factor*:

    rho_f = pi * L * d^2 / E_sun        [dimensionless]

with `L` the spectral radiance, `d` the Earth-Sun distance in AU and `E_sun`
the band-mean solar irradiance at 1 AU. A reflectance factor still contains the
cosine of the illumination angle. Heliosat compares a pixel with references
taken at other sun angles, so it needs the sun-normalised reflectance:

    rho = rho_f / cos(theta_s)          [dimensionless]

`cloudindex.normalise_reflectance_by_sun` does this (and returns NaN, not
infinity, where cos(theta_s) <= 0). Whether a given
`SatelliteScene(quantity="reflectance")` is already sun-normalised is for the
ingest module to declare; **this module assumes it is** and does not check it.
If the declaration is wrong, every retrieval is biased by a sun-angle-dependent
factor. This is an open interface question to settle with the ingest author.

### 1.2 Cloud index

    n = (rho - rho_clear) / (rho_max - rho_clear)        [dimensionless]

`rho_clear` is the pixel's cloud-free ground reflectance, `rho_max` the
brightest-cloud reflectance. `n = 0` is clear ground, `n = 1` the brightest
cloud. `n` is deliberately **not** clipped: `n < 0` (darker than the clear
reference) and `n > 1` (brighter than the cloud reference) both occur and are
handled by the relation below.

If `rho_max - rho_clear < 0.05` (`MIN_REFERENCE_CONTRAST`), or either
reference is NaN, `n` is NaN: the denominator would be noise.

### 1.3 Clear-sky index (the published relation)

`k* = GHI / GHI_clear` (also written `kc`), from Hammer et al. (2003) as used in
Heliosat-2 by Rigollier et al. (2004). Exact breakpoints implemented in
`cloudindex.clear_sky_index`:

| range of n          | k*                                  |
|---------------------|-------------------------------------|
| n <= -0.2           | 1.2                                 |
| -0.2 < n <= 0.8     | 1 - n                               |
| 0.8 < n <= 1.1      | 2.0667 - 3.6667 n + 1.6667 n^2      |
| n > 1.1             | 0.05                                |

Checks (also asserted in the tests): the pieces join (k*(-0.2) = 1.2;
k*(0.8) = 0.2 from the line and 0.20006 from the quadratic; k*(1.1) = 0.05004
from the quadratic against the 0.05 floor); the quadratic's slope
`-3.6667 + 3.3333 n` is <= 0 on the whole segment, so the curve is monotonically
non-increasing - a cloud-to-irradiance curve that rose with cloud would be
nonsense.

References:

* Hammer, A., Heinemann, D., Hoyer, C., Kuhlemann, R., Lorenz, E., Mueller, R.,
  Beyer, H.G. (2003). *Solar energy assessment using remote sensing
  technologies.* Remote Sensing of Environment 86(3), 423-432.
* Rigollier, C., Lefevre, M., Wald, L. (2004). *The method Heliosat-2 for
  deriving shortwave solar radiation from satellite images.* Solar Energy
  77(2), 159-169.
* Heliosat lineage: Cano et al. (1986) *A method for the determination of the
  global solar radiation from meteorological satellite data*, Solar Energy
  37(1), 31-39; Beyer, Costanzo, Heinemann (1996), Solar Energy 56(3), 207-212.

Caveats stated plainly:

* The breakpoints and coefficients were entered from the literature as the team
  knows it, not re-derived, and the primary papers were not re-read while
  writing the code. Someone should check the table above against the paper.
* The relation was developed for Meteosat imagery. Applying it to Himawari-9
  AHI band 3 over Australia is an **assumption** that nothing in this platform
  can test.

### 1.4 Clip

`cloudindex.clip_clear_sky_index(k*, lower=0.0, upper=1.3)`; policy:

* `k*` up to about 1.1 is **preserved**. It is a real effect, *cloud
  enhancement*: sunlight scattered off the sides of cumulus adds to the direct
  beam at cloud edges and can push GHI above the clear-sky value.
* Above about 1.3 a pixel is treated as an **artefact**: a wrong clear-ground
  reference, sun glint, a dark or shadowed surface. Instantaneous enhancement
  spikes seen by ground sensors are too short and too small to survive averaging
  over a 0.5-1 km pixel and a ten-minute scan.
* Below 0, irradiance is impossible: clipped to 0.
* The number of pixels clipped high and low is **recorded**
  (`ClippedIndex.n_clipped_high/low`) and written into the provenance notes of
  point retrievals. A field that is mostly clipped is a failed retrieval.
* Honest note: the published relation never exceeds 1.2, so with it the 1.3
  ceiling cannot fire; it guards against a changed relation or k* injected from
  elsewhere. The 1.2 plateau for `n <= -0.2` is itself a mixed bag - genuine
  enhancement, or a pixel darker than a wrongly bright clear-ground reference.
  The method cannot tell which.

### 1.5 GHI

    GHI = k* x GHI_clear                     [W/m2]

`GHI_clear` is `rwi.solar.clearsky.clear_sky_irradiance(...).ghi_w_m2`
(Ineichen-Perez with the pvlib Linke-turbidity climatology), evaluated for the
point **at the scan slot time** (the code raises if it is more than 1200 s
away). The clear-sky model is therefore a second uncertain input: over
Australia's dry atmosphere the climatological turbidity may be off, and that
error multiplies straight through.

Pixel sampling for a point is the nearest pixel centre (no interpolation); a
point more than one pixel outside the grid is "outside scene", not extrapolated.

## 2. The two reference fields

### 2.1 Clear-ground reflectance, `rho_clear`

Estimated by `estimate_clear_sky_reflectance(scenes)` as the per-pixel low
percentile (default 5th) of a `(time, rows, cols)` stack, ignoring NaN. Or
supplied by the caller. For it to mean something the stack should be **the same
time of day on many recent days**, so that sun and view geometry (and hence the
surface's directional reflectance) match the scene being retrieved.

If there are fewer than `min_scenes` (default 10) scenes, or no pixel has
`min_valid_samples` (default 5) finite values, the estimator **raises**
`ReferenceEstimationError`. Pixels individually short of samples are NaN.
Retrieval functions given no references return "unavailable" with a reason. **No
constant is ever substituted.**

**What a wrong clear-ground reference does.** Everything downstream is linear
in `rho_clear` through `n`:

* reference too bright (it caught cloud, e.g. the pixel was cloudy in every
  scene) -> `n` too low -> `k*` too high -> GHI **overestimated**, and a
  cloudy pixel may read as clear;
* reference too dark (it caught shadow or noise) -> `n` too high -> GHI
  **underestimated**, and clear ground may read as thin cloud.

The bias is systematic: it repeats at the same pixel every day, so it will not
average away, and it will look like a ground-brightness map.

### 2.2 Brightest-cloud reflectance, `rho_max`

`estimate_max_reflectance(scenes)` pools every finite pixel in every scene and
takes the 98th percentile (a single scalar; thick cloud tops are about equally
bright everywhere, and a per-pixel high percentile over a desert is dominated by
whichever few clouds happened to pass). `pooled=False` gives a per-pixel field.
Too low -> `n` too large -> GHI underestimated; too high -> overestimated. A
sequence with no thick cloud under-estimates it. Same refusal rules as 2.1.

## 3. Why red desert is the hardest surface

Heliosat works because cloud is brighter than ground. Over the Stuart Highway
corridor the ground is bright red-orange soil: in the 0.64 um band its clear
reflectance is high, so `rho_max - rho_clear` is small. Consequences:

1. **Thin cloud and bright ground look alike.** Cirrus and thin altocumulus add
   little reflectance on top of an already bright surface, so `n` is small and
   noisy and the retrieval can miss real attenuation entirely.
2. **High sensitivity to the reference.** With a small span, a 0.02 error in
   `rho_clear` shifts `n` by several hundredths to a tenth, i.e. `k*` by the
   same amount - 10 % in GHI - before any cloud is involved.
3. **Surface variability.** Red soil, spinifex, salt pans, burn scars and
   ephemeral wet ground differ in reflectance and change after rain, so a
   reference learned in dry weeks can be wrong after a storm.
4. **Directional reflectance.** The desert surface brightens toward the
   backscatter direction; a reference at a different time of day or season is
   biased. Hence same-time-of-day stacks.
5. **Persistent cloud is invisible to the percentile estimator** and
   cloud-shadow pixels bias it dark.

No part of this has been measured here. The statements above are method
physics, not results.

## 4. Night and low sun

A visible-band retrieval needs sunlight reflecting off the scene. At a high
solar zenith the signal is weak, the path through the atmosphere long and the
cloud-shadow geometry extreme.

Rule (the same as `SatelliteScene.is_usable_visible`, zenith < 80 degrees):

* scene refused if `scene.is_usable_visible` is False, which includes a scene
  with **no declared zenith range** - the code does not assume daylight;
* a point is additionally refused if its own apparent zenith >= 80 degrees;
* a field is refused wholesale if the scene's zenith range reaches 80 degrees
  and no per-pixel zenith field is supplied; with one, only the offending
  pixels become NaN;
* infrared (brightness-temperature) scenes are refused: this method is visible.

The result is **unavailable**, reported as `Measured(value=None, quality=MISSING)`
for a point and NaN for a field, with a stated reason (`RetrievalStatus`,
`PointRetrieval.reason`, and the provenance notes). It is never `0.0` and never
a number.

This is a different statement from the physical one. With the Sun below the
horizon, ground-level irradiance is zero by physics, and the clear-sky model
says so exactly. The satellite *retrieval* of it is simply not available, and
the two must not be conflated: a zero from the retrieval would read as a
measurement of darkness.

## 5. Validation: what exists and what would change it

**No ground-truth irradiance exists in this platform.** Open-Meteo-style
"radiation" fields are model output, not measurement; the BoM observation
product does not report solar irradiance. So the bias, MAE and RMSE of this
retrieval are not merely unreported - they are unknown.

`retrieval.compare_with_measurements(retrieved, measured)` returns bias
(retrieved minus measured, W/m2), MAE and RMSE over supplied pairs, discarding
pairs where either side is NaN/None and refusing an empty comparison. It works
on supplied pairs and has never been run on real data, because no measured GHI
exists to supply.

**The single highest-value instrument the team could add is a pyranometer** -
on the vehicle (mounted flat, away from shading, with a logged timestamp and
position) or on a support car. It would measure GHI along the actual route,
pair directly with the retrieval, turn this function from a promise into a
result, and let the team calibrate `rho_clear`, `rho_max` and the clip against
the real desert instead of a European fit. A second, cheaper step is an
irradiance sensor at a fixed base station. Until then the satellite retrieval
should be shown as an unvalidated indication of *relative* cloud attenuation,
not as a number to size the energy strategy on.

Other things that would also help: a Linke-turbidity check from sun-photometer or
AERONET data for the clear-sky reference; reading the primary papers to check
the table in 1.3.

## 6. Validation status per function

Exactly one of: not started / researching / implementing / implemented, not
validated / tested / validated against real data / operationally ready.

"Tested" here would mean unit tests of the arithmetic only; none of these has
been compared to a real irradiance measurement, so none is "validated".

| Function | Status |
|---|---|
| `cloudindex.cloud_index` | implemented, not validated |
| `cloudindex.clear_sky_index` | implemented, not validated |
| `cloudindex.clip_clear_sky_index` | implemented, not validated |
| `cloudindex.estimate_clear_sky_reflectance` | implemented, not validated |
| `cloudindex.estimate_max_reflectance` | implemented, not validated |
| `cloudindex.normalise_reflectance_by_sun` | implemented, not validated |
| `retrieval.retrieve_ghi` / `retrieve_ghi_detailed` | implemented, not validated |
| `retrieval.retrieve_field` / `retrieve_field_detailed` | implemented, not validated |
| `retrieval.compare_with_measurements` | implemented, not validated |

(The unit-test suite checks exact arithmetic, the breakpoints of the relation
against hand-computed values, monotonicity, night/low-sun refusal, refusal on
missing references, clipping, provenance and NaN propagation. That makes the
code correct as written; it says nothing about whether the method is accurate
over central Australia.)

## Verification status of the n -> k* relation (integrator, 2026-11-02)

The piecewise coefficients were written from memory of the literature, and an
attempt to confirm them against a primary source failed in that session. This
section records exactly how far the relation is and is not verified, because a
systematic error here biases every retrieved irradiance and would be invisible
in the unit tests, which only check internal consistency.

**Attempts made:** the Mines ParisTech *Description of the software Heliosat-2*
PDF (no text layer, scanned); an arXiv secondary source (no extractable text);
a literature search (partial).

**Corroborated independently:**

* the linear middle branch `k* = 1 - n`. A Heliosat-2 description states that a
  cloud index above 0.2 corresponds to a clear-sky index below 0.8, which is
  exactly `k*(0.2) = 0.8` under this branch;
* the existence of a quadratic branch above `n = 0.8`, introduced because a
  linear relation underestimates `k*` for overcast skies;
* that Heliosat-2 limits the cloud index itself to `[-0.5, 1.5]`, now applied
  as `N_CLAMP_LOW`/`N_CLAMP_HIGH`.

**Not corroborated, do not trust without checking:** the constant `k* = 1.2`
below `n = -0.2`; the quadratic coefficients `2.0667, -3.6667, 1.6667`; the
floor `k* = 0.05` above `n = 1.1`.

**To resolve:** read Rigollier, Lefevre and Wald (2004), *Solar Energy* 77(2),
159-169, or the Mines ParisTech software description, and confirm or correct
the four branches. This is a contained, well-defined task for anyone with
library access, and it should be done before any strategic decision rests on a
satellite-retrieved irradiance.

**Beyond the coefficients:** the relation was developed for Meteosat over
Europe and Africa. Central Australia is bright red desert under a different
viewing geometry — the hardest case for this method — and nobody has tuned it
for Himawari. That is a separate error source from the coefficients, and it is
also unquantified.
