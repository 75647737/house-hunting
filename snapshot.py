#!/usr/bin/env python3
"""
Take pictures of the estates found by find_gated.py:
  <out>/snapshots/overview.png      - the whole map
  <out>/snapshots/01-<name>.png ... - satellite close-up of each estate (best first)

Usage: python snapshot.py --site site --top 10
Needs: pip install playwright && playwright install --with-deps chromium
"""
import argparse
import json
import os
import re

import folium
from playwright.sync_api import sync_playwright
from shapely.geometry import shape

ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
COLORS = {"likely": "#1a9850", "possible": "#fdae61", "unlikely": "#bbbbbb"}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unnamed"


def estate_page(feat, path):
    geom = shape(feat["geometry"])
    p = feat["properties"]
    minx, miny, maxx, maxy = geom.bounds
    pad = 0.0008  # ~90 m of surroundings
    m = folium.Map(tiles=None, zoom_control=False, attribution_control=True)
    folium.TileLayer(ESRI, attr="Esri World Imagery", max_zoom=19).add_to(m)
    color = COLORS.get(p.get("tier"), "#ffffff")
    folium.GeoJson(feat["geometry"], style_function=lambda _: {
        "color": color, "weight": 4, "fillOpacity": 0.08}).add_to(m)
    m.fit_bounds([[miny - pad, minx - pad], [maxy + pad, maxx + pad]])
    label = (f"<div style='position:fixed;top:10px;left:10px;z-index:9999;background:rgba(0,0,0,.7);"
             f"color:#fff;padding:8px 12px;border-radius:6px;font:14px sans-serif'>"
             f"<b>{p.get('name') or 'Unnamed estate'}</b><br>Score {p['score']}/100 ({p['tier']})"
             f"<br><span style='font-size:12px'>{p.get('reasons','')}</span></div>")
    m.get_root().html.add_child(folium.Element(label))
    m.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    outdir = os.path.join(args.site, "snapshots")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(args.site, "gated_communities.geojson"), encoding="utf-8") as f:
        feats = json.load(f)["features"][: args.top]

    jobs = [(os.path.abspath(os.path.join(args.site, "gated_map.html")),
             os.path.join(outdir, "overview.png"))]
    for i, feat in enumerate(feats, 1):
        html = os.path.join(outdir, f"{i:02d}.html")
        estate_page(feat, html)
        name = slug(feat["properties"].get("name") or "unnamed")
        jobs.append((os.path.abspath(html), os.path.join(outdir, f"{i:02d}-{name}.png")))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 750})
        for html, png in jobs:
            page.goto(f"file://{html}")
            page.wait_for_load_state("networkidle", timeout=60_000)
            page.wait_for_timeout(1500)  # let map tiles finish drawing
            page.screenshot(path=png)
            print("saved", png)
        browser.close()

    for i in range(1, len(feats) + 1):  # keep only the pictures
        os.remove(os.path.join(outdir, f"{i:02d}.html"))


if __name__ == "__main__":
    main()
