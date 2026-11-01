"""Rebuild bwsc-2027-stuart-highway.geojson from OpenStreetMap extracts.

This is a *reproducibility record*, not part of the runtime package. It documents
exactly how the committed GeoJSON was produced on 2026-11-01.

Inputs (Overpass API, https://overpass-api.de/api/interpreter, ODbL data), saved as
JSON in one directory passed as argv[1]:

  rel_2668551.json  [out:json];relation(2668551);out geom;   # "Stuart Highway" NT (Darwin -> NT/SA border)
  rel_198398.json   [out:json];relation(198398);out geom;    # "Stuart Highway" A87 SA (border -> Port Augusta)
  south.json   way[highway~"^(motorway|trunk|primary)$"]
                  [name~"Augusta Highway|Port Wakefield Road|Port Wakefield Highway|Stuart Highway"]
                  (-34.9,137.6,-32.4,138.8);                 # Port Augusta -> Port Wakefield -> Adelaide north
  adel.json    way[highway~"^(motorway|trunk|primary|secondary)$"](-34.96,138.45,-34.75,138.7);
  gap.json     way[highway~"^(trunk)$"] (-32.54,137.70,-32.44,137.82) and (-33.97,138.10,-33.88,138.30)
               # closes two small gaps in the relation/name-based extracts (Port Augusta, Snowtown/Frances Tce)

Method: build an undirected graph from all ways (node identity = coordinate rounded to 1e-7 deg),
run Dijkstra (geodesic edge lengths) from the node nearest State Square, Darwin to the node nearest
Victoria Park, Adelaide, then Douglas-Peucker simplify (8 m tolerance, local equirectangular metres).

Honest limits (also written into the GeoJSON ``rwi_provenance``): the path is the SHORTEST path
through OSM trunk/primary ways, not the official race route; one-way and dual-carriageway
direction is ignored; the Darwin and Adelaide CBD portions are approximations; no elevation.
"""

from __future__ import annotations

import heapq
import json
import math
import sys
from pathlib import Path

import numpy as np
from pyproj import Geod

GEOD = Geod(ellps="WGS84")

# State Square, Darwin: official start, from the 2025 BWSC Route Notes p.7 (S12.46306 E130.83781).
START = (-12.46306, 130.83781, "State Square, Darwin")
# Victoria Park / Pakapakanthi (Park 16), centre of OSM way 81798589 (finish staging area per the notes).
FINISH = (-34.933881, 138.6197882, "Victoria Park, Adelaide")

# (number, name, official 2025 km, reference lat, reference lon, reference source)
STOPS = [
    (1, "Katherine", 322.0, -14.4646157, 132.2635993, "OSM node 32621006 (place=town)"),
    (2, "Dunmarra", 631.0, -16.6801673, 133.4130039, "OSM node 1811172691 (Dunmarra Wayside Inn)"),
    (3, "Tennant Creek", 988.0, -19.6481652, 134.1899847, "OSM node 1852930060 (place=town)"),
    (4, "Barrow Creek", 1212.3, -21.5313252, 133.889358, "OSM node 2353455946 (Barrow Creek Hotel)"),
    (5, "Alice Springs", 1496.0, -23.6983884, 133.8812885, "OSM node 29341070 (place=city)"),
    (6, "Erldunda", 1694.0, -25.1976725, 133.2007846, "OSM node 778143080 (Erldunda Roadhouse)"),
    (7, "Coober Pedy", 2181.0, -29.0133682, 134.7536164, "OSM node 29383350 (place=town)"),
    (8, "Glendambo", 2434.0, -30.9694281, 135.7505901, "OSM node 1674725160 (Glendambo Hotel)"),
    (9, "Port Augusta", 2720.5, -32.4908819, 137.7639874, "OSM node 29386608 (place=town)"),
]


def key(p: dict) -> tuple[float, float]:
    return (round(p["lat"], 7), round(p["lon"], 7))


def build_graph(raw: Path) -> dict:
    adj: dict = {}

    def add(geom: list[dict]) -> None:
        ks = [key(p) for p in geom]
        for a, b in zip(ks, ks[1:], strict=False):
            if a == b:
                continue
            d = GEOD.inv(a[1], a[0], b[1], b[0])[2]
            adj.setdefault(a, {})[b] = min(d, adj.get(a, {}).get(b, 1e18))
            adj.setdefault(b, {})[a] = adj[a][b]

    for f in ("rel_2668551.json", "rel_198398.json"):
        for m in json.load(open(raw / f))["elements"][0]["members"]:
            if m["type"] == "way":
                add(m["geometry"])
    for f in ("south.json", "adel.json", "gap.json"):
        for e in json.load(open(raw / f))["elements"]:
            if f == "gap.json" and e["tags"].get("highway") != "trunk":
                continue
            if e["type"] == "way":
                add(e["geometry"])
    return adj


def nearest(adj: dict, lat: float, lon: float) -> tuple:
    return min(adj, key=lambda k: (k[0] - lat) ** 2 + ((k[1] - lon) * 0.9) ** 2)


def dijkstra(adj: dict, src: tuple, dst: tuple) -> list[tuple]:
    dist = {src: 0.0}
    prev: dict = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18):
            continue
        if u == dst:
            break
        for v, w in adj[u].items():
            if d + w < dist.get(v, 1e18):
                dist[v] = d + w
                prev[v] = u
                heapq.heappush(pq, (d + w, v))
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1]


def douglas_peucker(pts: np.ndarray, tol: float) -> np.ndarray:
    """Iterative DP on an (n, 2) array of planar metres; returns kept indices."""
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        ab = b - a
        seg = pts[i + 1 : j] - a
        norm = float(np.hypot(*ab))
        d = np.hypot(*(seg.T - 0)) if norm == 0 else np.abs(ab[0] * seg[:, 1] - ab[1] * seg[:, 0]) / norm
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return np.flatnonzero(keep)


def main(raw_dir: str, out_path: str) -> None:
    raw = Path(raw_dir)
    adj = build_graph(raw)
    finish = FINISH
    src = nearest(adj, START[0], START[1])
    dst = nearest(adj, finish[0], finish[1])
    path = [(START[0], START[1])] + dijkstra(adj, src, dst) + [(finish[0], finish[1])]
    xy = np.array([(lon * math.cos(math.radians(24.0)) * 111_320.0, lat * 110_574.0) for lat, lon in path])
    idx = douglas_peucker(xy, 8.0)
    simp = [path[i] for i in idx]
    lons = np.array([p[1] for p in simp])
    lats = np.array([p[0] for p in simp])
    seg = GEOD.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])[2]
    total_m = float(np.sum(seg))
    print(f"graph path points {len(path)} -> simplified {len(simp)}; length {total_m / 1000:.2f} km")

    coords = [[round(p[1], 6), round(p[0], 6)] for p in simp]
    features = [
        {
            "type": "Feature",
            "properties": {"role": "route_line", "name": "Darwin to Adelaide via Stuart Highway (OSM shortest path)"},
            "geometry": {"type": "LineString", "coordinates": coords},
        },
        {
            "type": "Feature",
            "properties": {"rwi_kind": "start", "name": START[2], "reference_source": "2025 BWSC Route Notes p.7 (S12.46306 E130.83781)"},
            "geometry": {"type": "Point", "coordinates": [START[1], START[0]]},
        },
        {
            "type": "Feature",
            "properties": {"rwi_kind": "finish", "name": FINISH[2], "official_km_2025": 3026.92,
                           "reference_source": "OSM way 81798589 centre (Victoria Park / Pakapakanthi)"},
            "geometry": {"type": "Point", "coordinates": [finish[1], finish[0]]},
        },
    ]
    for num, name, km, lat, lon, src_txt in STOPS:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "rwi_kind": "control_stop",
                    "name": f"{name} Control Stop",
                    "control_stop_number": num,
                    "official_km_2025": km,
                    "reference_source": src_txt + "; a town/roadhouse reference point, NOT the surveyed control-stop entrance",
                },
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )
    fc = {
        "type": "FeatureCollection",
        "rwi_provenance": {
            "provider": "OpenStreetMap contributors (via Overpass API), processed by JST-RWI",
            "dataset": "OSM Stuart Highway relations 2668551 (NT) and 198398 (SA A87), Augusta Highway / Port Wakefield Road / Main North Road ways, shortest-path chained and simplified",
            "license": "ODbL-1.0 (Open Database Licence); attribution required: (c) OpenStreetMap contributors",
            "source_url": "https://www.openstreetmap.org/copyright",
            "retrieved_at_utc": "2026-11-01T22:30:00Z",
            "osm_base_timestamp": "2026-11-01T22:24:36Z",
            "is_official_race_route": False,
            "accuracy": "OSM road centreline, typically metres (not independently validated). Simplified with Douglas-Peucker 8 m, so the polyline deviates from the OSM centreline by at most ~8 m. Horizontal only; no elevation.",
            "description": (
                "Geometry is the OpenStreetMap road network, NOT the official Bridgestone World Solar Challenge route. "
                "It is the shortest path through OSM trunk/primary ways from State Square, Darwin to Victoria Park, Adelaide. "
                "The first ~0.9 km (State Square to the Stuart Highway) and the final ~0.2 km (to Victoria Park centre) are straight-line links; "
                "the urban approach to Adelaide (about the last 10 km) follows Main North Road / Hackney Road / Hutt Street rather than the official Robe Terrace ring route. "
                "Control stop positions are town/roadhouse reference points snapped to the polyline; the official control-stop distances (2025 Route Notes V1.0, published 13 July 2025) "
                "are carried in each stop's notes. The 2027 route notes are not yet published."
            ),
            "notes": f"simplified vertex count {len(simp)}; polyline length {total_m / 1000:.2f} km vs official 3026.92 km (2025 chart)",
        },
        "features": features,
    }
    Path(out_path).write_text(json.dumps(fc, separators=(",", ":")))
    print("wrote", out_path, Path(out_path).stat().st_size, "bytes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
