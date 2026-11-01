# BWSC 2027 regulations: verified facts and constraints on JST-RWI

Status of this document: research note, written 2026-11-01 by the documentation engineer.
It records what the official documents actually say, with clause numbers, and what
they do **not** say. Nothing here is taken from memory of earlier events unless it is
explicitly labelled `NOT FROM THE REGULATIONS`.

Attribution (required by the document's CC BY 4.0 licence): The Government of South
Australia, 2027 Bridgestone World Solar Challenge Event Regulations, April 2026,
https://www.worldsolarchallenge.org/the-challenge/regulations . Clause texts below are
paraphrased, not copied. The regulations are a design-and-operations rulebook, not
legal advice; the Jury is "the only authority empowered to determine the interpretation
of these Regulations" (1.10.2).

## 1. Sources and how they were read

| Source | Version / date | How read | Result |
|---|---|---|---|
| 2027 BWSC Event Regulations | V1.0, published 7 May 2026, 72 pages | `pypdf` text extraction (`-I` mode, path passed as argument, output kept outside the repo) | Text layer present, **not** scanned. All 72 pages yielded text. Diagrams, tables drawn as images and the formula glyphs for the braking and Cruiser-hold formulas are only partly legible; no figure content was used. |
| 2027 BWSC Team Manager's Guide | V1, published 7 May 2026 | same method | Text layer present. Used only where cited as `TMG p.N`. It is not the Regulations; it says its own text may be updated by later editions. |
| Official web pages `worldsolarchallenge.org/2027-event/regulations` and `/about-us/route-map` | fetched 2026-11-01 | web fetch | The regulations page lists: Regulations V1.0 (7 May 2026), entry Terms and Conditions, Stability Considerations paper (Storey, dated 17 April 2026), Team Manager's Guide V1, Freight Manual V2.0 (10 June 2026), PVC Pat drawing, Solar Wombat 2027. **No Regulation Bulletin, no route notes, no control-stop list and no distance chart are listed.** The route-map page gives the route in words only. |

Documents referenced by the Regulations that we have **not** read (they do not exist yet
or were not obtained): Regulation Bulletins, Team Notices, the Event Schedule, the 2027
Route Notes, the Freight Manual, the team portal documents (critical-components list,
Solar Wombat electrical diagram). Any of them may add rules.

## 2. Verified facts

Legend: **V** = verified in the cited clause. **NF** = searched for and not found.
All times are ACST (UTC+9:30) because the Regulations fix the event time zone (1.3.1).

### 2.1 Event, route, schedule

| Topic | Status | Fact | Where |
|---|---|---|---|
| Event time zone | V | ACST = UTC+9.5 is the time zone for the event. | 1.3.1, Glossary |
| Event dates | V | Official start Sunday 22 Aug 2027, State Square Darwin, from 07:30; first vehicle released 08:00. Official finish line opens in Adelaide Thursday 26 Aug. Awards Sunday 29 Aug evening. Event period 7 Aug to 1 Sep 2027. | 1.12.2 schedule table, 3.18.1-2, 4.5.1 |
| Route | V | "Major highways" Darwin to Adelaide, approximately 3000 km, on public roads under normal traffic law. | 1.4.1, 3.20.1 |
| Route detail | V (TMG) | Stuart Highway Darwin to Port Augusta, then Highway 1 to Adelaide, "approximately 3,020 km". | TMG p.31 |
| Route may change | V | The organiser may change the course at any time. Detour signs must be obeyed. | 3.20.4-5 |
| Route notes | V (conflict) | Printed copy given to each team. Regulation 3.20.2 says downloads from "June 2025" which is evidently a carried-over typo. The Team Manager's Guide says route notes are compiled by the organiser, reviewed "at the last possible moment" and available for download from **June 2027**. | 3.20.2, TMG p.31 |
| Control stops: where | V | Mandatory; locations are published in the route notes. The Guide says declared locations and operating times follow a route survey in **June 2027** and that **undeclared control stops may be created at any time**. | 3.20.3, 3.27.1, TMG p.34 |
| Number of control stops | **NF** | The Regulations do not give a number or a list. | search of full text |
| Route Notes / distance chart published? | **NF as of 2026-11-01** | Not on the official pages checked; not referenced as available. | web pages above |

### 2.2 Event classes

| Topic | Status | Fact | Where |
|---|---|---|---|
| Classes | V | Four: Historic (previous-event vehicles, parades and special events), Toyota Challenger (single-seat, competitive), Cruiser (two or more seats, judged on practical design plus efficiency), Innovation (non-competitive). | 2.1.1 |
| Which class JU Solar Team enters | **NF** (not a regulation matter) | Must come from the team's entry. Do not assume. All class-dependent numbers below must be parameters. | n/a |
| Challenger energy source | V | Powered by solar irradiance collected by the vehicle; the only external energy input is what is in the energy storage at the start. | 2.1.2 |
| Cruiser energy source | V | Designed for renewable energy; regenerative braking, gradients and wind may be used; may be recharged externally overnight (between the end of one day's driving and the start of the next), but not once driving has started that day. | 2.1.3, 2.6.1, 3.19.5 |

### 2.3 Solar collector

| Topic | Status | Fact | Where |
|---|---|---|---|
| Challenger area limit | V | At most **6 m2** of solar collector area, "no tolerance on the limit". Area counts directly exposed front area of all PV cells, exposed rear area of bifacial cells, other energy-conversion surfaces, and surfaces that transmit, refract or reflect light onto converters. Encapsulation within 10 mm of the minimum rectangle around a cell is not counted. The limit holds for **every possible configuration**, driving and solar charging. | 2.4.2 |
| Cruiser area limit | V | Unrestricted, but inside the vehicle dimensions. | 2.4.3 |
| Allowed cell technologies | **NF as a positive list** | The Regulations say PV is "the most common type" and that other collector types need organiser approval. There is no list of permitted PV technologies. | 2.4.1 |
| Prohibited technologies | V | Materials with high environmental toxicity, "including GaAs, CdTe and CuInSe2", must not be used for solar energy conversion. (Note: `ArrayConfig` in `contracts.py` mentions GaAs in a comment; GaAs is not permitted in 2027.) | 2.4.4 |
| Devices that raise irradiance | V | External devices intended to increase irradiance on the collector or its efficiency must never be used. Water spraying not allowed; cleaning with water only when the Sun is below the horizon. | 3.19.1 |
| Charging equipment | V | All devices used for solar charging (stands, supports, cables) must be carried in the vehicle. | 2.4.5 |
| Dimensions | V | Fit inside a 5800 x 2300 x 1650 mm prism. | 2.2.1 |

### 2.4 Energy storage

| Topic | Status | Fact | Where |
|---|---|---|---|
| Capacity limit | V | At most **11 MJ for Challenger** (= 3.056 kWh) and **55 MJ for Cruiser** (= 15.28 kWh). Cell energy is `3600 * V_nominal * q` using the manufacturer's nominal voltage and the largest of rated, minimum and typical capacity. This is nominal cell energy, not usable energy; the usable figure the strategy engine needs is lower and is the team's own parameter. | 2.5.2 |
| Packs | V | At most two rigid, tool-sealed packs; cells must stay inside manufacturer voltage, current and temperature limits and the vehicle must enforce this automatically. | 2.5.4-2.5.6 |
| Seals / Observer | V | The energy storage must never leave the Observer's control during the on-road event. Challenger packs must be sealable so no electrical connection can be made. | 3.19.2, 2.5.15 |
| Driver display battery | V | A commercial phone/tablet used as a driver display must be powered from the vehicle through a blocking diode within 200 mm of the device, must not be externally charged during the event, and **its battery capacity counts toward the storage limit**. | 2.5.9 |
| Instrumentation | V | Speed, indicator and hazard state, energy-storage warnings (and electronic rear-vision images) must always be visible to the driver, not behind menus; instrumentation is powered from the energy storage, not separate batteries. | 2.25.1-2 |

### 2.5 Daily timing, driving window, stops

| Topic | Status | Fact | Where |
|---|---|---|---|
| Daily start | V | Official start 08:00 every day. Starting early costs 2 min per minute early ("no flying starts"). | 3.21.1-2, 3.21.5 |
| Daily finish | V | Official finish time 17:00. Late: 1 min penalty per minute for the first 10 min, 2 min per minute beyond; the penalty shifts the next day's start by the same amount. | 3.21.3-4 |
| Night driving | V | "Must not be driven after sunset"; no driving on a public road between sunset and sunrise. | 3.21.4, 3.24.8 |
| Control stop duration | V | **30 minutes** plus any penalty time set by the Clerk of the Course. Timing starts when the arriving driver activates the control-stop timing system. | 3.27.7-8 |
| Control stop overnight rule | V | Control-stop timing is paused at 17:00 and resumes at 08:00 next day; teams must remove the vehicle at 17:00 and return ready for 08:00; the morning space is unshaded if possible; the collector orientation need not match the previous afternoon. TMG: arriving with under 30 min to closure means the remainder is served next morning after check-in. | 3.27.9, TMG p.34 |
| Control stop handling | V | Arriving driver alone may reconfigure for charging before timing starts. Others must not touch the vehicle until 5 minutes before the end. Departing driver alone may reconfigure 5 minutes before the end. The Control Stop Manager "will endeavour" to find unshaded spaces but cannot guarantee it. Ballast is removed at control stops. | 3.27.4-6, 3.27.10-12, 2.8.1 |
| Driver-change stops | V | Compulsory driver changes at locations to be specified in a **Regulation Bulletin**; vehicles stop 5 min at each, separate from any overnight stop. | 3.23.4 |
| Overnight stop | V | Starts when driving stops for the day and ends when driving restarts. Overnight camp at least 10 m from the road. The vehicle may be driven, pushed or carried by hand to its solar charging location; no limit on people reconfiguring the collector for charging. | 3.28.1-7 |
| Challenger storage lock-out | V | Observer must be satisfied Challenger storage cannot be charged between **19:00 and 06:30**. Challenger packs must be disconnected and sealed before **19:15** each day and must not be unsealed before **06:15**. Reading (ours, to be confirmed): solar charging of a stationary Challenger vehicle is allowed from stopping (about 17:00) until the packs are sealed (19:00-19:15) and again from about 06:30 until the 08:00 start. | 3.17.2, 3.28.8-9 |
| Minimum progress | V | "No longer competing" if the team cannot hold a **minimum of 60 km/h** on the open road, cannot reach the next control stop before it closes, or arrives after it closed. TMG adds that teams "achieve 500 km per day". | 3.29.1, TMG p.34 |
| Cruiser hold after Katherine | V | Holding duration `d = 300 (s* - s) / s*` minutes from the design score, not beyond 08:00 on day 2. Relevant only if the team is Cruiser. | 3.21.6 |

### 2.6 Speed limits and road rules

| Topic | Status | Fact | Where |
|---|---|---|---|
| Road law | V | All activity is under applicable traffic codes and civil law; comply with road authorities, police and officials. | 1.4.1, 3.24.9 |
| Speed limit as a rule | V | Exceeding **any posted speed limit** is a penalty offence (penalty set by the Clerk of the Course). Driving under **50 km/h** where the limit is 100 km/h or more, when not constrained by traffic, is also penalised. | 3.31.6 |
| Posted limits on the route | V (TMG) | National defaults 50 km/h urban, 100 km/h open road unless signposted, 25 km/h passing roadworks and emergency vehicles; "some sections of the Stuart Highway have a limit of 130 km/h". The Regulations contain no segment table of limits. | TMG p.16 |
| Segment-level speed-limit table | **NF** | Not in the Regulations or Guide. Needed by the strategy engine; must be sourced (route notes or road-authority data). | search |
| Visibility penalty | V | Driving "without adequate visibility, due to smoke, dust or rain" is a penalty offence. | 3.31.6 |
| Penalties | V | Demerit points = 30 min time penalty each; 3 points = exclusion; Clerk of the Course may impose operational restrictions "such as speed restrictions". | 3.31.1-4 |
| Escorts | V | Front escort within 500 m, rear escort within 5 s; driving without both escort vehicles is penalised. | 3.24.6-7, 3.31.5 |
| Crosswind / gusts | V | The vehicle must be stable "at all achievable speeds and in crosswinds likely to be encountered", with analysis approved by the certifying engineer. Explanatory note: in previous events vehicles left the road in gusts; "wind gusts exceeding 100 km/h are possible". Static stability factor must exceed 1 (45 degree tilt). | 2.19.3-4 |

### 2.7 Telemetry, weather data, external assistance, communications

This is the section that matters most for JST-RWI.

| Topic | Status | Fact | Where |
|---|---|---|---|
| Rules about weather forecasts, meteorological information or weather services | **NF** | Full-text search of the Regulations for "weather", "forecast", "meteorolog", "cloud" finds **no** rule. The only "weather" hits in the Guide are general advice (acclimatise, wear suitable clothing, drive "in accordance with road and weather conditions"). | grep of both texts |
| Rules about strategy software, outside computation, AI tools, or "external assistance" to the driver | **NF** | No occurrence of "strateg", no "external assistance" clause. The Regulations contain no rule on what a support crew may or may not tell the driver. The only information-flow rules found are: voice radio to the rear escort is required (3.8.1), communication with the Clerk of the Course goes through Mission Control (3.1.2), Observers must not advise or direct teams (3.17.3), and teams must not demand that the Observer assist team operations (3.17.4). | grep of both texts |
| Team telemetry | **NF** in Regulations; partly V in Guide | The Regulations have a heading "Tracker and telemetry" but regulate only the organiser's tracker. The Guide says telemetry radio equipment must comply with Australian civil law and with ACMA rules, that ACMA's primary concern is interference to licensed users, that low-power devices in the desert are unlikely to be a problem, and warns of licensed users transmitting "telemetry or data packets" on out-of-band frequencies. | 2.9, TMG p.36 |
| Organiser tracker | V | A self-contained, self-powered tracker box (120 x 120 x 70 mm, up to 2 kg) is provided by the organiser, needs no electrical connection to the vehicle, needs a radio-transparent 120 degree cone for its antenna, and is returned at the end. Tracking data are not accepted as the basis for protests; "information concerning progress and operation of competition vehicles may be published". Whether **teams** receive the tracker data: **NF**. | 2.9.1-3, 3.22 |
| Mandated communications | V | Two-way voice radio between competition vehicle and rear escort, working even in safe state; a satellite phone and two 80-channel UHF CB radios are issued by the organiser and the satellite phone must be on during event hours. | 3.8.1-4, TMG p.36 |
| Mobile and internet coverage | V (TMG) | The route has "large distances with no cellular coverage"; Telstra has the best coverage; Wi-Fi at event sites "should not be relied upon for your team operations". | TMG p.13 |
| Observer seat and equipment | V | The Observer must have the front passenger seat of the **rear escort vehicle**, with fair and reasonable space: the footwell must not be filled with computer equipment. | 3.17.5 |
| Fair-play clauses | V | Entrants must act fairly and in good faith; penalties for deliberately seeking an unfair advantage or departing from the "spirit of the Event"; exclusion for misrepresentation. | 1.6.3-4, 3.31.7 |
| Cruise control / automatic driving | V | Any cruise control or automatic-driving function must deactivate on brake or manual input and must not resume by itself. | 2.26.1-2 |
| Drones | V | Remotely piloted aircraft need organiser approval and CASA compliance. | 1.28 |
| Confidentiality | V | The organiser keeps technical details of vehicles confidential until the start of Static Scrutineering. | 1.27.1 |

### 2.8 Scrutineering and qualification

| Topic | Status | Fact | Where |
|---|---|---|---|
| Documentation | V | Online forms by deadline: Groups A and B (team info; mechanical, electrical, **Solar Collector Specification**, **Energy Storage System Specification**) due 5 Mar 2027; Group C (storage incident plan, safety plan) 30 Apr 2027; Group D 28 May 2027; Group E (roadworthiness certificate) 15 Aug 2027. No vehicle enters Static Scrutineering until documentation is approved. | 1.29.1 |
| Static Scrutineering | V | Starts Monday 16 Aug 2027. Includes an NT Road Transport Authority regulatory and roadworthiness inspection; checks include "solar collector type and size"; all energy storage devices must be declared; qualification must be achieved in road-ready condition. | 1.12.2, 3.12 |
| Dynamic Scrutineering | V | Figure-8 during static week; the rest on Saturday 21 Aug (07:00 roll call; closes 12:30). No vehicle starts until it has passed. | 3.14 |
| Occupant mass | V | Occupant plus ballast at least 80 kg; weighed at Static Scrutineering; ballast supplied by the organiser and removed at control stops. | 2.8.1, 3.12.8 |
| Changes after scrutineering | V | No design or driver changes after passing Static Scrutineering except in exceptional circumstances approved by the Chief Scrutineer; parts used must be carried on the route. | 3.15 |
| Entry | V | Applications close midday Fri 9 Oct 2026 (ACST); fees payable by noon 11 Dec 2026; Challenger fee AUD 20,000 early / 22,000 standard. | 1.13.1, 1.14 |

## 3. What this means for the platform

These are our engineering readings, not rulings. Each one names the module affected.

| # | Constraint | Consequence | Module |
|---|---|---|---|
| R1 | No clause on weather data, strategy software or AI. | Not a licence to assume it is allowed. Ask the organisers in writing (section 5, Q1); until answered, design for the conservative case: the platform is advisory, runs on team equipment, and no output is a vehicle command. | all, `strategy` |
| R2 | Daily driving window 08:00-17:00 ACST and no driving between sunset and sunrise; control-stop timing paused 17:00-08:00. | `build_trajectory(daily_window_local=...)` must default to this window, stop the trajectory at the day end, and carry the remaining distance to the next morning. Sunset must come from `solar_day_events` at the vehicle's position, not from a table. | `route`, `solar` |
| R3 | 30 minute control stops with unshaded parking and collector reconfiguration. | The energy forecast needs a stationary, tilted-collector mode for control stops and the two evening/morning stationary charging windows; the area cap applies to every configuration. | `energy`, `route` |
| R4 | Minimum 60 km/h on open road and ~500 km/day; maximum is the posted limit (100 km/h default, some 130 km/h sections). | Strategy bounds must be per-segment, from a speed-limit table that does not exist yet (open item). A recommendation below 60 km/h average or above the posted limit is invalid. | `strategy`, `route` |
| R5 | Penalty for driving without adequate visibility (smoke, dust, rain); gusts above 100 km/h possible. | Alert categories `visibility`, `precipitation`, `gust`, `crosswind` map to real penalties and to the certifying-engineer stability analysis; the platform warns, humans decide. | `alerts` |
| R6 | Observer sits in the rear escort and the footwell must stay free of computer equipment. | The strategy workstation cannot live in the Observer's seat space. Location of the operator and the data path to the vehicle is a team decision to clear with the organisers. | operations |
| R7 | Driver display, if a phone/tablet, counts against the storage limit and must be vehicle powered. | Recommendations reach the driver by voice radio from a strategist; a driver-facing display from this platform is out of scope until the team decides. | operations |
| R8 | Cellular coverage is poor on large stretches; Wi-Fi unreliable. | Offline-first operation is a hard requirement, not an option: cache, last-known-good, visible data age (ADR 0004, ARCHITECTURE section 8). | `providers`, `api` |
| R9 | Organiser tracker is self-contained; no evidence teams get its feed. | The live-GPS source is the team's own receiver on team telemetry, which the Regulations do not regulate beyond ACMA/civil law. | `core.position` |
| R10 | Course can change at any time; undeclared control stops possible. | Route data must be replaceable at runtime and control stops treated as editable waypoints with provenance. | `route` |
| R11 | Collector area <= 6 m2 and storage <= 11 MJ (Challenger); GaAs, CdTe, CuInSe2 prohibited. | `ArrayConfig` and `VehicleConfig` should be checked against the class limits at load time (a validation, not a default). The `ArrayConfig` comment naming GaAs should be corrected by the integrator. | `energy`, `vehicle`, `contracts` |
| R12 | Event time is ACST (UTC+9:30). | Regulation deadlines and daily times must be shown in ACST as well as the position's own local time. NOT FROM THE REGULATIONS: both NT and SA are expected to be on UTC+9:30 in late August (South Australian daylight saving starts in October), so the `timebase.py` module docstring that says NT and SA differ by 30 minutes is not true for the race dates; the geography-derived approach is still correct and should be kept. | `core.timebase` |
| R13 | Telemetry and position are sensitive until published by the organiser. | Access control and no public exposure of live position or energy state. | `api` |

## 4. Verified versus not found: summary

* **Verified (with clause):** classes; Challenger 6 m2 and 11 MJ limits, Cruiser 55 MJ and
  unrestricted area; prohibited collector materials; 08:00 start, 17:00 finish and late-finish
  penalties; no driving between sunset and sunrise; 30 min control stops with the 17:00 to 08:00
  pause; 60 km/h minimum; posted-limit and 50 km/h penalties; visibility penalty; event time zone
  ACST; mandated radios; organiser-supplied tracker; Observer seat rule; fair-play clauses;
  scrutineering and documentation dates; Challenger storage lock-out 19:00-06:30; 5 min driver-change
  stops; the route is Stuart Highway then Highway 1.
* **Verified only in the Team Manager's Guide (not in the Regulations):** route about 3,020 km;
  route notes and control-stop locations from June 2027; undeclared control stops possible; the 130 km/h
  sections; patchy cellular coverage; ACMA statement on telemetry; "500 km per day".
* **Not found:** any rule on weather data or forecasts; any rule on strategy software, AI or
  external computation; any rule on what the support crew may tell the driver; whether teams
  receive the organiser's tracker feed; a positive list of allowed cell technologies; the number
  and list of control stops; driver-change locations (promised in a Regulation Bulletin); a
  segment-level speed-limit table; the official route geometry; the 2027 Route Notes and distance
  chart (**not published as of 2026-11-01**).
* **Internal inconsistencies noticed in V1.0:** 3.20.2 says route notes download "from June 2025";
  4.6.1 says "Wednesday 1 September 2026" where the schedule says 2027; 3.17.2 (19:00 and 06:30)
  and 3.28.8-9 (19:15 and 06:15) give different clock times for the Challenger storage lock-out,
  which is consistent only if read as "sealed by 19:15, not charged between 19:00 and 06:30"
  and should be confirmed.

## 5. Open questions for the organisers

Send to `teams@worldsolarchallenge.org` (1.30.1). If an answer restricts what teams may do the
organiser publishes it for all teams (1.30.2); an answer that does not restrict is not published,
so keep our own copy.

1. Is the use of weather forecasts and meteorological services, and of team-operated strategy
   software that computes outside the vehicle (including AI-based decision-support tools) and
   receives data over cellular or satellite links, permitted without restriction during the
   on-road event? The Regulations are silent.
2. Is there any restriction on what a strategist or support crew may communicate to the driver by
   radio (for example speed or energy advice derived from forecasts)?
3. May a team operate its own telemetry link from the vehicle to the escort or support vehicles, on
   which frequencies, and does the team need an ACMA licence or class licence for it? May the
   telemetry include position, speed, power and state of charge?
4. Do teams receive the organiser tracker's position data, from Mission Control or otherwise (format,
   latency, licence)? Is the live position of other teams available to teams, and may it be used in
   strategy?
5. Will the official route geometry (GPX/KML/GeoJSON) be published with the Route Notes, with what
   licence for reuse, and when? What is its accuracy?
6. When will the declared control stops, their operating hours, the driver-change locations (3.23.4),
   the urban-area reference point and the marshalling point (4.2.2, 4.2.5) be published, and by what
   mechanism will undeclared control stops be announced?
7. Does the organiser publish the posted speed limits segment by segment, or must teams compile them?
8. Confirm the reading of 3.17.2 and 3.28.6-9 that a stationary Challenger vehicle may solar-charge
   between stopping for the day and 19:00 (packs sealed by 19:15) and again from 06:30 until the
   08:00 start; confirm the clock times in the two clauses.
9. May a pyranometer, anemometer or other weather sensor be mounted on the competition vehicle or the
   escort vehicles, within the dimensional prism (2.2.1) and subject to the Observer rules?
10. Where may the strategy operator and equipment be located, given that the rear escort front
    passenger seat and its footwell are reserved for the Observer (3.17.5)? Is a strategy vehicle
    separate from the escorts acceptable?
11. Correct or confirm the typographic inconsistencies listed in section 4 (3.20.2 "June 2025", 4.6.1
    "2026").

## 6. Open questions for the team (inputs the platform needs)

1. Competition class and therefore the numeric limits (Challenger assumed nowhere in code).
2. Solar array: area, cell type and datasheet, temperature coefficient, mounting geometry, whether the
   array tilts at control stops, MPPT efficiency (the Group B Solar Collector Specification due
   5 March 2027 will contain most of this).
3. Battery: nominal energy, usable fraction, reserve policy, voltage and current limits (Group B Energy
   Storage System Specification).
4. Vehicle: mass with driver and ballast, CdA, rolling resistance, drivetrain efficiency, auxiliary power,
   yaw-drag sensitivity.
5. Telemetry: protocol, fields, rate, link, and who owns the receiver (needed before Phase 3).
6. BoM licensing path (see `data-sources.md`).
7. Whether to attend with a dedicated strategy vehicle (R6).
8. Who is the named human decision-maker for strategy and for safety-related alerts.
9. Licence of the repository (ADR 0002) and whether the code may be made public given 1.27.1 and
   team policy.
