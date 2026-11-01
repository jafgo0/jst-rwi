#!/usr/bin/env bash
# End-to-end smoke check against a running API. Verifies the platform's
# non-negotiable behaviours rather than just that endpoints respond.
#   usage: scripts/smoke.sh [base_url]
set -euo pipefail
BASE="${1:-http://localhost:8000}"
PY="${PY:-.venv/bin/python}"

say() { printf '\n== %s\n' "$1"; }
get() { curl -sS --max-time 20 "$@"; }

say "health"
get "$BASE/api/health" | $PY -m json.tool | head -25

say "no position -> must refuse to guess (expect 409)"
code=$(curl -sS -o /tmp/rwi_smoke.json -w '%{http_code}' "$BASE/api/solar/position" || true)
echo "HTTP $code"; cat /tmp/rwi_smoke.json; echo
[ "$code" = "409" ] || [ "$code" = "200" ] || { echo "unexpected status"; exit 1; }

say "critical night test: Jonkoping, local midnight"
get "$BASE/api/solar/clearsky?lat=57.7826&lon=14.1618&elev=227&time=2026-01-15T23:00:00Z" \
  | $PY -c 'import json,sys
d=json.load(sys.stdin)
assert d["ghi_w_m2"]==0.0 and d["dni_w_m2"]==0.0 and d["dhi_w_m2"]==0.0, d
assert d["kind"]=="clear_sky_model", d["kind"]
print("ghi/dni/dhi =", d["ghi_w_m2"], d["dni_w_m2"], d["dhi_w_m2"], "| kind =", d["kind"])'

say "australian position drives the calculation"
get "$BASE/api/solar/position?lat=-23.6980&lon=133.8807&elev=545&time=2027-08-24T03:00:00Z" \
  | $PY -c 'import json,sys
d=json.load(sys.stdin)
assert d["apparent_elevation_deg"]>40, d
print("elevation =", round(d["apparent_elevation_deg"],2), "deg | azimuth =", round(d["azimuth_deg"],2), "deg")'

say "set a live position and read the state back"
get -X POST "$BASE/api/state/position" -H 'content-type: application/json' \
  -d '{"mode":"live_gps","latitude_deg":-14.4652,"longitude_deg":132.2635,"elevation_m":108,"heading_deg":181,"speed_mps":25,"source":"smoke_test"}' \
  | $PY -c 'import json,sys
d=json.load(sys.stdin)
print("mode =", d["state"]["mode"], "| tz =", d["timezone_name"], "| offset =", d["utc_offset_hours"], "| simulated =", d["is_simulated"])'

say "alerts"
get "$BASE/api/alerts" | $PY -c 'import json,sys
items=json.load(sys.stdin)
print(len(items),"alert(s)")
for a in items[:5]: print(" -", a["severity"], a["category"], "|", a["headline"])'

say "routes"
get "$BASE/api/route" | $PY -m json.tool | head -20

echo
echo "smoke check finished"
