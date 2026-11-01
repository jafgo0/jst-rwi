# ADR 0002: Licence decision for the repository

Status: **OPEN** (no licence granted). Recommendation below. Interim state: all rights reserved,
repository private; `pyproject.toml` already says "Proprietary - JU Solar Team (license decision pending)".
Date: 2026-11-01

## Context

JST-RWI is written for a university competition team. The master specification asks for a "license
decision" at repository initialisation (section 13, step 1). The decision is not purely technical: it
depends on who owns the work (the team, Jonkoping University, sponsors, individual students), on the
team's publication policy, and on obligations that come with data and dependencies. Nothing in this ADR
is legal advice; the team should ask the university's legal or innovation office.

Facts established so far
* The 2027 Regulations keep technical details of vehicles confidential until Static Scrutineering
  (clause 1.27.1) and require approval for commercial use of recordings of the event (1.27.6). They do
  not mention software licensing. Whether the BWSC organisers or the team's sponsors restrict
  publication of strategy tooling during the event: **not found**; ask.
* Code in this repository has no vehicle data yet. Once vehicle parameters (CdA, mass, battery, array)
  are committed they are competitively sensitive; keep them out of any public repository regardless
  of the code licence (configuration lives outside git, see `.env.example` and ADR 0006).
* Data licences are separate from the code licence (see below).

## Options

| Option | What it means | Fits when | Risks |
|---|---|---|---|
| A. Proprietary / team-internal (all rights reserved, private repo) | No one outside the team may use or copy it. | The team wants a competitive edge in 2027, or ownership with the university is unresolved. | Other teams and future JU members cannot reuse it without permission; no outside contributions; "open science" credit lost; dependency on repository access staying private. |
| B. MIT | Very permissive; keep notice. | The team wants maximum reuse and simplicity. | No patent grant; anyone, including competitors, may use it freely with no obligation to contribute back. |
| C. Apache-2.0 | Permissive plus explicit patent grant and notice requirements. | Same as B but with clearer contributor and patent terms, which suit an institution. | Slightly longer notice obligations; same competitor-reuse consequence as B. |
| D. Copyleft (GPL-3.0 / AGPL-3.0) | Derivative works must be shared under the same terms. | The team wants to force reciprocity. | Hard for sponsors and other teams to adopt; AGPL has network-use implications; probably over-restrictive for this project. |
| E. Dual: proprietary during the event, then Apache-2.0 release | Private through August 2027, public afterwards with all vehicle data removed. | Wants both the edge and the legacy. | Needs the decision recorded now so contributors assign or license their work accordingly. |

## Constraints that apply whichever option is chosen

* **OpenStreetMap-derived route geometry is covered by ODbL** (Open Database Licence). If the team builds
  route geometry from OSM data and publishes the database or a derivative database, it must attribute
  OpenStreetMap, indicate changes, and offer the derived database under ODbL (share-alike); a
  produced work such as a rendered map needs attribution. ODbL attaches to the **data**, not the code:
  keep OSM-derived files in `data/routes/` with their own licence note, separate from the code licence.
  The official BWSC route notes, when published, are a different source with their own terms (unknown).
* **Weather data licences** are not changed by our code licence: Open-Meteo data is CC BY 4.0
  with attribution, ECMWF open data CC BY 4.0, BoM data has its own terms (see
  `docs/research/data-sources.md`). Redistributing cached weather files is subject to these.
* **Third-party libraries** (believed to be permissive: BSD/MIT/Apache families for FastAPI, Pydantic,
  httpx, NumPy, pandas, SciPy, pvlib, pyproj, timezonefinder, gpxpy; this was not machine-checked here)
  do not force a copyleft on us. Run a licence scan before any release.
* **Map tiles**: public OpenStreetMap tile servers have a usage policy that does not allow heavy or
  offline-bulk use; a tile source and its licence must be chosen separately.
* **Regulations text** is CC BY 4.0 (Government of South Australia); quoting it in docs needs the attribution
  given in `docs/research/bwsc-2027-regulations.md`.
* **Contributor ownership**: students contribute under university rules; confirm who owns IP before any
  public licence is attached.

## Decision

OPEN. The platform will not carry any open-source licence until the team has answered the questions
below. Until then: private repository, no `LICENSE` file granting rights, and `pyproject.toml` stays
"Proprietary - license decision pending".

## Recommendation

Option **E**: keep the repository private and proprietary through the 2027 event (vehicle-sensitive
configuration never committed), and plan an **Apache-2.0** release of the code after the event, with
vehicle parameters removed, OSM-derived data under ODbL in its own folder, and provider data left to
be fetched by users under their own licences. Reasons: it protects the competitive window, avoids
deciding IP ownership under time pressure, preserves the legacy, and Apache-2.0's patent grant and
contributor terms suit a university. If the team prefers openness from the start, Apache-2.0 now is
also acceptable provided no vehicle data is ever committed.

## What the team must confirm

1. Who owns the code: JU Solar Team, Jonkoping University, or individual authors? Is there a university
   open-source policy?
2. Do sponsors or the competition (BWSC organisers) restrict publication of strategy or telemetry tools?
3. Is a public repository acceptable to the team's strategists before the race?
4. Will any OSM-derived route data be used, and will the derived database ever be published?
5. Who may sign off a release licence (team manager, faculty supervisor)?

## Consequences

* Until decided, outside contributors cannot be accepted and the code cannot be shared with another team.
* A later change from proprietary to open is easy; the reverse is not, so the conservative default is correct.
* This ADR must be superseded by a numbered ADR recording the final choice.

## Alternatives considered

See the options table; E is recommended, A is the interim state, B/C are the open end states,
D is not recommended.
