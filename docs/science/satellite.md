# Satellite ingest: Himawari-9 tiles, geolocation and scenes (`rwi.satellite.source / tiles / geo / scene`)

Owner: satellite-ingest. Code: `src/rwi/satellite/{source,tiles,geo,scene}.py`. Tests:
`tests/unit/test_satellite_source*.py`, `tests/unit/test_satellite_geo*.py`. Fixtures:
`tests/fixtures/satellite/` (real-pixel crops, see section 12).

Everything marked **MEASURED** was read from the live bucket or from real files on **2026-11-02**
(UTC). Anything not verified is marked **NOT VERIFIED**. Times are UTC.

Why this exists: a 9-25 km NWP grid refreshed every 6 h cannot see a cloud band arriving in twenty
minutes. Geostationary imagery at 0.5 km every 10 minutes can. This module only gets the pixels,
puts them in the right place and says honestly what they are; turning them into irradiance is
`docs/science/satellite-irradiance.md`, and cloud motion is `docs/science/nowcasting.md`.

---

## 1. The product, and why the tiled one

| Item | Value |
|---|---|
| Bucket | `https://noaa-himawari9.s3.amazonaws.com/` (anonymous HTTPS, S3 REST listing; no boto3, no credentials) |
| Product | `AHI-L2-FLDK-ISatSS` = NOAA STAR "Sectorized Full Disk" imagery (`production_location: STAR`), from JMA Himawari-9 AHI |
| Layout | 16 bands x **88 tiles** per full-disk scan; each tile is a NetCDF-4 file of 2-5 MB (band C03) |
| Cadence | 10 minutes (`periodicity = 10`) |

Alternatives, as supplied by the integrator's probe (not re-measured here): the whole disk is ~616 MB per
slot over all bands and the L2 full-disk cloud mask alone is 352 MB per slot. A race support car in
the outback cannot pull hundreds of megabytes every ten minutes. The tiled product lets us fetch only
the tiles under the route (section 7). My own measurement of the cost of the *whole* disk in C03
alone (sum of the 88 listed sizes): 213-399 MB per slot over 2026-11-02.

The tile files carry reflectance (or brightness temperature), not a cloud mask. Cloud detection is
done downstream from the visible reflectance (Heliosat-style cloud index); there is no native
cloud product in what we fetch.

## 2. Bucket layout and naming (MEASURED)

```
AHI-L2-FLDK-ISatSS/YYYY/MM/DD/HHMM/OR_HFD-005-B11-M1C03-T059_GH9_s20263060300000_c20263060310460.nc
                                   |   |   |   |  |   |    |    |                |
                                   |   |   |   |  |   |    |    |                +- c: product CREATED
                                   |   |   |   |  |   |    |    +- s: scan START
                                   |   |   |   |  |   |    +- GH9 = Himawari-9
                                   |   |   |   |  |   +- tile T001..T088
                                   |   |   |   |  +- band
                                   |   |   |   +- mode digit
                                   |   |   +- file group B11 / B12 / B14
                                   |   +- nominal resolution x0.1 km (005 = 0.5 km)
                                   +- HFD = full disk
```

* **`s` and `c` stamps are `YYYY` + day-of-year + `hhmmss` + tenths**, not month/day. `s20263060300000`
  is 2026, day 306 = 2 November, 03:00:00.0. (`tiles.parse_stamp`, tested including a leap year.)
* **Resolution tiers.** C03 = 0.5 km (22000 x 22000 grid, 2200 px tiles); C01, C02, C04 = 1 km
  (11000, 1100 px); all others = 2 km (5500, 550 px). Group codes: B11 = C01-C06, C08, C09, C16;
  B12 = C10-C15; B14 = C07. The group code belongs to the provider, so listings are narrowed with it
  as a *hint* and fall back to a wide listing when it matches nothing.
* **One slot folder holds 1,408 objects** (16 x 88) and S3 returns at most 1,000 per page
  (`IsTruncated=true` + `NextContinuationToken`). Page 1 ends part-way through band C12. A reader
  that ignores the flag silently loses C12-C15 and C07. `source._list_objects` follows every token, raises
  if S3 says "truncated" without a token, and stops at 50 pages. A band-narrowed listing is one
  88-object page (~33 kB) instead of ~760 kB.
* **The product folder is not only the full disk.** It also holds `HR3` files: a *single-tile*
  rapid-scan target area every 2.5 minutes, in their own minute folders (xx:02, :04, :07, :09 ...).
  A day therefore lists ~550 minute folders, of which only the 144 multiples of 10 are full-disk
  scans. HR3 was measured centred at 22.5 N 140.8 E (Ogasawara, Japan): **it does not cover
  Australia** and is not used. `list_slots` keeps only minute % 10 == 0; `list_tiles` additionally
  keeps only `HFD` files of the requested band.
* **Scheduled gaps.** On 2026-11-02 there was no full-disk slot at 02:40, 05:40 or 14:40 (the first and
  last match the known Himawari housekeeping windows; 05:40 I did not expect and did not
  explain). `latest_slot` therefore never assumes continuity: it lists the hour folders, takes the
  newest candidates and returns the first whose band is *complete* (>= 88 tiles), because the newest
  folder is usually half uploaded.
* Duplicate tile ids in one slot (a reprocessed file) resolve to the newest `c` stamp, with a warning.

## 3. Latency and cadence (MEASURED)

* Cadence 10 min, 112 full-disk slots between 00:00 and 19:00 on the day measured.
* `c` (created) minus `s` (scan start): **534-549 s** for the first tiles of a slot, up to **646 s** for
  the last (slot 03:00: T044 created 03:09:09, T059 created 03:10:46). Tiles of one slot are created
  over about two minutes.
* S3 `LastModified` is typically 2-6 minutes *after* `c` (slot 18:10: `c` 18:18, objects 18:20:50-18:24:16,
  i.e. 10.8-14.3 min after the nominal scan start). `SatelliteTile.published_at_utc` is the `c` stamp,
  as specified, so it is a lower bound on the age of what you hold; the integrator's "~14 min" figure
  is the bucket-availability latency.
* At 19:15:22 the newest complete slot found by `latest_slot` was 19:00: the picture shown at that
  moment was **15 minutes old**. Every `SatelliteScene` states `image_age_at_load_s` in its notes and
  its `provenance.valid_time_utc` is the scan slot, never "now".
* **NOT VERIFIED:** the time each pixel was acquired. The slot time is the *nominal scan start*; AHI
  scans the disk north to south over ~10 minutes, so Darwin-Adelaide rows are probably acquired some
  five minutes later. The files carry no per-pixel time.

## 4. Projection and geolocation (`geo.py`)

The CRS is built from the file's own `fixedgrid_projection` attributes with pyproj
(`+proj=geos +h=35785831 +lon_0=140.7 +a=6378137 +b=6356752.31414 +sweep=y`), never from constants,
so Himawari-10 or a re-processed archive decodes without a code change. Latitudes are geodetic on
the file's ellipsoid (GRS80, within 0.1 mm of WGS84).

**Packed coordinates.** `x` and `y` are `int16` with `scale_factor`/`add_offset`; the packed integer
is the **full-disk pixel index** (`x_packed == tile_column_offset + column`). Unpacked they are scan
angles in **microradian**: `x_urad = 13.97199774457005 * col - 153698.96118914284` for C03; y is mirrored
(`y = -13.972 * row + 153698.961`, row 0 = north). For band resolution `f` times coarser than 0.5 km:
`scale = f * 13.972`, `offset = -scale * (N/2 + 0.5)` with `N = 22000/f` (MEASURED on C01, C02, C13;
used only to choose tiles before downloading, and cross-checked against each file on decode).
The sub-satellite point is therefore at pixel (11000.5, 11000.5), one pixel off the arithmetic centre
(10999.5); this is how the product is built, not a bug here.

**Pitfall 1: microradian.** PROJ's `geos` wants metres on the perspective plane:
`x_m = x_urad * 1e-6 * h`. Passing the raw number is a factor 10^6 too small, so every pixel lands
within millimetres of the sub-satellite point and the error looks like "a slightly shifted image".
Test: 1000 urad x 35,785,831 m = 35,785.831 m (hand value), and the closed-form comparison below.

**Pitfall 2: sweep axis.** Himawari: `sweep_angle_axis = y`; GOES-R: `x`. Unit vector from the
satellite to the ground: `(cos x cos y, sin x cos y, sin y)` for y-sweep (CGMS LRIT/HRIT) and
`(cos x cos y, sin x, cos x sin y)` for x-sweep (GOES-R PUG). Using the wrong one does not crash: it
moves off-axis pixels by tens of kilometres (e.g. 0.07 deg at 20 N, 111 E in the test set; 0.05 deg
of latitude on the T044 tile). It also does not mirror the image, it *warps* it; mirroring is
tested separately (east = increasing column, north = decreasing row).

**Off the disk.** PROJ returns `inf`; this module returns NaN (arrays) or `None` (scalar helpers
`point_to_pixel`, `pixel_to_point`). The limb on the equator is at ~151,860 urad.

**References and agreement (MEASURED, `@pytest.mark.science`):**

| Check | Reference | Result |
|---|---|---|
| scan angle -> lat/lon | independent closed-form ray/ellipsoid intersection written in the test (CGMS for y, PUG for x) | < 1e-6 deg at six off-axis angles |
| tile centre T059, T069, T078 | producer's `tile_center_latitude/longitude` attributes, centre = pixel index 1100 of 2200 | 1.4e-5 deg, 2.6e-5 deg, 4.4e-5 deg (about 1.5-5 m); test tolerance 1e-4 deg |
| T044 tile centre latitude | same attribute | y-sweep 5e-6 deg; x-sweep would be 0.05 deg off |
| sub-satellite point | (0 N, 140.7 E) | pixel (11000.5, 11000.5) to 1e-3 px |

Note: `tile_center_longitude` is **wrong in the file for T044** (19.06 where the true value is -160.94: a
180-degree wrap in the producer's metadata); it is -999 for tiles that run off the disk. Only the latitude
is used there.

Also checked by eye, not as a test: along 34.70-34.78 S the reflectance changes from sea (4-5 %) to
land/tidal flat (7-10 %) at 138.45-138.47 E, consistent with the Gulf St Vincent east coast within
2-3 km; the tidal flats blur the edge, so this is a plausibility check, **not** a geolocation validation.

**Tile layout.** Tiles are numbered row-major over the cells of the 10 x 10 grid that contain any
pixel looking at the Earth (88 of 100). MEASURED offsets (row, column) match the layout for T001
(0, 4400), T044 (8800, 19800), T059 (13200, 8800), T069 (15400, 8800), T078 (17600, 8800), T088
(19800, 15400), and for C01/C13 T059. `decode` refuses a file whose attributes contradict the layout
implied by its own grid; `load_scene` refuses a download whose tile set differs from the one the
geometry prior chose. The tile-to-bounds map is computed from the projection (no download) and cached as
`<cache>/satellite/tile_maps/tile_map-<signature>.json`.

**`tiles_covering`.** The lat/lon box is sampled every 0.1 degree, each sample is projected into the
grid, and the owning tiles are returned. Then one narrow listing supplies the keys. A box off the disk
returns `[]` without any network call; a half-published slot raises `TileMissingError`.

### The tiles that cover the race route (MEASURED)

Every one of the 3,524 vertices of `data/routes/bwsc-2027-stuart-highway.geojson` lies in one of:

| Tile | Row/col offset | Approx. extent (lat, lon) | Covers |
|---|---|---|---|
| **T059** | 13200, 8800 | 20.9 S - 9.8 S, 129.7 E - 140.9 E | Darwin to north of Tennant Creek (pixel rows 13706-15399) |
| **T069** | 15400, 8800 | 32.8 S - 20.3 S | Tennant Creek, Alice Springs, Coober Pedy |
| **T078** | 17600, 8800 | 48.0 S - 32.2 S | Port Augusta to Adelaide (route reaches pixel row 18016) |

The route's bounding box selects exactly these three of 88. Caution: Darwin (column 8883) is only 83
pixels (about 45 km) east of the T058/T059 boundary at column 8800. A nowcast box with a margin of 50 km around the car near Darwin needs **T058** too;
`tile_ids_covering` computes that exactly.

## 5. What the pixel values are

### Sun normalisation: the visible band is NOT sun-normalised (declared, tested)

`Sectorized_CMI` of the visible bands has `standard_name = toa_bidirectional_reflectance`,
`units = 1`, no `long_name`, `scale_factor = 0.00048828125` (1/2048), `add_offset = 0`. This is a
**reflectance factor** (pi L / E_sun style), **not divided by cos(solar zenith)**. Evidence, from real data
(tile T069, a 100 x 100 px window of clear Australian interior, one day):

| Slot | Solar zenith | cos | median |
|---|---|---|---|
| 03:00Z | 12.2 deg | 0.977 | 0.175 |
| 06:00Z | 47.2 deg | 0.679 | 0.098 |
| 08:00Z | 74.0 deg | 0.276 | 0.033 |

A sun-normalised albedo would stay roughly constant; this falls close to cos(zenith) (observed
08Z/03Z = 0.19, cos ratio 0.28; normalised would be ~1). Consequences:

* `SatelliteScene.sun_normalised` is `False` (`REFLECTANCE_SUN_NORMALISED`), and
  `provenance.processing` begins with `REFLECTANCE_STATEMENT` ("... NOT sun-normalised ...").
* A consumer that needs an albedo-like quantity applies
  `rwi.satellite.cloudindex.normalise_reflectance_by_sun(values, solar_zenith_field(...))` explicitly.
* Even after dividing by cos, the 03/06/08Z medians remain 0.179, 0.144, 0.120: path length and surface
  BRDF are not removed. Normalised does not mean sun-independent.
* For brightness-temperature scenes (`quantity = "brightness_temperature"`) `sun_normalised` is `None`.
* Pinned by `test_the_scene_declares_that_reflectance_is_not_sun_normalised` and
  `test_visible_reflectance_follows_the_sun_so_it_is_not_normalised`.

### Missing data: no fill value, space pixels are not flagged (MEASURED)

The product declares **no** `_FillValue`/`missing_value`, and the netCDF default fill (-32767) never
occurs. Pixels that look at space hold small noise integers (-6 ... ~100, i.e. "reflectance" up to
0.05), identical to dark ocean. They are masked **by geometry** (a pixel whose line of sight misses
the Earth is NaN). Declared fill/missing/valid-range values would be honoured as well; the netCDF
`int16` default is always masked. On-disk negative values (noise) are kept.

### Resampling to a regular grid

`GeoGrid` holds one latitude per row and one longitude per column, which a fixed grid does not have.
Scenes are therefore resampled by bilinear interpolation, only where all four native neighbours
are valid (a missing neighbour makes NaN, never a nearest-value fill), onto a regular lat/lon grid
whose step equals the native ground footprint at the box centre (0.53 km at Katherine-Tennant Creek,
not 0.5; larger further from the sub-satellite point). Rows run north to south. Longitudes are
unwrapped around 140.7 E (can exceed 180 east of the antimeridian). Pixels are not independent after
resampling; do not treat them as 0.5 km observations. `resolution_km` is the N-S step.

### Parallax (NOT corrected)

A cloud at 10 km altitude seen at a 25-45 degree viewing zenith is displaced 5-10 km from the point on
the ground beneath it. The scene is registered to the *surface*; the cloud's shadow is displaced
relative to the cloud pixel. This matters at a 0.5 km pixel size and is a known error source for
the irradiance retrieval.

## 6. Scene provenance, licence and attribution

`SatelliteScene.provenance`: provider `NOAA STAR / JMA (via NOAA NODD)`, dataset
`AHI-L2-FLDK-ISatSS/C03`, kind **DERIVED** (resampled; never OBSERVATION), `valid_time_utc` = scan slot,
`retrieved_at_utc` = the **earliest** original retrieval time of the tiles used (a cache hit keeps the
original time; section 8), and notes with the object keys, tile ids, native/grid resolution,
`product_created_latency_s`, `image_age_at_load_s`, the sun-normalisation statement, "NOT a
ground measurement" and the attribution.

**Licence and required attribution** (read on 2026-11-02 from <https://registry.opendata.aws/noaa-himawari>):

* No named licence. "NOAA has rights to distribute this data freely and openly to the public"; JMA
  produces and manages the data.
* "NOAA and JMA request attribution for the use or dissemination of unaltered data."
* It is not permitted to state or imply endorsement by, or affiliation with, NOAA or JMA.
* If the data are modified they may not be presented as original, unaltered data. **Our scenes are
  resampled, so they are modified data** and are labelled as such.
* Suggested citation: "JMA Himawari-8/9 was accessed on DATE from https://registry.opendata.aws/noaa-himawari."
* Contact: nodd@noaa.gov. JMA documentation: <https://www.data.jma.go.jp/mscweb/en/himawari89/>.

`source.ATTRIBUTION` (single source of truth): "Contains JMA Himawari-9 data (Japan Meteorological
Agency) distributed by NOAA; accessed from https://registry.opendata.aws/noaa-himawari. Resampled and
processed by JST-RWI: this is modified data, not the original product, and carries no NOAA or JMA
endorsement." Display it wherever imagery or a derived product is shown. I did not find a
separate restriction on commercial use on that page; I have not read JMA's own terms in full
(**NOT VERIFIED**).

## 7. Bandwidth budget for the race route (MEASURED)

Sizes of the three route tiles (band C03) from the bucket listing, 2026-11-02, hourly slots:

| Slot (Z) | T059 | T069 | T078 | Total per slot |
|---|---|---|---|---|
| 00:00 | 4.54 | 4.78 | 5.16 | 14.5 MB |
| 02:00 | 4.83 | 4.86 | 5.09 | 14.8 MB |
| 03:00 | 5.10 | 5.11 | 5.08 | 15.3 MB |
| 04:00 | 5.20 | 5.25 | 5.08 | **15.5 MB** (largest seen) |
| 06:00 | 4.73 | 4.87 | 4.77 | 14.4 MB |
| 08:00 | 3.76 | 3.83 | 4.20 | 11.8 MB |
| 09:00-19:00 (night) | 2.6-2.9 | 2.6-2.9 | 2.8-2.9 | 8.6-8.8 MB (6.5 MB at 14:00) |

* **About 15 MB per 10-minute slot while the sun is up** (11.8-15.5 MB; at night an almost-black image
  still costs 8.7 MB because noise compresses badly). That is **0.2 Mbit/s on average**, delivered as a
  ~15 MB burst every ten minutes (3-4 s at the 4 MB/s I measured from the development machine).
* **Race day** (event-time window 08:00-17:00 ACST = 22:30-07:30Z, 54 slots; clause 3.27.9 as encoded in
  `rwi.regulations`): ~15 MB x 54 = **~0.8 GB per day** for the three tiles of C03 only.
* **Only the tile the car is in** (plus the one ahead): 5 MB per slot, 0.07 Mbit/s, ~0.26 GB/day.
* Whole disk, C03 alone: 213-399 MB per slot (the thing this design avoids).
* **Verdict:** feasible from a support car on an ordinary mobile link, *if* the plan has ~1 GB/day per vehicle and
  fetches only the tiles the car is near; one cold fetch of two tiles with decode took 2.7 s, from cache
  0.6 s. It is not free on a metered plan, and the cap should be checked with the connection provider.
* **NOT VERIFIED:** sizes in August (race season; a clearer sky may compress better), the throughput of
  the real support-car link, and other bands (C01/C02 are ~1.3 MB per tile, C13 ~0.3 MB per tile: measured
  once for T059). Fetching only an HDF5 sub-window with HTTP range requests could cut this
  further; not implemented.

## 8. Download cache

`fetch_tile(tile, cache_dir=...)`: path `<cache>/satellite/<object key>` plus a sidecar
`<file>.meta.json` (key, `retrieved_at_utc`, `size_bytes`, `source_url`).

* A hit makes no request and **never rewrites the record**: `retrieved_at_utc` stays the original, so
  "how old is this data" stays honest after a restart.
* Atomic: the body streams to a temp file in the same directory, is fsync'd, length-checked against
  both `Content-Length` and the listed size, the sidecar is written atomically, and only then is the file
  renamed. A crash can leave an orphan sidecar or a `.tmp-*.part` file, never a data file without a
  record and never a half-written file under the final name. A file whose size differs from its record
  is not trusted.
* Offline (`settings.allow_network = False`): serves the cache or raises
  `ProviderUnavailableError`. `load_scene` offline reconstructs the tile list from the cache
  (`list_cached_tiles`) and refuses a partial mosaic.
* 404/403 -> `TileNotFoundError` (not retried); 5xx/transport errors are retried with the configured
  back-off; keys containing `..` are rejected.
* Cache eviction is not implemented (a day of three tiles is ~0.8 GB).

## 9. Dependencies

Tiles are read with `h5py` (NetCDF-4 is HDF5), a hard dependency of `pvlib`, so it is always
installed. `netCDF4` is **not** declared in `pyproject.toml` and is not used; the first version of this
module used it and broke when the environment was rebuilt. No new dependency is introduced.

## 10. What is NOT available

* **No ground truth.** There is no irradiance measurement anywhere in this platform, so nothing derived
  from these pixels can be validated. Geolocation is cross-checked against the producer's own metadata
  and one coastline; reflectance is not cross-checked against any surface measurement.
* No cloud mask or cloud-top height in the tiles; no per-pixel time, viewing geometry or solar geometry in
  the file (solar zenith is computed here).
* No parallax correction (section 5); no calibration check against another sensor.
* Infrared bands exist (C07, C11-C16, 2 km) and decode, but no infrared consumer exists yet. At night
  the visible bands are unusable (`is_usable_visible` is false when the minimum solar zenith is >= 80 deg).
* The 2.5-minute HR3 sector does not cover Australia.
* Archive depth was not probed; this module fetches the slot it is asked for.

## 11. Pitfalls found while building this (summary)

1. Day-of-year time stamps. 2. 1,000-key truncation of the slot listing. 3. Rapid-scan folders in the same
   product folder. 4. No fill value; space is "dark ocean". 5. `x`/`y` are packed indices in microradian.
   6. Sweep axis y vs x. 7. Visible CMI is not sun-normalised. 8. `tile_center_longitude` wrong for T044.
   9. Per-band offsets differ by half a pixel step. 10. The bucket re-writes tiles of the same slot over
   ~2 minutes, so a "slot" is not atomic. 11. `netCDF4` is not a declared dependency.

## 12. Tests and fixtures

`tests/fixtures/satellite/*.nc` are 50 x 200 to 200 x 200 pixel crops of real tiles (23-68 kB each, ~250 kB
total) with their real attributes; `build_fixtures.py` there derives them from the 5 MB originals, which are
not committed. The fixtures are in the providers-owned `tests/fixtures/` tree, under their own
subdirectory.

| File | What it tests |
|---|---|
| `test_satellite_geo.py` | microradian hand value, closed-form comparison, sub-satellite pixel, producer tile centres, sweep axis, mirroring, off-disk NaN/None, round trips, CRS from attributes, layout offsets, `tiles_covering` ids (route = 3 of 88), tile map cache |
| `test_satellite_source.py` | key/stamp parsing, truncated and paginated listings, missing token, empty slot, rapid-scan exclusion, `latest_slot`, retries, offline, cache hit keeps the original retrieval time, torn downloads, `tiles_covering` with the listing |
| `test_satellite_source_scene.py` | scale/offset, fill, space masking on a real limb crop, regrid correctness, two-tile mosaic without a seam, subset, provenance, sun-normalisation declaration and its data evidence, night, solar zenith vs `rwi.solar` |
| `test_satellite_source_live.py` | live listing checks, `network` marker, skipped unless `RWI_RUN_NETWORK_TESTS=1` |

## 13. Validation status

| Component | Status |
|---|---|
| File-name and stamp parsing (`tiles.py`) | validated against real data (all 1,408 real keys of a slot parse; stamps agree with S3 times) |
| S3 listing, pagination, slot and latest-slot discovery (`source.py`) | validated against real data (live runs 2026-11-02, incl. the 1,000-key truncation) |
| Download and cache (`fetch_tile`) | tested (fake bucket; one live cold/warm end-to-end run; no long soak) |
| Projection and pixel/lat-lon conversion (`geo.py`) | tested (closed form and producer tile-centre metadata to 4e-5 deg; no independent ground control points) |
| Tile layout and `tiles_covering` | validated against real data (offsets of six tiles of C03 and two of other bands read from real files) |
| `decode_tile`, masking, resampling | tested |
| Sun-normalisation convention | tested (three sun angles at one location, one day) |
| Solar-zenith field and range | tested (agrees with `rwi.solar` to 0.05 deg) |
| Scene assembly and provenance (`load_scene`) | tested |
| Race readiness (August, support-car link, parallax, calibration) | not started |
