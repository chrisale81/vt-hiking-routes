# Swiss Hiking Loop Planner

A Dockerized Python/Streamlit planner for **round hikes on the official swissTLM3D hiking network**.

The default GUI is set up for the requested scenario:

- **Start:** `Lagerhaus Alpina Segnas`
- **Duration:** `3 h`
- **Direction:** `Disentis/Mustér`
- **Maximum steepness:** measured against a reference route, default `Sontga Gada → Mumpé Medel`
- **Official hiking paths:** strongly preferred
- **Alpine hiking trails:** forbidden
- **Loop:** yes

The application deliberately shows the actual `geo.admin.ch` search results and makes you select the intended result. It does not silently guess which place name you meant. If a place is not in the official search index, switch that picker to manual WGS84 coordinates.

## Run with Docker Compose

```bash
docker compose up --build
```

Open:

```text
http://localhost:8501
```

The `./data` directory is mounted into the container. The swissTLM3D GeoPackage is cached there, so it does not need to be downloaded for every container restart.

## Run with plain Docker

```bash
docker build -t swiss-hiking-planner .
docker run --rm -p 8501:8501 -v "$(pwd)/data:/data" swiss-hiking-planner
```

## Run without Docker

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## How loop generation works

1. Resolve start, direction and optional steepness-reference locations using the official `geo.admin.ch` SearchServer.
2. Resolve/download the current swissTLM3D hiking-path GeoPackage from `opendata.swiss` and cache it locally.
3. Read only the local region needed for routing.
4. Interpret the swissTLM3D `WANDERWEGE` category:
   - `Wanderweg` → allowed / preferred
   - `Bergwanderweg` → allowed / preferred
   - `Alpinwanderweg` → **removed from the graph**
   - `andere` → allowed, but strongly penalized by default
5. Build a noded NetworkX routing graph from the official linework, classifying each segment by how much motor traffic to expect on it:
   - `1m Weg`, `2m Weg`, `Markierte Spur` → traffic-free
   - road width but with a driving ban, marked as not drivable, or an unpaved `Natur` surface → traffic-calmed (farm and forest tracks)
   - `3m`/`4m Strasse` open to cars → **road shared with cars**
   - `6m Strasse` and wider, or a `Hochleistungsstrasse` → **major road**
6. Search turnaround points in a cone toward the requested direction, keeping them at least 700 m apart so the loops explore different corridors.
7. Generate two different paths between the start and each turnaround point and combine them into a loop.
8. Reject loops that reuse too much of the same trail.
8a. Reject loops that spend more than the road tolerance (default 20%) of their length on roads shared with cars, and prefer the quieter ones within that. Roads are nudged away from rather than banned, since a loop usually has to leave the village on one — measurement showed a heavy penalty backfires, sending the router on detours that then bust the duration window.
9. Ask the official `geo.admin.ch` elevation-profile service for the best candidates.
10. Calculate duration using the Swiss Hiking Trails rule:
    - 15 minutes per horizontal kilometre
    - +15 minutes per 100 m ascent
    - +15 minutes per 200 m descent
    - breaks are not included
11. Compare a candidate's maximum sustained 50 m grade with the reference route. The default tolerance is +5% relative to the measured reference grade.
12. Discard loops whose measured walking time misses the requested duration by more than the duration tolerance (default 30%). The graph estimate is made before the real climb is known, so a loop can otherwise come back at twice the time asked for.
13. Rank the remaining routes by duration fit, route uniqueness, direction, and official-path share. Loops pay a score surcharge for overlapping an already-listed route, and an outright repeat of one is dropped, so the list holds different walks rather than the same walk several times.

## Outputs

For each of the top candidates (seven by default, adjustable in *Advanced routing*) the GUI provides:

- route on a swisstopo map
- estimated hiking time
- distance
- ascent / descent
- maximum sustained 50 m grade
- 95th-percentile sampled grade
- share of preferred official paths
- share of the loop shared with cars, with those stretches drawn in red on the map
- repeated-trail share
- elevation profile scaled to the route's own elevation range, with kilometre marks on the map; clicking the profile marks that exact spot on the map in red
- GPX download
- GeoJSON download

There is also a ZIP download containing all listed routes.

Fewer routes than requested can come back: a route is only listed if it fits the duration
tolerance and is genuinely different from the ones above it.

## Walking with a dog

Roads open to motor traffic are the main hazard, so they are treated as a cost and capped
by the *Max share on roads with cars* tolerance in the sidebar. The stretches that remain
are drawn in red on the map, with the metres given under the route metrics.

The classification is only as good as swissTLM3D's road attributes: it knows width,
surface and legal driving bans, but not how busy a road actually is at the hour you walk
it. A quiet 4 m lane and a rat-run are indistinguishable in the data. Treat the red
stretches as places to have the dog on the lead, not as a measured traffic count.

Around villages, official hiking routes genuinely run on paved lanes for long stretches,
so a strict tolerance can leave very few loops — or none. The planner says so rather than
quietly relaxing the limit.

## Deploying to Streamlit Community Cloud

The repository is deployable as-is: `app.py` at the root is the entry point, and every
dependency installs from wheels, so no `packages.txt` or system GDAL is required.

1. Push to a public GitHub repository.
2. On [share.streamlit.io](https://share.streamlit.io) choose the repo, branch `main`,
   and `app.py` as the main file.
3. Under *Advanced settings* select **Python 3.12** (the version this is tested on).
4. Deploy. No secrets are needed -- every data source is a public federal API.

Measured on a stock `python:3.12-slim` container capped at 1 GB of memory, which is the
closest honest proxy for the managed runtime:

| | |
|---|---|
| Dependency install | wheels only, no system libraries |
| First download of swissTLM3D | ~22 s |
| Disk after extraction | 389 MB |
| Peak memory, download + planning | 358 MB |
| Planning a 3 h loop | ~8 s |

The app has no persistent disk on a managed host, so the cache lands in the home
directory and is rebuilt after every reboot: the first visit following a cold start pays
the download once, and subsequent route requests do not. `HIKING_CACHE_DIR` overrides the
location; Docker Compose mounts a volume at `/data` and that is used automatically when
present.

The 389 MB extract is the constraint to watch. Community Cloud does not guarantee a disk
budget, so if a deploy dies during startup that is the first thing to suspect -- the
Docker route below has no such limit.

## Important limitations

This is a route-generation engine, not a live safety oracle. It currently does **not** evaluate:

- current trail closures or diversions
- weather
- snow / ice
- livestock protection dogs
- temporary dog restrictions
- hunting / wildlife quiet zones
- trail surface condition
- construction work

Those can be added as additional exclusion/penalty layers later.

## Official services used

- swissTLM3D hiking trails: https://opendata.swiss/de/dataset/swisstlm3d-wanderwege
- geo.admin.ch SearchServer: https://api3.geo.admin.ch/rest/services/ech/SearchServer
- geo.admin.ch elevation profile: https://api3.geo.admin.ch/rest/services/profile.json
- swisstopo WMTS basemap: https://wmts.geo.admin.ch/

## Cache refresh

Use **Refresh hiking dataset** in the sidebar. The app checks the `opendata.swiss` catalogue and stores the current GeoPackage under `/data/cache` inside Docker, or in `~/.cache/swiss-hiking-planner` when no `/data` volume is mounted. Set `HIKING_CACHE_DIR` to put it anywhere else.

The downloaded archive is deleted once extracted, since only the GeoPackage is ever read.
