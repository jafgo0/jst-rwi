# Checklista: JST-RWI meteorolog-sida

Statusbild 2026-11-07. Scope = det meteorolog-lagret äger (väder, sol,
satellit, validering). Strategi / batteri / telemetri ligger utanför.
Grund: `docs/REQUIREMENTS.md` + det här sessionsflödet.

## Klart och fungerande

- [x] **Dashboard i webbläsare** — alla vyer (Command, Solar, Forecast, Route, Strategy, Alerts, History)
- [x] **Live-väderprognos** via Open-Meteo (ECMWF-IFS reanalys + prognos)
- [x] **Observationer** via BoM-stationer (72 h bakåt)
- [x] **Solgeometri** — sunrise, solar noon, sunset, elevation/azimuth
- [x] **Clear-sky-modell** (Ineichen-Perez via pvlib + Linke-klimatologi)
- [x] **GHI/DNI/DHI-dekomposition** för modellerad array-effekt
- [x] **Himawari-9 råtiles** egen pipeline för live-nowcasting (0.5 km, 10 min)
- [x] **SatCORPS Himawari-9 SH V1.4** (NASA Earthdata) — moln-fas, optisk tjocklek, topp-höjd, albedo
- [x] **SatCORPS dashboard-panel** på Solar-vyn med cross-check
- [x] **Strategi-rekommendationer** (konservativ/balanserad/snabbare/väderanpassad)
- [x] **Alerts-regler** (vind, regn, gust)
- [x] **Rutevalidering** (Darwin → Adelaide via Stuart Highway, 2025 control stops)
- [x] **Multi-model-jämförelse** (rwi.fusion, 51 tester)
- [x] **Bias-korrigering mekaniskt ramverk** (rwi.verification, 18 tester)
- [x] **Forecast-verifiering** (70 tester inkl. circular scoring)
- [x] **SQLite-arkiv** för prognos/observation-par (ADR 0007, 44 tester)
- [x] **Automatisk capture-schemaläggning** (rwi.capture, 15 tester)

## Nyligen klart denna session

- [x] **NASA Earthdata-katalog** (`docs/data-sources/nasa-earthdata-catalog.md`) — 5 produkter verifierade
- [x] **MERRA-2 M2T1NXRAD SWGDN** som fjärde oberoende sol-referens
- [x] **Kvadrangulering mot BoM-SILO** för 6 ruttpunkter, race-vecka 2025
- [x] **Aerosol-hypotes testad och avfärdad** (MERRA-2 M2T1NXAER vid Alice Springs)
- [x] **GHI-bias-chip** på Solar-vyn (tänds bara när 3 modeller håller med och median ≥15 W/m²)

## Kvar att göra — meteorolog-scope

### Enkla vinster (1–4 h var)

- [ ] **GPM IMERG precipitation** — vara-varning race-dag; sällsynt händelse i torrtid men hög konsekvens. 4 h-latens live-användbar.
- [ ] **CERES SYN1deg 1-Hour** — oberoende yta-flödes-referens (nyare Edition 4B än vad POWER serverar)
- [ ] **MODIS MAIAC AOD** — 1 km damm-signal för clear-sky Linke-override (men MERRA-2 AER-pathen är redan verifierad)
- [ ] **ARG-registry: lista källor i About-panel** så dashboarden syns sina egna provenance-sidor

### Medelstora (dag var)

- [ ] **Konvektionsvarning** (#13) — flagga baserad på CAPE/weather code + rörlig satellit-cell för eftermiddagsceller
- [ ] **Sensor-kalibrering** (#34) — drift-detektering mot clear-sky-bound för BoM-stationer
- [ ] **Historisk ruttanalys** (#37, #128) — archive-query per ruttpunkt, labelled historical
- [ ] **AIFS som fjärde prognoskälla** (#70) — jämför med ECMWF, inga påståenden före bevis

### Stora / blockerade

- [ ] **BoM Alice Springs 015540 1-min station** — **blockerad**: BoM:s 1-min-produkt går bara till dec 2020, ingen 2025-data finns
- [ ] **ARPANSA 1-min markmätning** — alternativ grundsanning för Alice Springs; registrering krävs
- [ ] **Live forecast capture en säsong** — infrastruktur finns, behöver tid (en varm månad räcker)
- [ ] **Live-GPS** (#29, #33, #79) — kräver att teamets telemetri-protokoll är spikat
- [ ] **AI-assistent-lager** (Phase 5) — läser strukturerad output, kommer sist

## Oförklarade signaler (vårt eget eftersläpning)

- [ ] **Alice Springs +23 W/m² konsensus-bias mot BoM-SILO** — inte aerosol (bevisat), återstår: SILO-interpoleringsfel eller delad yt-process-bias. Chip finns nu på dashboard.
- [ ] **Darwin: 3/4 produkter inom ±10, SILO är avvikaren** — värt att dokumentera som "SILO är inte alltid sann" när produktvalet görs
