# Data sources: licence, access and cost facts

Verified 2026-11-01 from official documentation pages, by the documentation engineer.
Scope: **licence, cost, access method, cadence, nominal resolution only.** Variable lists,
methods, bias and skill belong in `docs/science/providers.md` (another author); this file
does not repeat them and nothing here says anything about forecast accuracy.

Verification levels used below:

* **READ** - the official page was fetched and read on the date above.
* **SNIPPET** - the official page could not be fetched (HTTP 403 to automated access); the fact comes from
  a web-search extract of that official page. Treat as unverified until a human reads the page in a browser.
* **NOT VERIFIED** - could not be established.

## 1. Summary

| Source | Cost | Licence | Fit for JST-RWI | Verification |
|---|---|---|---|---|
| Open-Meteo forecast / ensemble API | Free tier, **non-commercial only**; paid tiers for commercial use (prices not stated on the pages read) | Data CC BY 4.0, attribution required | Primary phase-1 candidate. Team status as "non-commercial" must be confirmed. | READ |
| ECMWF open data (IFS, AIFS) | Free | CC BY 4.0 plus ECMWF terms; commercial use allowed with attribution | Direct GRIB access is a later upgrade path; heavier engineering. | READ |
| BoM (Australian Bureau of Meteorology) | Anonymous FTP free but **not for commercial use**; Registered User Services carry fees; commercial licence separate | Default BoM copyright / Express Licence Agreement / Data Licence Agreement | Authoritative local observations and NWP, but licensing path needs a human decision. | SNIPPET (403) |
| Himawari-8/9 (JMA) via NOAA on AWS | Free, no account | JMA data, openly distributed by NOAA, attribution requested | Satellite imagery source for Phase 5 nowcasting. | READ |

## 2. Open-Meteo

Pages read: <https://open-meteo.com/en/terms>, <https://open-meteo.com/en/pricing>,
<https://open-meteo.com/en/licence>, <https://open-meteo.com/en/docs>,
<https://open-meteo.com/en/docs/ensemble-api> (all READ, 2026-11-01).

| Item | Fact |
|---|---|
| Access method | HTTPS JSON API. Forecast `https://api.open-meteo.com/v1/forecast`; ensemble `https://ensemble-api.open-meteo.com/v1/ensemble`. Commercial users use a `customer-` prefixed host and an API key. No key for the free tier. |
| Free tier | "Non-commercial purposes" only. Limits: 600 calls/minute, 5,000/hour, 10,000/day, 300,000/month, "applied by IP address and application". Cloud hosts with shared IPs may be limited sooner. |
| What the pages call non-commercial | Private or non-profit sites or apps without subscriptions or ads, public research at public institutions, educational content. Commercial examples: apps with subscriptions or ads, commercial products or promotions, undisclosed research at commercial entities. A university team with sponsors is not obviously on either list; **ask Open-Meteo** (info@open-meteo.com) and keep the answer. |
| Paid tiers | Standard (1 M calls/month), Professional (5 M), Enterprise (50 M+); minute/hour/day limits removed. Prices are **not stated** on the pages read ("fixed monthly price"). |
| Plan restrictions to check | The pricing page says the historical, climate, ensemble and satellite-radiation APIs "require the Professional plan or higher". The terms page and the ensemble docs page do not say this. The two statements conflict or concern different usage classes; **NOT VERIFIED** which applies to a free non-commercial user of the ensemble API. Test before relying on it. |
| Licence of data | CC BY 4.0. Credit appropriately, link the licence, indicate changes. Wherever Open-Meteo data is shown for a location, link to open-meteo.com (example wording: "Weather data by Open-Meteo.com"). The dashboard must therefore carry this attribution; `ProviderCapabilities.attribution` is the place. |
| Upstream licences | The licence page lists CC BY 4.0 or similar for DWD, ECMWF, BoM and several others; CC BY-SA 4.0 for UK Met Office; other terms for NOAA NCEP, CMC, Meteo-France, JMA and some Copernicus services. Upstream terms follow the model used. |
| Code licence | Open-Meteo server code: AGPL v3 (relevant only if the team ever self-hosts it). |
| Cadence | Forecast models listed on the docs page include ECMWF IFS/AIFS (9-25 km, 15 days, every 6 hours), DWD ICON, NOAA GFS and others. Ensemble page: ECMWF IFS 0.25 degree, 51 members, 15 days, every 6 hours; ECMWF AIFS 0.25 degree, 51 members; NOAA GFS ensemble 31 members; BOM ACCESS-GE 40 km, 18 members, 10 days, every 6 hours. |
| Items to watch | The docs page lists "BOM ACCESS Global (until Jun 2025)", meaning a BoM deterministic model may no longer be served, while the ensemble page still lists ACCESS-GE. Model availability changes without notice; the provider adapter must read availability at run time, never assume. |
| Radiation convention | Radiation variables are **backward-averaged over the preceding hour**; `*_instant` variants give the value at the timestamp. This is a data-handling fact the provider adapter must respect when aligning with solar geometry (details in `docs/science/providers.md`). |
| Date range | `forecast_days` 0-16, `past_days` 0-92. |
| Commercial terms | The service reserves the right to block misuse without notice. |

Consequence for the project: phase 1 may use the free tier only if the team is confirmed as
non-commercial; during the race the call budget (10,000/day) is ample for route sampling but the
key risk is the licence classification, not the volume. See ADR 0004 for how the adapter isolates it.

## 3. ECMWF open data

Page read: <https://www.ecmwf.int/en/forecasts/datasets/open-data> (READ, 2026-11-01).
The task's reference URL `.../open-data-0` was not used; this is the current page.

| Item | Fact |
|---|---|
| Licence | CC BY 4.0 with ECMWF terms of use; redistribution and commercial use allowed with attribution. |
| Cost | Free for the open subset. Direct delivery or additional data may carry service charges under a Service Agreement (no prices on the page). |
| Content | IFS (atmosphere and wave; control and perturbed forecasts) and AIFS Single and AIFS ENS. |
| Resolution | IFS 0.25 degree; from 20 October 2026 also 0.1 degree (about 9 km), published about two hours after the 0.25 degree product, surface and pressure levels in separate files. AIFS 0.25 degree only. |
| Cadence | Four runs per day (00, 06, 12, 18 UTC). Rolling archive of the most recent 12 runs (about 2-3 days). |
| Access | Open-data portal (data.ecmwf.int), mirrors on AWS, Azure and Google Cloud, and the `ecmwf-opendata` Python client. GRIB2 format. |
| Latency | Not stated numerically. **NOT VERIFIED**; measure it. |

Consequence: reading GRIB2 directly needs extra dependencies (`cfgrib`/`eccodes`, `xarray`) that
are not in `pyproject.toml`; this is why Open-Meteo is the phase-1 route to ECMWF data (ADR 0004).

## 4. Australian Bureau of Meteorology (BoM)

The pages `bom.gov.au/resources/data-services`, `/other/copyright.shtml`, `/other/charges.shtml`,
`/catalogue/data-feeds.shtml` and the anonymous-FTP guide PDF all returned **HTTP 403** to
automated fetches on 2026-11-01. Everything below is from web-search extracts of official BoM
pages and is therefore **SNIPPET** level. A team member must read the pages in a browser before
any decision.

| Item | Fact (SNIPPET) |
|---|---|
| Access tiers | The BoM data catalogue describes unregistered free access, registered access (free, registration required) and charged access (registration required). |
| Anonymous FTP | Free; products on it are described as "not for commercial use" and fall under the default BoM copyright notice (personal use or use within your own organisation; not supplying to others or commercial use). Users who intend to **publish** BoM data should be Registered Users. |
| Registered User Services fee | Extract of the charges page: one-time establishment fee AUD 1,271 and annual FTP fee AUD 1,220 for 2025/26, under cost-recovery. Figures may have changed. |
| Licence agreements | A Data Licence Agreement exists (a 2025 edition was found; titles suggesting 2026 and October 2026 editions appeared in results but could not be read). "Commercial Benefit" is defined there as receipt of revenue or credit by the licensee from use of the data, excluding a research grant. Where used commercially, data may be passed to third parties only as part of a licensee product. |
| ACCESS NWP | The APS3 NetCDF notes list the licence for ACCESS-Global forecast files as the Bureau Express Licence; an Express Licence Agreement allows supply to third parties only as part of a user product, with acknowledgement of the Bureau. |
| Other | BARRA reanalysis is CC BY 4.0 via NCI (a separate licence). Open-Meteo's licence page lists BoM as an upstream source under CC BY or similar terms. |
| Real-time observation feeds | Whether the public observation JSON/XML pages may be used by software is **NOT VERIFIED**. The repository's `bom_ftp_base` setting points at `http://www.bom.gov.au/fwo`; no adapter should use it until a human has confirmed the terms. |

Consequence: BoM observations (the only real "observation" source for the route) are the highest
value and the least clear licence. The question for the team: apply for a Registered User Service or
data licence now, in the team's name, stating the purpose (university competition, non-commercial,
sponsor-supported), and keep the BoM-via-Open-Meteo path as the fallback.

## 5. Himawari-8/9 satellite data

Pages read: JMA MSC AHI specification
<https://www.data.jma.go.jp/mscweb/en/himawari89/space_segment/spsg_ahi.html> and the NOAA
Registry of Open Data entry <https://registry.opendata.aws/noaa-himawari/> (both READ, 2026-11-01).

| Item | Fact |
|---|---|
| Instrument | Advanced Himawari Imager, 16 bands, nominal resolution at the sub-satellite point 0.5 km (band 3), 1 km (bands 1, 2, 4), 2 km (bands 5-16). |
| Cadence | Full disk every 10 minutes; Japan area scans every 2.5 minutes. The AWS entry additionally lists regions 4-5 every 30 seconds (regions near Japan, not Australia). |
| Spacecraft | Himawari-9 at 140.7 E operational, Himawari-8 as backup. |
| Access | Anonymous S3 buckets `noaa-himawari8` and `noaa-himawari9` (us-east-1), no AWS account needed. Archive from July 2015. |
| Cost | Free. |
| Licence / terms | JMA produces the data; NOAA distributes it openly; attribution requested; you must not imply NOAA or JMA endorsement; modified data must not be described as original. A formal licence text was **not** on the page read. |
| Latency | **NOT VERIFIED** (not stated). Must be measured before any nowcasting claim. |
| Practical note | Raw files are large (Himawari Standard Data / NetCDF). Phase 5 will need cropping to the Australian corridor; this is an engineering item, not a licensing one. |

## 6. Not covered and why

* NOAA GFS/HRRR direct, UK Met Office, Meteo-France, DWD direct feeds: not needed for phase 1; reachable via Open-Meteo.
* Local weather stations and vehicle-mounted sensors: team-owned, no licence question; see regulation question Q9 in `bwsc-2027-regulations.md`.
* Solar radiation datasets (satellite-derived, e.g. a national solar-exposure product): not checked; add when a
  validation source for irradiance is chosen.
* pvlib clear-sky and solar-position libraries: open-source library, not a data licence question (ADR 0003).

## 7. Decisions needed from the team

1. Confirm with Open-Meteo whether the team's use is non-commercial, or budget a paid plan.
2. Start the BoM registration/licence process now; it may take weeks.
3. Decide who holds accounts (team email, not a personal address) and where API keys live (`.env`, never in git).
4. Agree attribution text shown on the dashboard for each source.
