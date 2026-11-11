# Gated community mapper

Finds likely gated communities in an area using OpenStreetMap, and makes your own
map of them, together with places you have seen yourself.

## Setup (once)

    pip install -r requirements.txt

## Run

    python find_gated.py                            # Ruaka (default)
    python find_gated.py --place "Kitengela, Kenya"
    python find_gated.py --bbox -1.225 36.745 -1.180 36.800
    python find_gated.py --min-score 60             # only the most likely ones

Then open `output/gated_map.html` in your browser.

## How an estate is scored (0-100)

| Clue in the map data                      | Points |
|-------------------------------------------|--------|
| Tagged `residential=gated` / `gated=yes`  | +100   |
| `access=private`                          | +40    |
| Gate on its edge (more gates, more points)| +30-50 |
| Wall/fence along >30% of its edge         | +25    |
| Has a name / estate-style name            | +10 / +10 |
| Bigger than 60 ha (a whole neighbourhood) | -30    |

60+ = likely, 30-59 = possible.

## Add places you have seen

Edit `my_sightings.csv` (open it in Excel or any text editor):

    name,lat,lon,notes,date_seen
    Green Court,-1.2041,36.7718,"guard house, 2 bedroom units",2026-11-12

To get the lat/lon, click the spot on your map (or long-press in Google Maps).
Run the script again, and your places show as purple stars.

## Optional: Google Places

Google has more Ruaka estates than OpenStreetMap. To include them, create a
Google Maps API key with "Places API (New)" enabled, then:

    export GOOGLE_MAPS_API_KEY=your_key      # Windows: set GOOGLE_MAPS_API_KEY=your_key
    python find_gated.py --google

Google gives a monthly free allowance; after that it charges per search.

## Outputs

- `output/gated_map.html` - interactive map (street + satellite layers)
- `output/gated_communities.csv` - list with Google Maps links
- `output/gated_communities.geojson` - shapes, can be imported into Google My Maps

## Live map on GitHub Pages

Every push to `main` (and every Monday) GitHub runs the script and publishes the map at
`https://<your-username>.github.io/<repo-name>/`.

One-time setup: repo **Settings → Pages → Source: GitHub Actions**.

To add a place you've seen: open `my_sightings.csv` on GitHub, click the pencil, add a
line, **Commit changes**. The map updates in about a minute.

Optional Google results: **Settings → Secrets and variables → Actions → New secret**
named `GOOGLE_MAPS_API_KEY`.
