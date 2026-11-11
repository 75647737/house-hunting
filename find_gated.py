#!/usr/bin/env python3
"""
Find likely gated communities in an area using OpenStreetMap data,
and build your own map of them (plus places you have seen yourself).

Outputs (in ./output):
  gated_map.html               - open in a browser
  gated_communities.geojson    - shapes, for QGIS / Google My Maps / geojson.io
  gated_communities.csv        - name, score, lat, lon, reasons

Examples:
  python find_gated.py                          # Ruaka (default area)
  python find_gated.py --place "Kitengela, Kenya"
  python find_gated.py --bbox -1.225 36.745 -1.180 36.800
  python find_gated.py --google                 # also ask Google Places (needs API key)
"""
import argparse
import csv
import json
import os
import re
import sys
import time

import folium
import requests
from folium.plugins import LocateControl, MarkerCluster
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

# Ruaka and its edges (Banana Hill, Ndenderu, Two Rivers, Gachie): south, west, north, east
RUAKA_BBOX = (-1.225, 36.745, -1.180, 36.800)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "gated-community-mapper/1.0 (personal house-hunting script)"

# Words that often appear in Kenyan estate names
ESTATE_WORDS = re.compile(
    r"\b(estate|gardens?|court|villas?|park|residence[s]?|homes|ridge|heights|"
    r"springs|meadows|groove|grove|valley|apartments|place|close|view)\b",
    re.I,
)

# Around 15 m at the equator. Gates are often mapped just outside the boundary.
GATE_TOLERANCE_DEG = 0.00015


# --------------------------------------------------------------------------- data

def geocode(place):
    """Turn a place name into a bounding box using OpenStreetMap Nominatim."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    r.raise_for_status()
    hits = r.json()
    if not hits:
        sys.exit(f"Could not find '{place}'. Try adding the county or ', Kenya'.")
    s, n, w, e = map(float, hits[0]["boundingbox"])
    # Pad small places a little so the edges are included
    pad = 0.01
    return (s - pad, w - pad, n + pad, e + pad)


def overpass_query(bbox):
    s, w, n, e = bbox
    b = f"({s},{w},{n},{e})"
    return f"""
[out:json][timeout:120];
(
  way["landuse"="residential"]{b};
  relation["landuse"="residential"]{b};
  way["residential"]{b};
  way["place"~"neighbourhood|quarter"]{b};
  way["gated"="yes"]{b};
  way["building"="apartments"]["name"]{b};
  node["barrier"~"gate|lift_gate|sliding_gate|swing_gate|entrance"]{b};
  node["entrance"]{b};
  way["barrier"~"wall|fence"]{b};
);
out geom tags;
"""


def fetch_osm(bbox):
    q = overpass_query(bbox)
    last_err = None
    for url in OVERPASS_URLS:
        for attempt in range(3):
            try:
                print(f"Asking OpenStreetMap ({url}) ...")
                r = requests.post(url, data={"data": q},
                                  headers={"User-Agent": USER_AGENT}, timeout=180)
                if r.status_code in (429, 504):
                    raise requests.HTTPError(f"server busy ({r.status_code})")
                r.raise_for_status()
                return r.json()
            except Exception as err:  # network errors, busy server
                last_err = err
                wait = 5 * (attempt + 1)
                print(f"  failed: {err}. Retrying in {wait}s")
                time.sleep(wait)
    sys.exit(f"Could not reach OpenStreetMap: {last_err}")


def google_places(bbox, api_key, queries=None):
    """Optional: search Google Places (New) for estates. Returns point features."""
    s, w, n, e = bbox
    queries = queries or ["gated community", "estate", "townhouses", "apartments"]
    out = []
    for q in queries:
        body = {
            "textQuery": q,
            "locationRestriction": {"rectangle": {
                "low": {"latitude": s, "longitude": w},
                "high": {"latitude": n, "longitude": e},
            }},
            "pageSize": 20,
        }
        r = requests.post(
            "https://places.googleapis.com/v1/places:searchText",
            json=body,
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.location,"
                                    "places.formattedAddress,places.types",
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  Google search '{q}' failed: {r.status_code} {r.text[:200]}")
            continue
        for p in r.json().get("places", []):
            out.append({
                "id": p["id"],
                "name": p.get("displayName", {}).get("text", "?"),
                "lat": p["location"]["latitude"],
                "lon": p["location"]["longitude"],
                "address": p.get("formattedAddress", ""),
                "query": q,
            })
    # remove duplicates
    return list({p["id"]: p for p in out}.values())


# ------------------------------------------------------------------ geometry

def way_to_shape(el):
    pts = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
    if len(pts) < 2:
        return None
    if len(pts) >= 4 and pts[0] == pts[-1]:
        poly = Polygon(pts)
        return poly if poly.is_valid else poly.buffer(0)
    return LineString(pts)


def relation_to_polygon(el):
    lines = []
    for m in el.get("members", []):
        if m.get("role") in ("outer", "") and m.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
            if len(pts) >= 2:
                lines.append(LineString(pts))
    if not lines:
        return None
    polys = list(polygonize(linemerge(lines)))
    return unary_union(polys) if polys else None


def parse(osm):
    areas, gates, walls = [], [], []
    for el in osm.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "node":
            gates.append({"pt": Point(el["lon"], el["lat"]), "tags": tags, "id": el["id"]})
            continue
        shape = way_to_shape(el) if el["type"] == "way" else relation_to_polygon(el)
        if shape is None or shape.is_empty:
            continue
        if tags.get("barrier") in ("wall", "fence"):
            walls.append(shape if shape.geom_type == "LineString" else shape.boundary)
        elif shape.geom_type in ("Polygon", "MultiPolygon"):
            areas.append({"shape": shape, "tags": tags, "id": f"{el['type']}/{el['id']}"})
    return areas, gates, walls


# -------------------------------------------------------------------- scoring

def score_area(area, gates, walls_union):
    tags, shape = area["tags"], area["shape"]
    score, reasons = 0, []

    if tags.get("residential") == "gated" or tags.get("gated") == "yes":
        score += 100
        reasons.append("tagged as gated")
    if tags.get("access") in ("private", "no", "customers", "permit", "destination"):
        score += 40
        reasons.append(f"access={tags['access']}")

    zone = shape.buffer(GATE_TOLERANCE_DEG)
    n_gates = sum(1 for g in gates if zone.contains(g["pt"]))
    if n_gates:
        score += 30 + 10 * min(n_gates - 1, 2)
        reasons.append(f"{n_gates} gate(s)")

    if walls_union is not None:
        edge = shape.boundary
        covered = edge.intersection(walls_union.buffer(GATE_TOLERANCE_DEG)).length
        share = covered / edge.length if edge.length else 0
        if share > 0.3:
            score += 25
            reasons.append(f"wall/fence along {share:.0%} of edge")
        elif share > 0.1:
            score += 10
            reasons.append(f"some wall/fence ({share:.0%})")

    name = tags.get("name", "")
    if name:
        score += 10
        reasons.append("named")
        if ESTATE_WORDS.search(name):
            score += 10
            reasons.append("estate-style name")

    # Very large polygons are usually whole neighbourhoods, not one compound
    area_m2 = shape.area * (111_320 ** 2)
    if area_m2 > 600_000:
        score -= 30
        reasons.append("very large (probably a whole area)")

    return min(score, 100), reasons, n_gates, area_m2


def tier(score):
    if score >= 60:
        return "likely"
    if score >= 30:
        return "possible"
    return "unlikely"


# ---------------------------------------------------------------- your sightings

def load_sightings(path):
    """CSV with columns: name,lat,lon,notes,date_seen  (notes/date optional)."""
    if not path or not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({**row, "lat": float(row["lat"]), "lon": float(row["lon"])})
            except (KeyError, ValueError):
                print(f"  skipping bad sightings row: {row}")
    return rows


# ---------------------------------------------------------------------- output

COLORS = {"likely": "#1a9850", "possible": "#fdae61", "unlikely": "#bbbbbb"}


def build_map(bbox, results, loose_gates, sightings, google_hits, out_html):
    s, w, n, e = bbox
    m = folium.Map(location=[(s + n) / 2, (w + e) / 2], zoom_start=15, tiles=None)
    folium.TileLayer("OpenStreetMap", name="Street map").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery", name="Satellite",
    ).add_to(m)

    layers = {t: folium.FeatureGroup(name=f"{t.title()} gated", show=(t != "unlikely"))
              for t in ("likely", "possible", "unlikely")}
    for r in results:
        color = COLORS[r["tier"]]
        popup = (f"<b>{r['name'] or 'Unnamed estate'}</b><br>"
                 f"Score: {r['score']}/100 ({r['tier']})<br>"
                 f"{'; '.join(r['reasons'])}<br>"
                 f"Size: {r['area_m2']/10_000:.1f} ha<br>"
                 f"<a href='https://www.google.com/maps/search/?api=1&query={r['lat']:.6f},{r['lon']:.6f}' "
                 f"target='_blank'>Open in Google Maps</a> · "
                 f"<a href='https://www.openstreetmap.org/{r['osm_id']}' target='_blank'>OSM</a>")
        folium.GeoJson(
            mapping(r["shape"]),
            style_function=lambda _, c=color: {"color": c, "fillColor": c,
                                               "weight": 2, "fillOpacity": 0.35},
            tooltip=r["name"] or "Unnamed estate",
            popup=folium.Popup(popup, max_width=320),
        ).add_to(layers[r["tier"]])
    for g in layers.values():
        g.add_to(m)

    gate_layer = folium.FeatureGroup(name="Gates not inside an estate", show=False)
    for g in loose_gates:
        folium.CircleMarker([g["pt"].y, g["pt"].x], radius=3, color="#d73027",
                            tooltip=g["tags"].get("name", g["tags"].get("barrier", "gate"))
                            ).add_to(gate_layer)
    gate_layer.add_to(m)

    if google_hits:
        gl = MarkerCluster(name="Google Places results")
        for p in google_hits:
            folium.Marker([p["lat"], p["lon"]], tooltip=p["name"],
                          popup=f"<b>{p['name']}</b><br>{p['address']}<br>search: {p['query']}",
                          icon=folium.Icon(color="blue", icon="home")).add_to(gl)
        gl.add_to(m)

    if sightings:
        sl = folium.FeatureGroup(name="Places I have seen")
        for sgt in sightings:
            folium.Marker(
                [sgt["lat"], sgt["lon"]], tooltip=sgt.get("name", "Seen"),
                popup=(f"<b>{sgt.get('name','')}</b><br>{sgt.get('notes','')}"
                       f"<br>{sgt.get('date_seen','')}"),
                icon=folium.Icon(color="purple", icon="star"),
            ).add_to(sl)
        sl.add_to(m)

    folium.Rectangle([[s, w], [n, e]], color="#3388ff", fill=False, dash_array="6",
                     tooltip="Search area").add_to(m)
    LocateControl().add_to(m)
    # Click anywhere to get coordinates you can paste into my_sightings.csv
    folium.LatLngPopup().add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    legend = """
    <div style="position:fixed;bottom:20px;left:20px;z-index:9999;background:white;
         padding:8px 12px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font:13px sans-serif">
      <b>Gated communities</b><br>
      <span style="color:#1a9850">&#9632;</span> Likely&nbsp;
      <span style="color:#fdae61">&#9632;</span> Possible<br>
      <span style="color:purple">&#9733;</span> Seen by me&nbsp;
      <span style="color:#d73027">&#9679;</span> Loose gate<br>
      <i>Click the map to get coordinates</i>
    </div>"""
    m.get_root().html.add_child(folium.Element(legend))
    m.save(out_html)


def write_outputs(results, outdir):
    feats = []
    for r in results:
        feats.append({"type": "Feature", "geometry": mapping(r["shape"]),
                      "properties": {k: r[k] for k in
                                     ("name", "score", "tier", "osm_id", "lat", "lon")}
                      | {"reasons": "; ".join(r["reasons"])}})
    with open(os.path.join(outdir, "gated_communities.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, indent=1)

    with open(os.path.join(outdir, "gated_communities.csv"), "w", newline="",
              encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["name", "score", "tier", "lat", "lon", "gates", "hectares",
                     "reasons", "google_maps_link"])
        for r in results:
            wr.writerow([r["name"], r["score"], r["tier"], f"{r['lat']:.6f}", f"{r['lon']:.6f}",
                         r["gates"], f"{r['area_m2']/10_000:.2f}", "; ".join(r["reasons"]),
                         f"https://www.google.com/maps/search/?api=1&query={r['lat']:.6f},{r['lon']:.6f}"])


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Map likely gated communities from OpenStreetMap.")
    area = ap.add_mutually_exclusive_group()
    area.add_argument("--place", help="Place name, e.g. 'Ruaka, Kenya'")
    area.add_argument("--bbox", nargs=4, type=float, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ap.add_argument("--from-file", help="Use a saved Overpass JSON instead of downloading")
    ap.add_argument("--sightings", default="my_sightings.csv",
                    help="Your own CSV of places you've seen (name,lat,lon,notes,date_seen)")
    ap.add_argument("--google", action="store_true",
                    help="Also search Google Places (needs GOOGLE_MAPS_API_KEY env var)")
    ap.add_argument("--min-score", type=int, default=30,
                    help="Hide estates scoring below this (0-100, default 30)")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    bbox = tuple(args.bbox) if args.bbox else geocode(args.place) if args.place else RUAKA_BBOX
    os.makedirs(args.out, exist_ok=True)
    print(f"Search area (S, W, N, E): {bbox}")

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            osm = json.load(f)
    else:
        osm = fetch_osm(bbox)
        with open(os.path.join(args.out, "osm_raw.json"), "w", encoding="utf-8") as f:
            json.dump(osm, f)

    areas, gates, walls = parse(osm)
    walls_union = unary_union(walls) if walls else None
    print(f"Found {len(areas)} residential shapes, {len(gates)} gates, {len(walls)} walls/fences")

    results = []
    used_gates = set()
    for a in areas:
        sc, reasons, n_gates, area_m2 = score_area(a, gates, walls_union)
        if sc < args.min_score:
            continue
        zone = a["shape"].buffer(GATE_TOLERANCE_DEG)
        used_gates.update(g["id"] for g in gates if zone.contains(g["pt"]))
        c = a["shape"].representative_point()
        results.append({"shape": a["shape"], "name": a["tags"].get("name", ""),
                        "score": sc, "tier": tier(sc), "reasons": reasons,
                        "gates": n_gates, "area_m2": area_m2, "osm_id": a["id"],
                        "lat": c.y, "lon": c.x})
    results.sort(key=lambda r: -r["score"])
    loose = [g for g in gates if g["id"] not in used_gates]

    google_hits = []
    if args.google:
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            print("--google needs GOOGLE_MAPS_API_KEY set; skipping Google.")
        else:
            google_hits = google_places(bbox, key)
            print(f"Google Places returned {len(google_hits)} places")

    sightings = load_sightings(args.sightings)
    out_html = os.path.join(args.out, "gated_map.html")
    build_map(bbox, results, loose, sightings, google_hits, out_html)
    write_outputs(results, args.out)

    likely = sum(r["tier"] == "likely" for r in results)
    print(f"\n{likely} likely and {len(results) - likely} possible gated communities.")
    for r in results[:15]:
        print(f"  {r['score']:3d}  {r['name'] or '(unnamed)':35s} {'; '.join(r['reasons'])}")
    print(f"\nOpen this in your browser: {os.path.abspath(out_html)}")


if __name__ == "__main__":
    main()
