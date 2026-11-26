# Vehicle dynamics and race strategy

Scientific documentation for `rwi.vehicle` (longitudinal power model, energy
store) and `rwi.strategy` (segments, scenario engine, constant-speed
optimiser, recommendation).

Owner: vehicle performance and race strategy.
Code: `src/rwi/vehicle/dynamics.py`, `src/rwi/vehicle/battery.py`,
`src/rwi/strategy/segments.py`, `src/rwi/strategy/scenarios.py`.
Tests: `tests/unit/test_vehicle_dynamics.py`,
`tests/unit/test_vehicle_battery.py`,
`tests/unit/test_strategy_scenarios.py`,
`tests/unit/test_strategy_optimiser.py`.

---

## 0. What this subsystem is, and what it must never be

**It issues no vehicle commands.** Nothing in `rwi.vehicle` or `rwi.strategy`
produces a speed command, a throttle or brake signal, an array or battery
control action, or any other instruction to the car. The outputs are numbers
and caveats for the strategy group: scenario comparisons and one advisory
recommendation, both of which **require human review** before they influence
how the car is driven. The driver and the observer remain responsible for the
road, the traffic and the event regulations at all times. No part of this code
is safe to connect to a control loop, and no part of it has been designed,
reviewed or tested for that purpose.

**Nothing here is a JU Solar Team vehicle specification.** The vehicle
parameters shipped with the code (`DEFAULT_VEHICLE`) are generic
Challenger-class placeholders marked `parameter_source="assumed"`. See §6.

---

## 1. Force and power balance

A quasi-static, longitudinal, point-mass model. Symbols and units:

| Symbol | Meaning | Unit |
|---|---|---|
| `v` | ground speed | m/s |
| `v_air` | along-track **apparent** airspeed (headwind positive) | m/s |
| `w_along` | component of the true wind blowing in the direction of travel | m/s |
| `rho` | air density | kg/m3 |
| `CdA` | drag area, `Cd * A` | m2 |
| `Crr` | rolling-resistance coefficient | – |
| `m` | total mass (vehicle + driver + ballast) | kg |
| `g` | standard gravity, 9.80665 (`rwi.core.units.GRAVITY_M_S2`) | m/s2 |
| `grade` | road gradient, rise/run | – |
| `theta` | road angle, `atan(grade)` | rad |
| `eta_dt` | drivetrain efficiency (motor + controller + bearings) | – |
| `P_aux` | auxiliary electrical load | W |
| `s` | yaw drag sensitivity, `yaw_drag_sensitivity_per_deg` | 1/deg |
| `psi` | apparent-wind yaw angle | deg |

### 1.1 Forces (N)

```
F_aero  = 0.5 * rho * CdA_eff * sign(v_air) * v_air^2
F_roll  = Crr * m * g * cos(theta)
F_grade = m * g * sin(theta)
```

with the effective drag area

```
CdA_eff = CdA * (1 + s * |psi|)        s = 0 by default  =>  CdA_eff = CdA
```

### 1.2 Mechanical power at the road (W)

Each force does work at the speed of the vehicle **over the ground**, because
that is the speed at which the tractive force displaces the car:

```
P_aero  = 0.5 * rho * CdA_eff * sign(v_air) * v_air^2 * v
P_roll  = Crr * m * g * cos(theta) * v
P_grade = m * g * sin(theta) * v                       (negative downhill)

P_tractive = P_aero + P_roll + P_grade
```

These three are the `aerodynamic_power_w`, `rolling_power_w` and
`gradient_power_w` fields of `ConsumptionEstimate`, and they are reported as
*mechanical* powers so each term can be checked on its own.

### 1.3 Electrical demand at the battery terminals (W)

```
P_elec_traction = P_tractive / eta_dt                  if P_tractive >= 0
P_total         = P_elec_traction + P_aux
```

`total_power_w` is this electrical figure. It is deliberately **not** the sum
of the four component fields: the drivetrain division and the negative-power
policy (§3) sit between them.

### 1.4 Energy (Wh)

```
E = integral P_total dt                                (trapezoidal / segment-wise)
```

Energy is always Wh, power always W, irradiance always W/m2; the only sanctioned
power-to-energy conversion for series data is
`rwi.core.units.integrate_power_wh`. The scenario engine integrates per segment
as `P_total * dt / 3600` with `dt = distance / v`, plus `P_aux * dwell / 3600`
for mandatory stops.

### 1.5 Reference values asserted in the tests

With `rho = 1.2 kg/m3`, `CdA = 0.1 m2`, `v = 25 m/s`, still air:

```
P_aero = 0.5 * 1.2 * 0.1 * 25^3 = 937.5 W          (exact)
```

With `Crr = 0.005`, `m = 300 kg`, `g = 9.80665 m/s2`, `v = 25 m/s`, flat road:

```
P_roll = 0.005 * 300 * 9.80665 * 25 = 367.749375 W (exact)
```

Both are asserted to within 1e-9 W so that any unit regression (km/h for m/s,
`g = 9.81`, a stray factor of 3.6) fails loudly rather than quietly shifting
every energy number by a few per cent.

Air density at ISA sea level (101325 Pa, 15 degC) is 1.2250 kg/m3; that
standard value is asserted as an external reference.

### 1.6 What the model does not contain

* no inertial term (`m * a * v`): acceleration and braking energy are ignored,
  which is defensible for long steady legs and wrong for stop-start driving;
* no transient or thermal derating of motor, controller or battery;
* no lateral dynamics, tyre slip, or cornering drag;
* no yaw-dependent drag curve (see §2.3);
* no traffic, road trains, overtaking, convoy stops or driver changes beyond an
  explicitly supplied dwell time;
* no gust or turbulence response: the wind is a steady mean per segment.

---

## 2. Apparent wind, and why ground speed alone is wrong

### 2.1 The physics

Aerodynamic **force** is set by the speed of the air flowing over the body.
Aerodynamic **power** delivered to the road is that force times the speed of
the body over the ground. These are two different speeds whenever the wind
blows, and conflating them is the single most common error in a solar-car
energy model:

```
P_aero = F_aero(v_air) * v           NOT  F_aero(v) * v
```

The apparent wind is the vector sum of the true wind and the reversed
ground-speed vector. That vector sum lives in exactly one place,
`rwi.core.wind.relative_wind`, and the vehicle model never re-derives it. Its
`headwind_mps` field is the along-track apparent airspeed,

```
v_air = v - w_along
```

positive for a headwind, following the meteorological convention that
`wind_from_deg` is the direction the wind blows *from*.

### 2.2 The three regimes, and the sign handling

| Regime | `w_along` | `v_air` | Effect on `P_aero` |
|---|---|---|---|
| Headwind | `< 0` | `> v` | **larger** than still air |
| Moderate tailwind | `0 < w_along < v` | `0 < v_air < v` | **smaller** than still air |
| Tailwind stronger than the ground speed | `> v` | `< 0` | **negative**: the air pushes the car |

To keep the third regime, the square is taken as a *signed* square,
`sign(v_air) * v_air^2`. A tailwind therefore reduces drag power continuously
and crosses into negative drag power exactly when `v_air` changes sign - no
discontinuity, no absolute value, no clamp. Worked example asserted in the
tests: `rho = 1.2`, `CdA = 0.1`, `v = 25 m/s`, 30 m/s tailwind gives
`v_air = -5 m/s` and `P_aero = -37.5 W`.

A negative `P_aero` does **not** by itself mean the battery is charged; what
happens to a negative *total* tractive power is the subject of §3.

The tests assert, at 25 m/s with a 5 m/s wind, that
`P_aero(head) > P_aero(still) > P_aero(tail)` strictly, and that the head and
tail values equal the still-air value scaled by `((v +/- 5)/v)^2` exactly.

### 2.3 Crosswind and yaw: what is deliberately not modelled

Only the along-track component of the apparent wind enters the drag term. The
crosswind component is *not* folded into an apparent-speed magnitude, because
the drag coefficient of a solar car at yaw is not `CdA` - a real car's drag can
rise steeply with yaw angle, and the shape of that rise is a property of the
specific body. The team has no measured drag-vs-yaw curve, so none is invented.

The hook for the measurement, when it exists, is
`VehicleConfig.yaw_drag_sensitivity_per_deg`, a linear approximation
`CdA_eff = CdA * (1 + s * |psi|)`. It **defaults to 0.0**, which means no yaw
effect at all. That is an understatement of real drag in any crosswind, and the
provenance notes of every `ConsumptionEstimate` say so
("drag at yaw is UNDERESTIMATED until a drag-vs-yaw curve is measured").
Crosswind is still available to other parts of the platform - `RelativeWind`
carries it, and it matters for stability and alerting - it simply does not
change the drag number here.

### 2.4 Air density

```
rho = p / (R_d * T)          R_d = 287.058 J/(kg K), T in K
```

via `rwi.core.units.air_density_kg_m3` (dry air; humidity ignored, which
slightly overestimates density and therefore drag). Precedence:

1. an explicit `air_density_kg_m3` argument;
2. pressure and temperature, through the ideal-gas law;
3. otherwise the **ISA sea-level default** of 1.2250 kg/m3, with the
   substitution recorded in `provenance.notes`.

The fallback matters: near Alice Springs (about 545 m, often 35 degC) real
density is around 1.10 kg/m3, so the ISA default overstates drag power by
roughly 10 %. Conservative, but not accurate - supply the forecast pressure and
temperature whenever they are available.

---

## 3. Regeneration and the negative-power policy

`P_tractive` goes negative on a steep enough descent, or when a tailwind
exceeds the ground speed. Two policies exist, selected by the `regeneration`
argument, and the choice is always recorded in `provenance.notes`:

| Policy | Rule | When to use |
|---|---|---|
| `"none"` (**default**) | `P_elec_traction = 0.0`; `P_total = P_aux` | Always, until the team **confirms** that the controller recovers energy. Assumes the surplus is lost in the brakes or by freewheeling. |
| `"regenerative"` | `P_elec_traction = P_tractive * eta_dt` (multiply) | Only with a confirmed, quantified regenerative-braking capability. `P_total` may be negative, i.e. net charging. |

Two deliberate decisions:

* **The default under-promises.** Clamping to zero means a car that does
  recover energy performs better than the model predicts. The opposite error -
  crediting recovery that does not exist - would silently justify a faster,
  infeasible strategy.
* **Efficiency multiplies, never divides, on the way back.** Dividing a
  negative power by `eta_dt` would make the recovered power *larger* in
  magnitude than the mechanical surplus, inventing energy. Multiplying means
  losses reduce what comes back, which is the physical direction.

The battery round-trip efficiency is **not** applied in the power model; it is
applied once, on the charge side, in `rwi.vehicle.battery` (§4). The two
efficiencies are therefore impossible to double-count, and a test asserts the
exact Wh for a charge-then-discharge cycle.

---

## 4. Energy store

A watt-hour accumulator with a policy reserve. Nothing more:

```
soc            = E / C
reserve_wh     = C * reserve_fraction
usable_wh      = C * (1 - reserve_fraction)
charge:    E' = min(E + E_in * eta_rt, C)       spill reported, warning
discharge: E' = max(E - E_out, 0)               shortfall reported, blocking
```

* **Round-trip efficiency is applied exactly once, on the charge side.** A
  single round-trip number describes a whole charge-then-discharge cycle, so
  taking it on one side is the whole of it. Taking `sqrt(eta)` on each side
  would be equally defensible for a symmetric pack, but then energy that is
  charged and never discharged (what is left in the pack at the finish) would
  carry half a loss it never incurred. Taking it on both sides in full would
  double-count. The choice is documented in the module and asserted in a test:
  1000 Wh offered at `eta_rt = 0.95` stores exactly 950.0 Wh, a subsequent
  1000 Wh discharge removes exactly 1000.0 Wh, and the cycle costs exactly
  50.0 Wh.
* **The reserve boundary is inclusive.** Discharging exactly the usable
  capacity lands *on* the reserve and is not a violation; one watt-hour further
  raises a `blocking` `battery_reserve` violation. Tested in both directions.
* **Exceeding capacity is a `warning`, not a blocker**: a real system curtails
  the MPPTs, so it is lost opportunity (`energy_curtailed_wh`), not damage.
* **Running out is `blocking`** (`battery_empty`), the state of charge is
  reported clamped at 0 %, and the unmet demand is carried separately as
  `energy_shortfall_wh` rather than hidden by the clamp.

### 4.1 Thermal behaviour: flagged, never modelled

There is **no thermal model**. No cell open-circuit-voltage curve, no internal
resistance, no current or C-rate limit, no coulomb counting, no ageing. A pack
or ambient temperature may be supplied; it is compared against a configurable
safe band (placeholder 0-45 degC) and a `warning` is raised outside it. The
temperature never changes an energy number, and the relationship between that
band and real pack behaviour is **unvalidated**. An unknown temperature raises
nothing - an absent measurement is not evidence of a safe temperature.

---

## 5. Scenario engine

### 5.1 Integration

Per segment (`rwi.strategy.segments.SegmentConditions`): pick the speed, check
the speed limits, compute the apparent wind, compute the demand, integrate
time and energy, push the net energy through the battery. Conditions are
constant within a segment, so discretisation averages away every hill and gust
inside it; shorter segments are strictly more faithful and cost only CPU time.

### 5.2 The assumption that most shapes the answers: generation is tied to position

A segment's generation is the number the caller supplied *for that piece of
road*, held constant while the vehicle is in it (and through any dwell, since
the array keeps working at a control stop). Consequently **a slower plan
collects more energy from the same forecast**.

That is right for a short re-planning horizon and wrong for a whole day: in
reality the sun moves, and a slower plan arrives at each segment later and
under a different sky. A time-consistent treatment requires re-running the
trajectory (`rwi.route`) and the irradiance and array model (`rwi.solar`,
`rwi.energy`) per candidate speed and feeding the result back in; the segment
interface is designed for exactly that, and until it is wired up the scenario
engine must be used with short horizons and read with this caveat in mind. The
caveat is repeated in `ScenarioResult.assumptions["generation_attribution"]` and
in `uncertainty_notes` of every result, not only here.

A per-segment **energy** figure (`generation_wh`) is available as the
alternative convention: fixed Wh per segment regardless of speed. The two
conventions cannot be mixed on one segment.

### 5.3 Constraints encoded

| Constraint | Severity | Trigger |
|---|---|---|
| `legal_speed` | **blocking** | target speed above `vehicle.max_legal_speed_kmh` |
| `speed_limit` | **blocking** | target or per-segment speed above a supplied posted limit |
| `battery_reserve` | **blocking** | state of charge would fall below the policy reserve |
| `battery_empty` | **blocking** | the pack cannot supply the demand at all |
| `power_limit` | **blocking** by default, configurable to `warning` | segment demand above a supplied power limit |
| `battery_capacity` | warning | charge beyond capacity; energy curtailed |
| `battery_temperature` | warning | supplied temperature outside the configured band |
| `arrival_deadline` | warning | arrival after a supplied deadline, or a deadline that could not be checked |

`feasible` is False if and only if at least one blocking violation was raised,
and an infeasible scenario can never be the recommended one.

Rationale for the two debatable severities: a speed limit is blocking because
**the platform must never recommend breaking one**, whatever the energy
arithmetic says; a deadline is only a warning because arriving late is a
competitive cost, not a rule the platform is entitled to treat as prohibitive.
A power limit blocks by default because a demand the hardware cannot deliver
makes the plan fictional, and can be downgraded when the limit is a
self-imposed advisory cap.

Repeated violations of the same constraint are compressed to one entry naming
the number of affected segments, so a 120-segment leg produces a readable list.

### 5.4 The four standard candidates

`standard_candidates()` builds exactly the four named in the project brief:

1. **conservative** - baseline minus 10 km/h (default), buying energy margin
   with time;
2. **balanced** - the baseline speed, the reference the others are compared
   against;
3. **faster-within-limits** - baseline plus 10 km/h (default), **capped at
   build time** at the smallest applicable legal limit, so the candidate itself
   never proposes an illegal speed;
4. **weather-aware adaptive** - the baseline modulated per segment by the
   heuristic below.

The margins are defaults chosen for plausibility. They are not optimised.

### 5.5 The weather-aware rule is a documented heuristic, not an optimum

```
v_seg = clamp( (v_target - k_w * w_head_kmh) * (1 + k_g * (g_seg/g_ref - 1)),
               v_min, v_max )

k_w = 0.5 (default), k_g = 0.2 (default)
w_head_kmh : along-track component of the TRUE wind, km/h, positive into the nose
g_seg      : the segment's generation index; g_ref the mean over all segments
v_max      : the smallest applicable legal limit
```

Why this shape: drag power grows with the apparent airspeed squared times the
ground speed, so speed bought into a headwind is the most expensive and speed
bought with a tailwind the cheapest; and energy is cheaper to spend where it is
being generated. `w_head_kmh` is obtained from `relative_wind` evaluated at
zero vehicle speed, so it is the wind's own contribution and not a function of
the speed being chosen - which keeps the rule a simple, deterministic,
single-pass function of its inputs.

Why it is **not** an optimum: the true optimum couples all segments through the
state of charge and depends on the entire remaining forecast. The gains `k_w`
and `k_g` have not been tuned against anything. Treat the candidate as "a
sensible weather-aware option to compare", never as "the best speed profile".

### 5.6 Uncertainty: an explicit two-point sensitivity sweep

With `uncertainty_fraction = f`:

| Case | Generation | Wind speed |
|---|---|---|
| pessimistic | `x (1 - f)` | `x (1 + f)` |
| base | `x 1` | `x 1` |
| optimistic | `x (1 + f)` | `x (1 - f)` |

The two outcomes are reported in `assumptions`
(`pessimistic_end_soc_fraction`, `optimistic_end_soc_fraction`,
`pessimistic_feasible`, ...) and summarised in `uncertainty_notes`, so a
recommendation can state how robust it is.

This is a **sensitivity sweep, not a probability distribution**. No confidence
level, exceedance probability or interval can be derived from it. In
particular, because a stronger tailwind *helps*, scaling the wind magnitude up
is a perturbation and not a worst case - it makes headwind segments worse and
tailwind segments better. Doing better means ensemble forecasts (available from
`rwi.providers`) propagated through the model, which is future work.

### 5.6a Generation bands (ADR 0008 phase 3)

`SegmentConditions` can carry `generation_p10_w` / `generation_p90_w` next to
`generation_w` (the P50). Both or neither; `P10 <= P50 <= P90`, all >= 0.
`generation_band_from_ghi` derives them from the quantile GHI of an ML forecast
(`rwi/ml-postprocess`, 80 % interval in `Measured.uncertainty`) by the ratios
`P10/P50` and `P90/P50` of GHI. That is linear in GHI: it ignores the
non-linear PV temperature and angle effects, so it is an approximation, and
it is noted as one.

- `evaluate_scenario(..., generation_band="p10" | "p50" | "p90")` runs the
  plan on the chosen quantile; `assumptions` records `generation_band` and
  `generation_band_segments`.
- When any segment has a band, the sensitivity sweep uses **P10 for the
  pessimistic case and P90 for the optimistic one** on those segments, instead
  of `x (1 -/+ f)`. Segments without a band keep the fraction; wind is always
  scaled by `f`. So `pessimistic_feasible` reflects the P10 risk.
- Per-segment P10 summed over a leg is **not** the leg P10: errors are
  correlated in time, so the sum is more pessimistic than a true 10 % case.
  The note says so; still no exceedance probability is claimed.

### 5.7 Determinism

Given the same inputs, `evaluate_scenario` returns a bit-identical
`ScenarioResult`. There is no randomness anywhere in `rwi.vehicle` or
`rwi.strategy`, and the only wall-clock dependence is the provenance timestamp,
which every public function accepts as an explicit `now_utc`. Tests assert full
`model_dump()` equality for a scenario, for a four-candidate comparison and for
the resulting recommendation.

---

## 6. `DEFAULT_VEHICLE`: complete assumption table

Every parameter below is `parameter_source="assumed"`: a generic,
order-of-magnitude figure for a BWSC Challenger-class single-seat car. **None
of it has been measured on, or supplied by, JU Solar Team, and it must not be
quoted as a specification or a prediction for the team's car.** It exists so the
model can be exercised and tested before the car exists. The same table lives
in the code, in `DEFAULT_VEHICLE.source_notes`.

| Parameter | Assumed value | Status | Measurement needed to replace it |
|---|---|---|---|
| `mass_kg` | 300 kg | assumed | Weigh the complete car in race trim on a calibrated scale: driver, ballast to the regulated minimum driver mass, battery, all equipment carried. Record date and scale. |
| `drag_area_cda_m2` | 0.10 m2 | assumed | Wind tunnel over the yaw range the route produces, or a coast-down campaign on a closed, flat, calm road (`fit_coastdown`). A single number is not enough; a drag-vs-yaw curve is needed. |
| `rolling_resistance_coefficient` | 0.005 | assumed | Tyre supplier test data at race inflation pressure, tyre temperature and load, cross-checked by the same coast-down campaign. Record the road surface. |
| `drivetrain_efficiency` | 0.92 | assumed | Dynamometer measurement of motor + controller + bearings over the real torque/speed envelope. It is a map, not a constant; this is a single-point stand-in. |
| `auxiliary_power_w` | 25 W | assumed | Shunt measurement of the real house load (telemetry, radio, lights, cluster, pumps, driver fan), separately for day and night configurations. |
| `battery_capacity_wh` | 5000 Wh | assumed | Full capacity test of the built pack at race temperature, not a datasheet cell sum. **TODO(regulations): the permitted energy-storage capacity for the 2027 Challenger class must be verified against the official BWSC 2027 regulations when published. This number is a model placeholder and asserts nothing about what the rules allow.** |
| `battery_reserve_fraction` | 0.10 | assumed (policy) | Not a measurement: a team decision by the strategy group and electrical lead, recorded with its reasoning (limp-home distance, pack ageing, SoC-estimation error). |
| `battery_round_trip_efficiency` | 0.95 | assumed | Charge/discharge energy measurement on the finished pack at race currents and temperatures. |
| `max_legal_speed_kmh` | 130 km/h | assumed | **TODO(regulations / traffic law):** verify per jurisdiction and per road section. 130 km/h is the NT open-road limit on parts of the Stuart Highway; South Australia is lower (110 km/h), and event regulations or road works may impose less. The strategy layer must be given the posted limit per segment; this is a last-resort ceiling only. |
| `yaw_drag_sensitivity_per_deg` | 0.0 | assumed (deliberately inert) | A measured drag-vs-yaw curve. Left at zero on purpose: inventing a curve would fabricate an aerodynamic property of an untested car. Zero means drag at yaw is underestimated. |
| Battery safe temperature band | 0-45 degC | assumed (placeholder) | Cell datasheet charge/discharge temperature limits plus the pack designer's derating policy. |

### 6.1 Replacing the assumptions with measurements

`fit_coastdown(speed_mps, power_w, ...)` fits `CdA` and `Crr` by least squares
to the flat-road, still-air, steady-state balance

```
P(v) = 0.5 * rho * CdA * v^3  +  Crr * m * g * v
```

which is linear in the two unknowns; the design matrix columns are
`0.5 * rho * v^3` and `m * g * v` and the solve is
`scipy.optimize.lsq_linear` with both parameters bounded non-negative (an
unconstrained solve of noisy data will happily return a negative `CdA`).
`CoastdownFit.apply_to(vehicle)` returns a `VehicleConfig` with the fitted
values and `parameter_source="measured"` - and takes an explicit
`parameter_source` argument so that a fit of *synthetic* data can stay labelled
"assumed".

Preconditions the caller owns and the fit cannot detect: flat road (or gradient
removed), still air (or apparent wind removed), steady speed, warm tyres, known
mass, known air density (a density error scales `CdA` by exactly that error).
The two terms are only separable across a decent speed span; below 5 m/s of
span the result carries an explicit "poorly separated" note. The function does
not differentiate a `v(t)` coast-down trace - whoever owns the measurement
supplies `(speed, power)` pairs, for instance via `P = -m * v * dv/dt`.

**No measurement data exists yet.** The fitter has only been exercised against
synthetic data generated from the model itself.

---

## 7. The optimiser

### 7.1 Formulation

`optimal_constant_speed` minimises travel time over one leg:

```
minimise    T(v) = D / v
subject to  SoC_finish(v) >= reserve_fraction
            v <= min(vehicle.max_legal_speed_kmh, speed_limit_kmh, per-segment limits)
            v >= min_speed_kmh
```

`T(v)` is strictly decreasing in `v`, so the optimum is the **largest feasible
speed**: either the legal cap, or the speed at which the energy constraint
becomes exactly active. The latter is a root of

```
h(v) = E_finish(v) - E_reserve
```

where `E_finish` is the *unclamped* finish energy recorded by
`evaluate_scenario` (`assumptions["end_energy_unclamped_wh"]`) - unclamped so
`h` stays smooth instead of flattening out once the pack hits empty.

### 7.2 Why `brentq` rather than a penalised `minimize_scalar`

One scalar variable, a monotone objective and a monotone constraint put the
optimum on a *boundary*, and locating a boundary is root-finding, not descent.
`scipy.optimize.brentq` is bracketed, derivative-free, guaranteed to converge
inside a sign-change bracket, and reaches `tolerance_kmh` in a handful of
evaluations. A penalised `minimize_scalar` would need a hand-chosen penalty
weight, could return a slightly infeasible point, and would blur "the
constraint is active" into "the optimiser stopped early". The two saturated
cases are handled explicitly *before* any root-finding - the cap is feasible,
or even `min_speed_kmh` is not - and the returned speed is re-evaluated and
verified feasible (stepping down by `tolerance_kmh` if floating-point error
tips it over) before it is reported.

Monotonicity of `h` in `v` is **assumed**, not proven: it is what the physics
gives for a fixed generation field, and only the endpoints are checked.

### 7.3 Benchmark against brute force

`tests/unit/test_strategy_optimiser.py` scans speeds from 30 km/h to the cap in
0.25 km/h steps using only `evaluate_scenario` and its `feasible` flag - an
implementation independent of the optimiser - and asserts that the optimiser's
answer is at or above the highest feasible grid point and no more than one step
above it. Further asserted: the optimum is feasible while one km/h faster is
not; the result saturates exactly at the legal cap when energy is abundant; a
per-segment limit caps it; infeasibility at every speed is reported as
`feasible=False, speed_kmh=None` and agrees with the scan finding nothing; and
the optimiser uses far fewer evaluations than the scan.

### 7.4 Limits - read this before quoting an optimal speed

* **Constant speed only.** This is the best single speed, not an optimal speed
  *profile*. It cannot trade speed between segments, time a cloud, exploit a
  hill, or place a stop.
* **No dynamic programming, no MPC.** The proper formulation is a DP or
  model-predictive-control problem over (position, state of charge, time) with
  the forecast and its uncertainty as inputs, and it is **future work**.
* **No traffic, no road trains, no overtaking, no convoy constraints.**
* **No cloud-timing optimisation** and no re-forecasting per candidate speed -
  see the position-versus-clock caveat in §5.2.
* It optimises **one leg** with the conditions it is given. It is not a
  whole-race plan.

---

## 8. Safety and regulatory constraints encoded

1. **No vehicle commands.** The platform produces advice, never control
   actions. (§0, and `SAFETY_NOTES` attached to every `Recommendation`.)
2. **Speed limits are blocking.** The vehicle's legal ceiling, a leg limit and
   any per-segment posted limit each make a scenario infeasible when exceeded,
   and an infeasible scenario can never be recommended. There is no override.
3. **The battery reserve is blocking.** It is a policy floor, justified by the
   state of charge being estimated rather than measured and by the model being
   unvalidated.
4. **A power limit blocks by default**, so a plan the hardware cannot deliver is
   not presented as available.
5. **Every recommendation carries a re-evaluation time.** A recommendation with
   no `reevaluate_after_utc` would be a standing instruction, which this
   platform must not issue.
6. **Confidence is capped at `"medium"`.** `"high"` is unreachable by
   construction while the models are unvalidated; `"medium"` additionally
   requires that a pessimistic sensitivity case was run, that it is also
   feasible, and that the chosen scenario stays at least 5 percentage points of
   state of charge above its reserve at the worst moment of the leg.
7. **No least-bad fallback.** If no scenario is feasible, `recommend` returns
   `recommended_scenario=None` and explains why each candidate failed.
8. **Every recommendation requires human review** by the strategy group before
   it influences the car. The driver and observer remain responsible for the
   road, the traffic and the regulations.
9. **TODO(regulations):** energy-storage capacity limits, control-stop rules and
   driving-window rules must be verified against the official BWSC 2027
   regulations. Nothing in this subsystem asserts what those rules permit.

---

## 9. Validation status

Vocabulary: *not started / researching / implementing / implemented, not
validated / tested / validated against real data / operationally ready*.

| Component | Status |
|---|---|
| Longitudinal power balance (`vehicle/dynamics.py: consumption`) | tested |
| Apparent-wind treatment of aerodynamic drag | tested |
| Yaw-dependent drag (`yaw_drag_sensitivity_per_deg`) | implementing (inert by default; needs a measured drag-vs-yaw curve) |
| Air-density handling and ISA fallback | tested |
| Regeneration / negative-power policy | implemented, not validated |
| `DEFAULT_VEHICLE` parameters | implemented, not validated (every value assumed; see §6) |
| Coast-down parameter fit (`fit_coastdown`) | tested (against synthetic data only; no measurements exist) |
| Energy store, reserve and capacity logic (`vehicle/battery.py`) | tested |
| Battery thermal behaviour | implementing (temperature is flagged against a placeholder band; no thermal physics) |
| Segment construction (`strategy/segments.py`) | tested |
| Scenario integration and constraint checks (`strategy/scenarios.py`) | tested |
| Four standard candidates | tested |
| Weather-aware adaptive heuristic | implemented, not validated (deliberately untuned) |
| Constant-speed optimiser | tested (benchmarked against a brute-force scan) |
| Uncertainty sensitivity sweep | implemented, not validated |
| Recommendation assembly (`recommend`) | tested |
| Dynamic-programming / MPC speed profile | not started |
| Coupling of generation to clock time rather than position | not started |
| Whole-race (multi-leg, multi-day) planning | not started |

**No component is "validated against real data" and none is "operationally
ready".** Nothing in this subsystem has been compared against a measurement of
any real solar car: not a drag figure, not a rolling-resistance figure, not a
pack efficiency, not an energy balance over a real leg. "tested" above means
exactly and only that the implemented equations behave as specified under unit
tests with hand-computed reference values - it is a statement about the code,
not about the car.
