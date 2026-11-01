# Route, map matching and trajectory (`rwi.route`)

Status written 2026-11-01. Owner: route. Code: `src/rwi/route/`, data: `data/routes/`,
tests: `tests/unit/test_route_*.py`.

## 1. Where the data comes from

### 1.1 Control stops and official distances (2025 Route Notes, used as a stand-in for 2027)

* Source: *2025 BWSC Route Notes, V1.0 Print, Published 13 July 2025* (56-page PDF, PDF
  metadata created 2025-07-13). URL:
  `https://assets.worldsolarchallenge.org/app/uploads/2025/07/13131527/2025-BWSC-Route-Notes-V1-FINAL-PRINT-Published-13072025.pdf`
  Downloaded and text-extracted with pypdf on 2026-11-01. The "Darwin to Adelaide Distance Chart"
  (PDF pp. 3-4) is an image and was **not** extracted; the sector tables were.
* **The 2027 route notes are not published** (as of 2026-11-01 the 2027 regulations page lists
  only Event Regulations, Team Manager's Guide, Freight Manual, Terms and Conditions, Stability
  Considerations and two drawings). Everything below is the 2025 route and may change for 2027
  (the notes themselves say "do not use route notes from previous events").
* Distances are "Approx km from Start", **cumulative from State Square, Darwin**
  (S12.46306 E130.83781, p.7). The notes use them as the unique location reference.

| # | Control stop | km (2025 notes) | Opens (2025) | Official close (2025) |
|---|---|---|---|---|
| start | State Square, Darwin | 0 | | |
| 1 | Katherine | 322 | Sun 24 Aug 11:00 | Sun 24 Aug 17:00 |
| 2 | Dunmarra (Highway Inn) | 631 | Sun 24 Aug 15:00 | Mon 25 Aug 14:00 |
| 3 | Tennant Creek | 988 | Mon 25 Aug 10:00 | Tue 26 Aug 11:00 |
| 4 | Barrow Creek | 1212.3 | Mon 25 Aug 13:00 | Tue 26 Aug 15:00 |
| 5 | Alice Springs | 1496 | Tue 26 Aug 08:00 | Wed 27 Aug 10:30 |
| 6 | Erldunda | 1694 | Tue 26 Aug 11:00 | Wed 27 Aug 14:00 |
| 7 | Coober Pedy | 2181 | Tue 26 Aug 16:00 | Thu 28 Aug 13:00 |
| 8 | Glendambo (printed "Control Point 8") | 2434 | Wed 27 Aug 10:00 | Thu 28 Aug 17:00 |
| 9 | Port Augusta (Central Oval) | 2720.5 | Wed 27 Aug 14:00 | Fri 29 Aug 13:00 |
| finish | Victoria Park, Adelaide | 3026.92 (total) | | |

Other facts taken from the notes: NT/SA border at km 1789; "In August we will not have daylight
saving time differences. Both states will be the same time zone." (UTC+9:30 in August; this is
why the NT/SA timezone tests use January); Katherine is "the last major shopping centre for
1200 km"; competition vehicles must not use the road south of Port Augusta from the Saturday.
The route notes give open/close times, **not** a mandatory stop duration and **not** a daily
driving window; those come from the 2027 Event Regulations (section 1.3).

### 1.2 Road geometry: `data/routes/bwsc-2027-stuart-highway.geojson`

* Source: **OpenStreetMap** via the Overpass API (`overpass-api.de`), planet timestamp
  2026-11-01T22:24:36Z. Relations "Stuart Highway" 2668551 (NT) and 198398 (SA, A87), plus
  Augusta Highway / Port Wakefield Road / Main North Road ways and small trunk-way gap fills
  (queries and method recorded in `data/routes/build_stuart_highway_route.py`).
* **Licence: ODbL 1.0; attribution "(c) OpenStreetMap contributors" required**
  (<https://www.openstreetmap.org/copyright>). Derived databases must be shared alike; check
  before redistributing the file outside the team.
* **This is NOT the official race route.** It is the shortest path through OSM trunk/primary
  roads from State Square to the centre of Victoria Park (OSM way 81798589), Douglas-Peucker
  simplified at 8 m to 3 524 vertices. Known deviations: straight-line links of ~0.9 km (State
  Square to the highway) and ~0.2 km (to Victoria Park); the last ~10 km into Adelaide follow Main
  North Rd / Hackney Rd / Hutt St instead of the official Robe Terrace ring route; one-way and
  dual-carriageway direction ignored; no elevation (all `elevation_m` are `None`).
* Length: **3 021.17 km vs 3 026.92 km official (2025): -5.75 km, -0.19 %.** The control stops
  are placed by projecting a town/roadhouse reference coordinate (OSM place or roadhouse node,
  listed in each stop's `reference` note) onto the polyline; they are *not* the surveyed control-
  stop entrances. Polyline minus published km at the stops:

| Stop | polyline km | published km | diff km | note |
|---|---|---|---|---|
| Katherine | 315.8 | 322 | -6.2 | notes put the stop at the south edge of town; our reference is the town centre |
| Dunmarra | 633.1 | 631 | +2.1 | |
| Tennant Creek | 986.6 | 988 | -1.4 | |
| Barrow Creek | 1210.4 | 1212.3 | -1.9 | |
| Alice Springs | 1492.5 | 1496 | -3.5 | |
| Erldunda | 1691.5 | 1694 | -2.5 | |
| Coober Pedy | 2176.9 | 2181 | -4.1 | reference snapped 1.1 km to the highway |
| Glendambo | 2429.6 | 2434 | -4.4 | |
| Port Augusta | 2714.6 | 2720.5 | -5.9 | |

  So distance-based quantities (e.g. "km to Alice Springs") from this file are good to roughly
  0.1-0.3 % but **must not be quoted as official distances**; use the published table for that.
* No fallback "control-stops-only" polyline was needed, so `bwsc-2027-control-stops.geojson`
  does not exist.
* Elevation: none. `grade_at_distance` returns 0.0 ("no information"); `Trajectory.assumptions`
  records `grade_known=False`. Elevation along the route must come from a DEM (SRTM/GA 1" DEM) in a
  later task and is **not** invented here.

### 1.3 Race rules used by the model (2027 Event Regulations, verified)

* Source: *2027 BWSC Event Regulations V1.0*, published 7 May 2026
  (<https://worldsolarchallenge.org/2027-event/regulations>); the PDF text was read and each
  clause below confirmed by direct text search on **2026-11-01**. The values live in
  `src/rwi/regulations.py` (`SCHEDULE`, `EVENT_TIMEZONE`) and `rwi.route.bwsc` imports them.

| Rule | Value | Clause |
|---|---|---|
| Official start each day | 08:00 | 3.21.1, 3.21.2 |
| Control-stop timing paused / vehicles leave the stop | 17:00 (resumes 08:00 next day) | 3.27.9 |
| Control-stop duration | 30 min (1800 s) plus any penalty time | 3.27.8 |
| Driver-change stop | 5 min | 3.23.4 |
| Event time zone | ACST, UTC+9:30, fixed | 1.3.1 |
| No driving on public roads between sunset and sunrise | - | 3.24.8 |

* The 08:00-17:00 window is **event time (ACST, fixed)**, not the vehicle's local time and not
  subject to daylight saving. `build_trajectory` therefore reads the window on the event clock by
  default (`window_clock="event"`); `window_clock="position"` is available for generic windows
  and uses `timezone_for_point` at the vehicle's position. Sunrise/sunset are geographic.
* Clause 3.24.8 (night driving ban) is a **second, independent** constraint. `build_trajectory`
  does **not** enforce it (no sunrise/sunset input); it says so in `Trajectory.assumptions`
  (`night_driving_enforced=False`). Use `rwi.regulations.is_within_driving_window` with
  sunrise/sunset to check or clip a plan.
* Penalty time on control stops (3.27.8) and driver-change stops (3.23.4) are not modelled.

## 2. Components and methods

### 2.1 Loader (`loader.py`)
`load_route_geojson` / `load_route_gpx`: parse, validate, compute cumulative distance as the sum
of WGS84 geodesic segment lengths (`rwi.core.geo.geodesic_distance_m`, pyproj/Karney). Rejects
(`RouteError`) empty/single-point/duplicate-only geometry, non-finite or out-of-range
coordinates, elevation outside the `GeoPoint` bounds, non-line geometry, several line features in
one collection, unreadable/corrupt files and point-to-point jumps over `max_jump_m` (default
100 km, configurable). Consecutive duplicate vertices are dropped and reported. A `Provenance`
and `source_description` are built from the file's `rwi_provenance` member; absent that, source,
licence and accuracy are stated as unknown. `annotate_control_stops` maps `NamedStop`s onto the
polyline (reusing a vertex within 50 m, else inserting one on the segment) and records snap
distance and the published km in the waypoint notes.

### 2.2 Geometry and map matching (`geometry.py`)
`point_at_distance`: bisection on cumulative distance, `interpolate_point` on the segment
geodesic, heading = forward azimuth to the next vertex; raises outside `[0, total]`.
`project_onto_route`: vectorised bounding-box lower bounds for all segments, examine segments in
increasing bound order, stop when the next bound exceeds the best distance. The foot point on a
segment starts from the spherical cross/along-track formulas of `rwi.core.geo` and is refined
by Newton iteration on the ellipsoid until the geodesic to the point is perpendicular to the
segment (tolerance 0.1 mm). Cost: O(N) numpy + O(N log N) ordering, then ~O(1) exact evaluations;
about 0.1 ms for a 3.5 k-vertex route. Sign: positive = right of travel.
**Accuracy:** on the synthetic routes foot point and cross-track agree with construction to
< 1 cm; real accuracy is that of the vertices (OSM centreline, simplification up to 8 m, so
expect metres, not centimetres). **Limits:** global nearest segment (no continuity prior, so a
route passing close to itself would be ambiguous - not the case for Darwin-Adelaide); no
antimeridian handling; matching quality against *real GPS fixes* has not been measured.

### 2.3 Trajectory / ETA (`trajectory.py`)
Kinematic model, no feedback from energy or strategy. Distance is advanced in sub-steps of at
most 1 km; time per sub-step = length / speed at the sub-step midpoint (midpoint rule; exact for
constant speed and for piecewise-constant profiles with breakpoints on 1 km multiples).
Samples: start, every `sample_spacing_m` (default 25 km), each control stop ahead, the finish.
The ETA of a stop is its **arrival**; `mandatory_dwell_s` is added after it. A stop exactly at the
start distance is treated as already departed.

Daily window (optional `daily_window_local=(start, end)`, start inclusive, end exclusive,
plus `window_clock`): with `"event"` (default) the window is read in ACST UTC+9:30 (clause 1.3.1),
which is how the BWSC 08:00/17:00 rule is defined; with `"position"` it is read as local civil
time derived at every sub-step from the IANA zone at the **vehicle's own position**
(`rwi.core.timebase.timezone_for_point`, DST aware; never the machine zone or a fixed offset).
If the window is closed the vehicle waits at its position until the next `start`; if it closes
mid-step the vehicle drives until the close and resumes the next morning. Unit tests cover both
clocks: Darwin (UTC+9:30) vs Perth (UTC+8) at the same instant and the NT/SA border in January
(UTC+9:30 vs +10:30) prove the position logic; the same inputs on the event clock show the
window does not move with position or SA daylight saving.

**Assumptions recorded (and returned in `Trajectory.assumptions`):** speed model and value,
sample spacing, integration step, ETA semantics, dwell handling and total (with the clause),
window applied/text/clock/timezone, `daily_window_verified` (True only if the window equals the
regulation window **and** is read on the event clock), `daily_window_source` (clauses 3.21.1-2,
3.27.9, 1.3.1), `night_driving_clause` / `night_driving_enforced=False` / `night_driving_note`,
overnight stops and total, `grade_known`.
Not modelled: traffic, road works, speed limits, battery or energy limits, driver changes,
charging stops, weather impact on speed, stop-and-go in towns, time to get to a control stop
entrance, the rule that stages end at control stops.

**BWSC rule constants** (`rwi.route.bwsc`, imported from `rwi.regulations`, no longer assumptions):
`BWSC_DAILY_WINDOW_EVENT_TIME` (08:00-17:00, 3.21.1-2 / 3.27.9) and
`BWSC_CONTROL_STOP_DWELL_S` (1800 s, 3.27.8). `build_trajectory` has no hidden default window:
callers pass it explicitly.

## 3. What is NOT verified for 2027

* The 2027 route, control-stop locations, distances, open/close times (notes unpublished).
* Other 2027 control-stop rules not used by the model (penalty time, arrival windows).
* Night driving (3.24.8) is a verified rule but is not enforced by `build_trajectory`.
* That the OSM geometry follows the roads the event will use in the CBDs and at the control-stop
  entrances; that OSM is current (e.g. road works, bypasses).
* Control-stop coordinates (town reference points only).
* Elevation/grade of any kind.
* Real-GPS map-matching accuracy; ETA accuracy against actual race progress.
* The 2025 distance chart image (pp. 3-4) was not read; sector tables were.

## 4. Validation status

| Component | Status |
|---|---|
| GeoJSON loader (`load_route_geojson`, validation, provenance) | tested |
| GPX loader (`load_route_gpx`) | tested |
| Control-stop annotation (`annotate_control_stops`) | tested |
| `point_at_distance` | tested |
| `project_onto_route` (map matching) | tested |
| `elevation_profile` / `grade_at_distance` | tested (synthetic only; no real elevation exists) |
| `build_trajectory` constant/profile speed, dwell | tested |
| `build_trajectory` daily window and local-time logic | tested |
| BWSC daily window 08:00-17:00 ACST (clauses 3.21.1-2, 3.27.9, 1.3.1), as used by the trajectory model | validated against real data (2027 Event Regulations V1.0 PDF, read 2026-11-01; rule text, not race-day behaviour) |
| BWSC control-stop dwell 30 min (clause 3.27.8) | validated against real data (same source; penalty time not modelled) |
| Night-driving ban (3.24.8) in `build_trajectory` | not started (rule verified; not enforced here) |
| 2025 control-stop list and official distances (transcription of the route notes) | tested |
| OSM route geometry `bwsc-2027-stuart-highway.geojson` | implemented, not validated |
| Route elevation data | not started |
| 2027 official route / control stops / distances / opening hours | researching |
