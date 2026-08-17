from __future__ import annotations

import html
import itertools
from collections import Counter
import json
import math
import re
import shutil
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator, Sequence
from xml.etree import ElementTree as ET

import geopandas as gpd
import networkx as nx
import numpy as np
import pyogrio
import requests
from pyproj import Transformer
from shapely.geometry import GeometryCollection, LineString, MultiLineString, mapping
from shapely.ops import unary_union


CKAN_PACKAGE_URL = (
    "https://ckan.opendata.swiss/api/3/action/package_show"
    "?id=swisstlm3d-wanderwege"
)
SEARCH_URL = "https://api3.geo.admin.ch/rest/services/ech/SearchServer"
PROFILE_URL = "https://api3.geo.admin.ch/rest/services/profile.json"

EPSG_LV95 = 2056
EPSG_WGS84 = 4326

WGS84_TO_LV95 = Transformer.from_crs(EPSG_WGS84, EPSG_LV95, always_xy=True)
LV95_TO_WGS84 = Transformer.from_crs(EPSG_LV95, EPSG_WGS84, always_xy=True)

CATEGORY_WANDERWEG = "Wanderweg"
CATEGORY_BERG = "Bergwanderweg"
CATEGORY_ALPIN = "Alpinwanderweg"
CATEGORY_OTHER = "andere"
CATEGORY_UNKNOWN = "unknown"

CATEGORY_PRIORITY = {
    CATEGORY_WANDERWEG: 0,
    CATEGORY_BERG: 1,
    CATEGORY_OTHER: 2,
    CATEGORY_UNKNOWN: 3,
}

# How much motor traffic a segment is exposed to. swissTLM3D marks hiking routes on the
# road network too, so a "Wanderweg" can legitimately be a road shared with cars.
TRAFFIC_FREE = "traffic-free"          # 1-2 m paths and marked tracks: no cars
TRAFFIC_CALMED = "traffic-calmed"      # road width, but closed or restricted to motors
TRAFFIC_ROAD = "road"                  # 3-4 m road open to cars
TRAFFIC_MAJOR = "major road"           # 6 m and wider, or a high-performance road
TRAFFIC_UNKNOWN = "traffic-unknown"    # dataset without road attributes

# Ties are broken towards the more dangerous class: where a noded segment sits on both a
# path and a road, assume the road.
TRAFFIC_PRIORITY = {
    TRAFFIC_MAJOR: 0,
    TRAFFIC_ROAD: 1,
    TRAFFIC_CALMED: 2,
    TRAFFIC_FREE: 3,
    TRAFFIC_UNKNOWN: 4,
}

# Only these count against the user's road tolerance.
TRAFFIC_WITH_CARS = {TRAFFIC_ROAD, TRAFFIC_MAJOR}


class HikingPlannerError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocationResult:
    label: str
    detail: str
    lat: float
    lon: float
    origin: str
    weight: float | None = None

    @property
    def latlon(self) -> tuple[float, float]:
        return self.lat, self.lon

    @property
    def display(self) -> str:
        d = self.detail.strip()
        return f"{self.label} — {d}" if d and d.lower() not in self.label.lower() else self.label


@dataclass
class RouteStats:
    distance_km: float
    ascent_m: float
    descent_m: float
    duration_minutes: float
    max_sustained_grade_percent: float
    p95_grade_percent: float
    official_share_percent: float
    car_road_share_percent: float = 0.0
    major_road_m: float = 0.0
    repeated_share_percent: float = 0.0
    direction_alignment: float = 0.0


@dataclass
class RouteCandidate:
    nodes: list[tuple[float, float]]
    stats: RouteStats
    score: float
    pivot: tuple[float, float]
    profile: list[dict] | None = None
    # Stretches shared with cars, so the map can show where the tolerated road is.
    road_spans: list[list[tuple[float, float]]] = field(default_factory=list)


@dataclass
class PlannerConfig:
    duration_hours: float = 3.0
    other_path_penalty: float = 8.0
    unknown_path_penalty: float = 4.0
    # Roads shared with cars are the main hazard when walking a dog, so they are
    # expensive to route over -- but not banned, since a loop usually has to leave the
    # village on one. `max_car_road_share` is the tolerance: how much of the loop may
    # run on them.
    # Measured: a gentle nudge plus the hard share cap beats a heavy penalty. Large
    # penalties send the router on detours that then bust the duration window, leaving
    # only worse loops -- at 12.0 the surviving routes had a *higher* road share.
    car_road_penalty: float = 2.0
    max_car_road_share: float = 0.20
    alpine_forbidden: bool = True
    node_spacing_m: float = 20.0
    max_snap_m: float = 600.0
    direction_cone_deg: float = 75.0
    max_repeated_share: float = 0.28
    candidate_pivots: int = 24
    # Turnaround points closer together than this are treated as the same place.
    min_pivot_separation_m: float = 700.0
    alternate_paths_per_pivot: int = 4
    # Loops are cheap to measure (a batched elevation call each) and the duration
    # filter discards most of them, so profile generously to end up with a real choice.
    profile_candidates: int = 44
    # Two loops sharing more than this share of their length are the same walk.
    max_candidate_similarity: float = 0.6
    # Score surcharge a loop pays for fully overlapping an already-listed one. Keeps
    # variety from outranking loops that actually match the requested duration.
    diversity_weight: float = 1.5
    # Above this overlap two loops are the same walk and only one is ever listed.
    duplicate_similarity: float = 0.9
    # A loop whose measured walking time misses the requested duration by more than
    # this share is a different hike, not a variant of the requested one.
    max_duration_error: float = 0.30
    steepness_tolerance: float = 1.05
    steepness_window_m: float = 50.0


# ---------------------------------------------------------------------------
# Coordinates and official Swiss location search
# ---------------------------------------------------------------------------


def latlon_to_lv95(point: tuple[float, float]) -> tuple[float, float]:
    lat, lon = point
    x, y = WGS84_TO_LV95.transform(lon, lat)
    return float(x), float(y)


def lv95_to_lonlat(point: tuple[float, float]) -> tuple[float, float]:
    x, y = point
    lon, lat = LV95_TO_WGS84.transform(x, y)
    return float(lon), float(lat)


def _strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def search_locations(query: str, limit: int = 8, session: requests.Session | None = None) -> list[LocationResult]:
    query = query.strip()
    if not query:
        return []
    sess = session or requests.Session()
    response = sess.get(
        SEARCH_URL,
        params={
            "searchText": query,
            "type": "locations",
            "sr": "4326",
            "returnGeometry": "true",
            "limit": str(min(max(limit, 1), 50)),
        },
        timeout=30,
    )
    response.raise_for_status()
    results: list[LocationResult] = []
    for item in response.json().get("results", []):
        attrs = item.get("attrs", {})
        lat = attrs.get("lat")
        lon = attrs.get("lon")
        if lat is None or lon is None:
            continue
        results.append(
            LocationResult(
                label=_strip_html(str(attrs.get("label", ""))) or str(attrs.get("detail", query)),
                detail=_strip_html(str(attrs.get("detail", ""))),
                lat=float(lat),
                lon=float(lon),
                origin=str(attrs.get("origin", "")),
                weight=float(item["weight"]) if item.get("weight") is not None else None,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Dataset download / cache
# ---------------------------------------------------------------------------


def _multilingual_text(resource: dict, key: str) -> str:
    value = resource.get(key, "")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(str(v) for v in value.values())
    return str(value)


def resolve_current_geopackage(session: requests.Session | None = None) -> tuple[str, str]:
    sess = session or requests.Session()
    response = sess.get(CKAN_PACKAGE_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise HikingPlannerError("opendata.swiss CKAN request was not successful.")

    result = payload["result"]
    candidates = []
    for resource in result.get("resources", []):
        fmt = str(resource.get("format", "")).upper()
        description = _multilingual_text(resource, "description").lower()
        url = str(resource.get("download_url") or resource.get("url") or "")
        if fmt == "ZIP" and ("geopackage" in description or ".gpkg.zip" in url.lower()):
            candidates.append(resource)

    if not candidates:
        raise HikingPlannerError("Could not find the GeoPackage ZIP resource in opendata.swiss.")

    resource = candidates[0]
    url = resource.get("download_url") or resource.get("url")
    marker = "|".join(
        str(x or "")
        for x in (
            result.get("metadata_modified"),
            resource.get("modified"),
            resource.get("metadata_modified"),
            url,
        )
    )
    return str(url), marker


def download_and_extract_dataset(cache_dir: Path, refresh: bool = False) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / "swisstlm3d-wanderwege.gpkg.zip"
    extract_dir = cache_dir / "dataset"
    state_path = cache_dir / "state.json"

    session = requests.Session()
    session.headers.update({"User-Agent": "swiss-hiking-planner/2.0"})
    url, modified_marker = resolve_current_geopackage(session)

    old_state = {}
    if state_path.exists():
        try:
            old_state = json.loads(state_path.read_text("utf-8"))
        except Exception:
            old_state = {}

    gpkg_files = list(extract_dir.rglob("*.gpkg")) if extract_dir.exists() else []
    cache_current = (
        not refresh
        and zip_path.exists()
        and bool(gpkg_files)
        and old_state.get("url") == url
        and old_state.get("modified") == modified_marker
    )

    if not cache_current:
        tmp_path = zip_path.with_suffix(zip_path.suffix + ".part")
        with session.get(url, stream=True, timeout=(30, 600)) as response:
            response.raise_for_status()
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=2 * 1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        tmp_path.replace(zip_path)

        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        state_path.write_text(
            json.dumps({"url": url, "modified": modified_marker}, indent=2),
            "utf-8",
        )

    gpkg_files = list(extract_dir.rglob("*.gpkg"))
    if not gpkg_files:
        raise HikingPlannerError("No .gpkg file found after extraction.")
    return gpkg_files[0]


def select_line_layer(gpkg: Path, requested: str | None = None) -> str:
    layers = pyogrio.list_layers(gpkg)
    names = [str(row[0]) for row in layers]
    if requested:
        if requested not in names:
            raise HikingPlannerError(f"Layer {requested!r} not found. Available: {', '.join(names)}")
        return requested

    line_layers = [(str(row[0]), str(row[1] or "")) for row in layers if "LineString" in str(row[1] or "")]
    if len(line_layers) == 1:
        return line_layers[0][0]
    preferred = [name for name, _ in line_layers if "wander" in name.lower() or "weg" in name.lower()]
    if len(preferred) == 1:
        return preferred[0]
    if not line_layers:
        raise HikingPlannerError("No LineString layer found in the GeoPackage.")
    raise HikingPlannerError(
        "Several line layers were found. Set a layer explicitly. Candidates: "
        + ", ".join(name for name, _ in line_layers)
    )


# ---------------------------------------------------------------------------
# Hiking data classification and graph building
# ---------------------------------------------------------------------------


def normalize_hiking_category(value) -> str:
    if value is None:
        return CATEGORY_UNKNOWN
    if isinstance(value, (int, np.integer)):
        return {
            0: CATEGORY_WANDERWEG,
            1: CATEGORY_BERG,
            2: CATEGORY_ALPIN,
            3: CATEGORY_OTHER,
        }.get(int(value), CATEGORY_UNKNOWN)
    if isinstance(value, float) and math.isfinite(value) and float(value).is_integer():
        return normalize_hiking_category(int(value))

    s = str(value).strip().casefold()
    if not s or s in {"nan", "none", "<null>"}:
        return CATEGORY_UNKNOWN
    if s in {"0", "wanderweg"} or ("wanderweg" in s and "berg" not in s and "alpin" not in s):
        return CATEGORY_WANDERWEG
    if s in {"1", "bergwanderweg"} or "bergwander" in s:
        return CATEGORY_BERG
    if s in {"2", "alpinwanderweg"} or "alpin" in s:
        return CATEGORY_ALPIN
    if s in {"3", "andere", "other"} or "andere" in s:
        return CATEGORY_OTHER
    return CATEGORY_UNKNOWN


def normalize_traffic_class(
    objektart,
    verkehrsbeschraenkung=None,
    befahrbarkeit=None,
    verkehrsbedeutung=None,
    belagsart=None,
) -> str:
    """Classify a swissTLM3D road segment by how much motor traffic to expect on it.

    Width is the primary signal (`objektart` is "1m Weg" ... "10m Strasse"), because a
    walker with a dog cares about how much room a car has to pass. A legal driving ban
    or a segment marked as not drivable downgrades a road to traffic-calmed -- most farm
    and forest tracks are road-width but see almost no cars.
    """
    def text(value) -> str:
        if value is None:
            return ""
        s = str(value).strip().casefold()
        return "" if s in {"nan", "none", "<null>", "k_w"} else s

    art = text(objektart)
    restriction = text(verkehrsbeschraenkung)
    drivable = text(befahrbarkeit)
    importance = text(verkehrsbedeutung)

    if not art:
        return TRAFFIC_UNKNOWN

    # Expressways stay dangerous whatever else is on the record.
    if "hochleistungsstrasse" in importance:
        return TRAFFIC_MAJOR

    if "spur" in art or "weg" in art:
        width_match = re.search(r"(\d+)\s*m", art)
        if not width_match or int(width_match.group(1)) <= 2:
            return TRAFFIC_FREE

    restricted = (
        "fahrverbot" in restriction
        or "verkehrsbeschraenkung" in restriction
        or "gesperrt" in restriction
        or drivable == "falsch"
        or text(belagsart) == "natur"
    )

    width_match = re.search(r"(\d+)\s*m", art)
    width = int(width_match.group(1)) if width_match else 0

    if width and width <= 2:
        return TRAFFIC_FREE
    if restricted:
        return TRAFFIC_CALMED
    if width >= 6:
        return TRAFFIC_MAJOR
    if width >= 3 or any(k in art for k in ("strasse", "platz", "verbindung", "fahrt")):
        return TRAFFIC_ROAD
    return TRAFFIC_UNKNOWN


def _column(columns: Sequence[str], name: str) -> str | None:
    for c in columns:
        if str(c).casefold() == name:
            return str(c)
    return None


def detect_hiking_column(columns: Sequence[str]) -> str | None:
    normalized = {c.casefold(): c for c in columns}
    if "wanderwege" in normalized:
        return normalized["wanderwege"]
    candidates = [c for c in columns if "wander" in c.casefold()]
    return candidates[0] if len(candidates) == 1 else None


def _bbox_around(center_lv95: tuple[float, float], radius_m: float) -> tuple[float, float, float, float]:
    x, y = center_lv95
    return x - radius_m, y - radius_m, x + radius_m, y + radius_m


def _bbox_around_points(points: Sequence[tuple[float, float]], margin_m: float) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs) - margin_m, min(ys) - margin_m, max(xs) + margin_m, max(ys) + margin_m


def load_hiking_lines(
    gpkg: Path,
    layer: str,
    bbox: tuple[float, float, float, float],
    alpine_forbidden: bool = True,
) -> gpd.GeoDataFrame:
    gdf = pyogrio.read_dataframe(gpkg, layer=layer, bbox=bbox)
    if gdf.empty:
        raise HikingPlannerError("No hiking features found in the selected area.")
    if gdf.crs is None:
        raise HikingPlannerError("The hiking dataset has no CRS information.")
    if gdf.crs.to_epsg() != EPSG_LV95:
        gdf = gdf.to_crs(EPSG_LV95)

    gdf = gdf[
        gdf.geometry.notna()
        & ~gdf.geometry.is_empty
        & gdf.geom_type.isin(["LineString", "MultiLineString"])
    ].copy()
    if gdf.empty:
        raise HikingPlannerError("No usable hiking line geometry found.")

    category_col = detect_hiking_column([str(c) for c in gdf.columns if c != gdf.geometry.name])
    if category_col:
        gdf["_category"] = gdf[category_col].map(normalize_hiking_category)
    else:
        # Dedicated Wanderwege package, but if schema classification cannot be found we keep
        # it explicit as unknown instead of inventing an official category.
        gdf["_category"] = CATEGORY_UNKNOWN

    columns = [str(c) for c in gdf.columns if c != gdf.geometry.name]
    art_col = _column(columns, "objektart")
    if art_col:
        gdf["_traffic"] = [
            normalize_traffic_class(art, restriction, drivable, importance, surface)
            for art, restriction, drivable, importance, surface in zip(
                gdf[art_col],
                *(
                    gdf[_column(columns, name)] if _column(columns, name) else [None] * len(gdf)
                    for name in ("verkehrsbeschraenkung", "befahrbarkeit", "verkehrsbedeutung", "belagsart")
                ),
            )
        ]
    else:
        # Without road attributes we cannot tell a lane from a main road. Say so by
        # classifying everything unknown rather than quietly declaring it traffic-free.
        gdf["_traffic"] = TRAFFIC_UNKNOWN

    if alpine_forbidden:
        gdf = gdf[gdf["_category"] != CATEGORY_ALPIN].copy()
    if gdf.empty:
        raise HikingPlannerError("No hiking lines remain after excluding alpine hiking trails.")
    return gdf


def iter_lines(geom) -> Iterator[LineString]:
    if geom is None or geom.is_empty:
        return
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        for part in geom.geoms:
            yield from iter_lines(part)
    elif isinstance(geom, GeometryCollection):
        for part in geom.geoms:
            yield from iter_lines(part)


def _has_z(coord: Sequence[float]) -> bool:
    return len(coord) >= 3 and math.isfinite(float(coord[2]))


def _interp(a: Sequence[float], b: Sequence[float], t: float) -> tuple[float, ...]:
    if _has_z(a) and _has_z(b):
        return (
            float(a[0] + (b[0] - a[0]) * t),
            float(a[1] + (b[1] - a[1]) * t),
            float(a[2] + (b[2] - a[2]) * t),
        )
    return float(a[0] + (b[0] - a[0]) * t), float(a[1] + (b[1] - a[1]) * t)


def _node_key(coord: Sequence[float]) -> tuple[float, float]:
    return round(float(coord[0]), 2), round(float(coord[1]), 2)


def swiss_hiking_minutes(distance_m: float, ascent_m: float, descent_m: float) -> float:
    """Swiss Hiking Trails rule: 15 min/km + 15 min/100 m up + 15 min/200 m down."""
    return (distance_m / 1000.0) * 15.0 + (max(ascent_m, 0.0) / 100.0) * 15.0 + (max(descent_m, 0.0) / 200.0) * 15.0


def _class_unions(gdf: gpd.GeoDataFrame, column: str, classes: Sequence[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in classes:
        subset = gdf.loc[gdf[column] == value, "geometry"]
        if len(subset):
            result[value] = unary_union(subset.to_numpy())
    return result


def _category_unions(gdf: gpd.GeoDataFrame) -> dict[str, object]:
    return _class_unions(
        gdf, "_category", [CATEGORY_WANDERWEG, CATEGORY_BERG, CATEGORY_OTHER, CATEGORY_UNKNOWN]
    )


def _traffic_unions(gdf: gpd.GeoDataFrame) -> dict[str, object]:
    return _class_unions(
        gdf,
        "_traffic",
        [TRAFFIC_MAJOR, TRAFFIC_ROAD, TRAFFIC_CALMED, TRAFFIC_FREE, TRAFFIC_UNKNOWN],
    )


def _classify_noded_line(
    line: LineString,
    class_unions: dict[str, object],
    priority: dict[str, int] | None = None,
    fallback: str = CATEGORY_UNKNOWN,
) -> str:
    priority = priority if priority is not None else CATEGORY_PRIORITY
    midpoint = line.interpolate(0.5, normalized=True)
    distances = []
    for cat, geom in class_unions.items():
        try:
            d = float(geom.distance(midpoint))
        except Exception:
            continue
        distances.append((d, priority.get(cat, 99), cat))
    if not distances:
        return fallback
    distances.sort()
    # Noded geometry originates from the source lines, so a tiny tolerance is expected.
    return distances[0][2] if distances[0][0] <= 0.5 else fallback


def build_graph(gdf: gpd.GeoDataFrame, config: PlannerConfig) -> nx.Graph:
    class_unions = _category_unions(gdf)
    traffic_unions = _traffic_unions(gdf)
    noded = unary_union(gdf.geometry.to_numpy())
    graph = nx.Graph()

    category_penalty = {
        CATEGORY_WANDERWEG: 1.0,
        CATEGORY_BERG: 1.0,
        CATEGORY_OTHER: max(config.other_path_penalty, 1.0),
        CATEGORY_UNKNOWN: max(config.unknown_path_penalty, 1.0),
    }
    road = max(config.car_road_penalty, 1.0)
    traffic_penalty = {
        TRAFFIC_FREE: 1.0,
        TRAFFIC_UNKNOWN: 1.0,
        # A farm track is worth a nudge away from, not a detour over a mountain.
        TRAFFIC_CALMED: 1.0 + (road - 1.0) * 0.15,
        TRAFFIC_ROAD: road,
        TRAFFIC_MAJOR: road * 4.0,
    }

    for line in iter_lines(noded):
        category = _classify_noded_line(line, class_unions)
        traffic = _classify_noded_line(
            line, traffic_unions, priority=TRAFFIC_PRIORITY, fallback=TRAFFIC_UNKNOWN
        )
        coords = list(line.coords)
        if len(coords) < 2:
            continue

        for a, b in zip(coords[:-1], coords[1:]):
            horizontal = math.hypot(float(b[0] - a[0]), float(b[1] - a[1]))
            if horizontal <= 0:
                continue
            subdivisions = max(1, int(math.ceil(horizontal / config.node_spacing_m)))
            prev = _interp(a, b, 0.0)
            for i in range(1, subdivisions + 1):
                cur = _interp(a, b, i / subdivisions)
                length = math.hypot(cur[0] - prev[0], cur[1] - prev[1])
                if length <= 0:
                    prev = cur
                    continue
                dz = cur[2] - prev[2] if _has_z(prev) and _has_z(cur) else 0.0
                ascent = max(float(dz), 0.0)
                descent = max(float(-dz), 0.0)
                walk_minutes = swiss_hiking_minutes(length, ascent, descent)
                u, v = _node_key(prev), _node_key(cur)
                if u == v:
                    prev = cur
                    continue
                attrs = {
                    "length": float(length),
                    "ascent": ascent,
                    "descent": descent,
                    "walk_minutes": walk_minutes,
                    "category": category,
                    "official": category in {CATEGORY_WANDERWEG, CATEGORY_BERG},
                    "traffic": traffic,
                    "car_road": traffic in TRAFFIC_WITH_CARS,
                    "routing_cost": (
                        walk_minutes
                        * category_penalty.get(category, config.unknown_path_penalty)
                        * traffic_penalty.get(traffic, 1.0)
                    ),
                }
                if not graph.has_edge(u, v) or attrs["routing_cost"] < graph[u][v]["routing_cost"]:
                    graph.add_edge(u, v, **attrs)
                prev = cur

    if graph.number_of_edges() == 0:
        raise HikingPlannerError("The routing graph contains no edges.")
    return graph


def nearest_graph_node(graph: nx.Graph, point: tuple[float, float]) -> tuple[tuple[float, float], float]:
    nodes = np.asarray(list(graph.nodes), dtype=float)
    if len(nodes) == 0:
        raise HikingPlannerError("Routing graph is empty.")
    d2 = (nodes[:, 0] - point[0]) ** 2 + (nodes[:, 1] - point[1]) ** 2
    idx = int(np.argmin(d2))
    node = (float(nodes[idx, 0]), float(nodes[idx, 1]))
    return node, float(math.sqrt(d2[idx]))


def _edge_key(u: tuple[float, float], v: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (u, v) if u <= v else (v, u)


def path_edges(path: Sequence[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [_edge_key(u, v) for u, v in zip(path[:-1], path[1:])]


def path_graph_stats(graph: nx.Graph, path: Sequence[tuple[float, float]]) -> dict:
    distance = ascent = descent = minutes = official_distance = 0.0
    car_distance = major_distance = 0.0
    for u, v in zip(path[:-1], path[1:]):
        data = graph[u][v]
        distance += float(data["length"])
        ascent += float(data.get("ascent", 0.0))
        descent += float(data.get("descent", 0.0))
        minutes += float(data.get("walk_minutes", 0.0))
        if data.get("official"):
            official_distance += float(data["length"])
        if data.get("car_road"):
            car_distance += float(data["length"])
            if data.get("traffic") == TRAFFIC_MAJOR:
                major_distance += float(data["length"])
    return {
        "distance_m": distance,
        "ascent_m": ascent,
        "descent_m": descent,
        "duration_minutes": minutes,
        "official_share_percent": 100.0 * official_distance / distance if distance else 0.0,
        "car_road_share_percent": 100.0 * car_distance / distance if distance else 0.0,
        "car_road_m": car_distance,
        "major_road_m": major_distance,
    }


def route_between(graph: nx.Graph, a_lv95: tuple[float, float], b_lv95: tuple[float, float], max_snap_m: float) -> list[tuple[float, float]]:
    a, da = nearest_graph_node(graph, a_lv95)
    b, db = nearest_graph_node(graph, b_lv95)
    if da > max_snap_m or db > max_snap_m:
        raise HikingPlannerError(
            f"Reference point is too far from hiking network (snap {max(da, db):.0f} m; limit {max_snap_m:.0f} m)."
        )
    try:
        return nx.shortest_path(graph, source=a, target=b, weight="routing_cost")
    except nx.NetworkXNoPath as exc:
        raise HikingPlannerError("No hiking route found between the reference points.") from exc


# ---------------------------------------------------------------------------
# Elevation profiles and steepness
# ---------------------------------------------------------------------------


def _profile_height(item: dict) -> float | None:
    alts = item.get("alts") or {}
    for key in ("COMB", "DTM2", "DTM25"):
        value = alts.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def _profile_input(route_nodes: Sequence[tuple[float, float]], max_points: int = 4500) -> LineString:
    line = LineString(route_nodes)
    if len(route_nodes) <= max_points:
        return line
    tolerance = 0.25
    simplified = line
    while len(simplified.coords) > max_points and tolerance <= 50:
        simplified = line.simplify(tolerance, preserve_topology=False)
        tolerance *= 2
    if len(simplified.coords) > max_points:
        indexes = np.linspace(0, len(route_nodes) - 1, max_points).astype(int)
        simplified = LineString([route_nodes[i] for i in indexes])
    return simplified


def fetch_elevation_profile(
    route_nodes: Sequence[tuple[float, float]],
    session: requests.Session | None = None,
    spacing_m: float = 20.0,
) -> list[dict]:
    sess = session or requests.Session()
    line = _profile_input(route_nodes)
    nb_points = int(min(2000, max(200, math.ceil(max(line.length, 1.0) / max(spacing_m, 5.0)))))
    response = sess.post(
        PROFILE_URL,
        data={
            "geom": json.dumps(mapping(line)),
            "sr": str(EPSG_LV95),
            "nb_points": str(nb_points),
            "offset": "3",
            "distinct_points": "false",
        },
        timeout=60,
    )
    response.raise_for_status()
    profile = response.json()
    if not isinstance(profile, list) or not profile:
        raise HikingPlannerError("geo.admin.ch returned an empty elevation profile.")
    return profile


def _sustained_max_grade(dist: np.ndarray, elev: np.ndarray, window_m: float) -> float:
    if len(dist) < 2:
        return 0.0
    max_grade = 0.0
    j = 1
    for i in range(len(dist) - 1):
        j = max(j, i + 1)
        while j < len(dist) and dist[j] - dist[i] < window_m:
            j += 1
        if j >= len(dist):
            break
        dd = dist[j] - dist[i]
        if dd > 0:
            max_grade = max(max_grade, abs(elev[j] - elev[i]) / dd * 100.0)
    return float(max_grade)


def profile_stats(profile: Sequence[dict], distance_m: float, window_m: float = 50.0) -> dict:
    dists: list[float] = []
    elevs: list[float] = []
    for item in profile:
        z = _profile_height(item)
        try:
            d = float(item["dist"])
        except (KeyError, TypeError, ValueError):
            continue
        if z is not None and math.isfinite(z) and math.isfinite(d):
            dists.append(d)
            elevs.append(z)

    if len(dists) < 2:
        raise HikingPlannerError("Elevation profile did not contain enough valid samples.")

    d = np.asarray(dists, dtype=float)
    z = np.asarray(elevs, dtype=float)
    dz = np.diff(z)
    dd = np.diff(d)
    valid = dd > 0
    ascent = float(np.sum(np.maximum(dz, 0.0)))
    descent = float(np.sum(np.maximum(-dz, 0.0)))
    grades = np.abs(dz[valid] / dd[valid] * 100.0) if np.any(valid) else np.asarray([0.0])
    p95 = float(np.percentile(grades, 95)) if len(grades) else 0.0
    sustained = _sustained_max_grade(d, z, window_m)
    minutes = swiss_hiking_minutes(distance_m, ascent, descent)
    return {
        "ascent_m": ascent,
        "descent_m": descent,
        "duration_minutes": minutes,
        "p95_grade_percent": p95,
        "max_sustained_grade_percent": sustained,
    }


# ---------------------------------------------------------------------------
# Loop generation
# ---------------------------------------------------------------------------


def _direction_alignment(start: tuple[float, float], pivot: tuple[float, float], target: tuple[float, float]) -> float:
    a = np.asarray([pivot[0] - start[0], pivot[1] - start[1]], dtype=float)
    b = np.asarray([target[0] - start[0], target[1] - start[1]], dtype=float)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))


def _path_overlap_share(graph: nx.Graph, p1: Sequence[tuple[float, float]], p2: Sequence[tuple[float, float]]) -> float:
    e1 = set(path_edges(p1))
    e2 = set(path_edges(p2))
    overlap = e1 & e2
    overlap_len = sum(float(graph[u][v]["length"]) for u, v in overlap)
    len1 = path_graph_stats(graph, p1)["distance_m"]
    len2 = path_graph_stats(graph, p2)["distance_m"]
    total = len1 + len2
    return (2.0 * overlap_len / total) if total > 0 else 1.0


def car_road_spans(graph: nx.Graph, path: Sequence[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Consecutive runs of the path that share the road with cars."""
    spans: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for u, v in zip(path[:-1], path[1:]):
        if graph[u][v].get("car_road"):
            if not current:
                current.append(u)
            current.append(v)
        elif current:
            spans.append(current)
            current = []
    if current:
        spans.append(current)
    return spans


def _combine_out_and_back_paths(
    out_path: Sequence[tuple[float, float]],
    back_basis: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    # both paths are start -> pivot. Reverse second one to return pivot -> start.
    return list(out_path) + list(reversed(back_basis))[1:]


def _loop_edge_profile(graph: nx.Graph, path: Sequence[tuple[float, float]]) -> tuple[frozenset, float]:
    edges = frozenset(path_edges(path))
    length = sum(float(graph[u][v]["length"]) for u, v in edges)
    return edges, length


def _loop_similarity(graph: nx.Graph, a: tuple[frozenset, float], b: tuple[frozenset, float]) -> float:
    """Length-weighted similarity of two loops: 0.0 = fully disjoint, 1.0 = same trail."""
    (edges_a, len_a), (edges_b, len_b) = a, b
    shared = edges_a & edges_b
    if not shared:
        return 0.0
    shared_length = sum(float(graph[u][v]["length"]) for u, v in shared)
    total = len_a + len_b
    return (2.0 * shared_length / total) if total > 0 else 1.0


def _select_diverse_candidates(
    graph: nx.Graph,
    candidates: list[tuple[float, list[tuple[float, float]], tuple[float, float], float]],
    limit: int,
    max_similarity: float,
) -> list:
    """Pick the best-scoring loops that are also meaningfully different from each other.

    Comparing edge sets for exact equality is not enough: neighbouring pivots and the
    shared out-leg of `combinations(alternatives, 2)` produce loops that differ by a
    single spur, which reads as the same walk shown three times. Candidates are
    therefore accepted only while they stay below `max_similarity` against every
    loop already picked. If too few survive, the remainder is backfilled in score
    order so the caller still receives a full batch to evaluate.
    """
    ordered = sorted(candidates, key=lambda x: x[0])
    profiles = [_loop_edge_profile(graph, item[1]) for item in ordered]

    picked: list[int] = []
    rejected: list[int] = []
    for i in range(len(ordered)):
        if len(picked) >= limit:
            break
        if any(_loop_similarity(graph, profiles[i], profiles[j]) > max_similarity for j in picked):
            rejected.append(i)
            continue
        picked.append(i)

    if len(picked) < limit:
        picked.extend(rejected[: limit - len(picked)])
        picked.sort()

    return [ordered[i] for i in picked]


def _order_by_diversity(
    graph: nx.Graph,
    candidates: list[RouteCandidate],
    diversity_weight: float,
    duplicate_similarity: float,
) -> list[RouteCandidate]:
    """Rank scored candidates so the leading ones are as distinct as the set allows.

    Two rules, because overlap comes in two flavours. Partial overlap is a surcharge
    on the score, not a veto: a loop retracing part of an earlier one pays for what it
    repeats, but a different loop still has to be a plausible answer to the request in
    order to overtake it -- an absolute preference for novelty promotes a 7 h
    expedition over a good 3 h walk purely for looking different. Overlap above
    `duplicate_similarity` is the same walk, though, and no surcharge makes listing it
    twice useful, so it is dropped outright.

    The returned list can therefore be shorter than the input. Offering two routes is
    a better answer than offering the same route three times.
    """
    profiles = [_loop_edge_profile(graph, c.nodes) for c in candidates]
    remaining = list(range(len(candidates)))
    ordered: list[int] = []

    while remaining:
        if not ordered:
            pick = remaining[0]  # candidates arrive sorted by score
        else:
            ranked = []
            for i in remaining:
                overlap = max(_loop_similarity(graph, profiles[i], profiles[j]) for j in ordered)
                if overlap > duplicate_similarity:
                    continue
                ranked.append((candidates[i].score + overlap * diversity_weight, i))
            if not ranked:
                break  # everything left is a repeat of something already listed
            pick = min(ranked)[1]
        ordered.append(pick)
        remaining.remove(pick)

    return [candidates[i] for i in ordered]


def generate_loop_candidates(
    graph: nx.Graph,
    start_lv95: tuple[float, float],
    direction_target_lv95: tuple[float, float],
    config: PlannerConfig,
    reference_grade_limit: float | None = None,
    session: requests.Session | None = None,
) -> list[RouteCandidate]:
    start_node, snap = nearest_graph_node(graph, start_lv95)
    if snap > config.max_snap_m:
        raise HikingPlannerError(
            f"Start is {snap:.0f} m from the hiking network; limit is {config.max_snap_m:.0f} m."
        )

    target_minutes = config.duration_hours * 60.0
    half = target_minutes / 2.0
    distances = nx.single_source_dijkstra_path_length(graph, start_node, cutoff=target_minutes * 0.8, weight="walk_minutes")

    cos_limit = math.cos(math.radians(config.direction_cone_deg))
    pivot_pool = []
    for node, minutes in distances.items():
        if node == start_node or minutes < half * 0.45 or minutes > half * 1.35:
            continue
        alignment = _direction_alignment(start_node, node, direction_target_lv95)
        if alignment < cos_limit:
            continue
        # Prefer about half the target duration and strongly prefer forward direction.
        pivot_score = abs(minutes - half) / max(half, 1.0) + (1.0 - alignment) * 0.65
        pivot_pool.append((pivot_score, node, alignment))

    # Taking the N best-scoring pivots picks N neighbours on the same ridge, and every
    # loop built from them walks the same corridor. Requiring a minimum spacing buys
    # genuinely different walks: separate turnaround points mean separate valleys.
    pivot_pool.sort(key=lambda x: x[0])
    spread: list[tuple[float, tuple[float, float], float]] = []
    for item in pivot_pool:
        node = item[1]
        if any(
            math.hypot(node[0] - other[0], node[1] - other[1]) < config.min_pivot_separation_m
            for _, other, _ in spread
        ):
            continue
        spread.append(item)
        if len(spread) >= config.candidate_pivots:
            break
    pivot_pool = spread
    if not pivot_pool:
        raise HikingPlannerError("No suitable turnaround points were found in the requested direction.")

    rough_candidates: list[tuple[float, list[tuple[float, float]], tuple[float, float], float]] = []
    too_much_road: list[float] = []
    for _, pivot, alignment in pivot_pool:
        alternatives: list[list[tuple[float, float]]] = []
        used_counts: Counter = Counter()
        signatures = set()

        for alt_index in range(config.alternate_paths_per_pivot):
            def alt_weight(u, v, data):
                base = float(data.get("routing_cost", data.get("walk_minutes", 1.0)))
                count = used_counts[_edge_key(u, v)]
                # First path is the preferred route. Subsequent routes pay heavily for
                # reusing its edges, which produces a practical loop without running
                # Yen's k-shortest-simple-path algorithm over a large national graph.
                return base * (1.0 + 10.0 * count)

            try:
                path = nx.shortest_path(graph, start_node, pivot, weight=alt_weight)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                break
            signature = tuple(path_edges(path))
            if signature in signatures:
                break
            signatures.add(signature)
            alternatives.append(path)
            for edge in path_edges(path):
                used_counts[edge] += 1

        if len(alternatives) < 2:
            continue

        for p1, p2 in itertools.combinations(alternatives, 2):
            repeated = _path_overlap_share(graph, p1, p2)
            if repeated > config.max_repeated_share:
                continue
            loop = _combine_out_and_back_paths(p1, p2)
            gs = path_graph_stats(graph, loop)
            car_share = gs["car_road_share_percent"] / 100.0
            if car_share > config.max_car_road_share:
                too_much_road.append(gs["car_road_share_percent"])
                continue
            duration_error = abs(gs["duration_minutes"] - target_minutes) / max(target_minutes, 1.0)
            official_penalty = max(0.0, 1.0 - gs["official_share_percent"] / 100.0)
            rough_score = (
                duration_error * 3.0
                + repeated * 2.5
                + (1.0 - alignment) * 0.8
                + official_penalty * 2.0
                # Within the tolerance, still prefer the loop that meets fewer cars.
                + car_share * 3.0
            )
            rough_candidates.append((rough_score, loop, pivot, repeated))

    rough_candidates = _select_diverse_candidates(
        graph,
        rough_candidates,
        limit=config.profile_candidates,
        max_similarity=config.max_candidate_similarity,
    )
    if not rough_candidates:
        if too_much_road:
            raise HikingPlannerError(
                f"Every loop here spends more than {config.max_car_road_share * 100:.0f}% of its "
                f"length on roads shared with cars (best was {min(too_much_road):.0f}%). Raise the "
                f"road tolerance, or start somewhere with more car-free paths."
            )
        raise HikingPlannerError(
            "The local hiking graph has no sufficiently non-retracing loop in that direction. Try a wider direction cone or allow more repeated trail."
        )

    sess = session or requests.Session()
    final: list[RouteCandidate] = []
    over_target: list[float] = []
    for rough_score, loop, pivot, repeated in rough_candidates:
        gs = path_graph_stats(graph, loop)
        try:
            profile = fetch_elevation_profile(loop, session=sess)
            ps = profile_stats(profile, gs["distance_m"], window_m=config.steepness_window_m)
        except Exception:
            # Fail closed on a requested steepness reference. Without profile we cannot assert
            # that the candidate respects the user's maximum steepness.
            if reference_grade_limit is not None:
                continue
            profile = None
            ps = {
                "ascent_m": gs["ascent_m"],
                "descent_m": gs["descent_m"],
                "duration_minutes": gs["duration_minutes"],
                "p95_grade_percent": 0.0,
                "max_sustained_grade_percent": 0.0,
            }

        grade = float(ps["max_sustained_grade_percent"])
        if reference_grade_limit is not None and grade > reference_grade_limit:
            continue

        actual_duration = float(ps["duration_minutes"])
        duration_error = abs(actual_duration - target_minutes) / max(target_minutes, 1.0)
        # Pivots are chosen on the graph's flat-ish time estimate; the measured profile
        # adds the real climbing time and can push a loop far past what was asked for.
        # Someone who asked for three hours does not want a seven-hour day.
        if duration_error > config.max_duration_error:
            over_target.append(actual_duration)
            continue
        official_penalty = max(0.0, 1.0 - gs["official_share_percent"] / 100.0)
        alignment = _direction_alignment(start_node, pivot, direction_target_lv95)
        slope_penalty = 0.0
        if reference_grade_limit and reference_grade_limit > 0:
            slope_penalty = min(grade / reference_grade_limit, 1.0) * 0.25

        car_share = gs["car_road_share_percent"] / 100.0
        score = (
            duration_error * 4.0
            + repeated * 2.8
            + (1.0 - alignment) * 0.8
            + official_penalty * 2.4
            + slope_penalty
            + car_share * 3.5
        )
        stats = RouteStats(
            distance_km=gs["distance_m"] / 1000.0,
            ascent_m=float(ps["ascent_m"]),
            descent_m=float(ps["descent_m"]),
            duration_minutes=actual_duration,
            max_sustained_grade_percent=grade,
            p95_grade_percent=float(ps["p95_grade_percent"]),
            official_share_percent=gs["official_share_percent"],
            car_road_share_percent=gs["car_road_share_percent"],
            major_road_m=gs["major_road_m"],
            repeated_share_percent=repeated * 100.0,
            direction_alignment=alignment,
        )
        final.append(
            RouteCandidate(
                nodes=loop,
                stats=stats,
                score=score,
                pivot=pivot,
                profile=profile,
                road_spans=car_road_spans(graph, loop),
            )
        )

    final.sort(key=lambda c: c.score)
    final = _order_by_diversity(
        graph, final, config.diversity_weight, config.duplicate_similarity
    )
    if not final:
        if over_target:
            closest = min(over_target, key=lambda m: abs(m - target_minutes))
            raise HikingPlannerError(
                f"Every loop found here misses the requested {config.duration_hours:.2f} h by more "
                f"than {config.max_duration_error * 100:.0f}% once the climb is measured "
                f"(closest was {closest / 60:.2f} h). Try that duration, or a start with a denser "
                f"trail network."
            )
        if reference_grade_limit is not None:
            raise HikingPlannerError(
                "Loop candidates were found, but none stayed within the reference steepness limit."
            )
        raise HikingPlannerError("Loop candidates were found, but none could be evaluated successfully.")
    return final


def compute_reference_grade(
    gpkg: Path,
    layer: str,
    ref_start_latlon: tuple[float, float],
    ref_end_latlon: tuple[float, float],
    config: PlannerConfig,
    session: requests.Session | None = None,
) -> tuple[float, RouteStats, list[tuple[float, float]], list[dict]]:
    a = latlon_to_lv95(ref_start_latlon)
    b = latlon_to_lv95(ref_end_latlon)
    direct = math.hypot(b[0] - a[0], b[1] - a[1])
    bbox = _bbox_around_points([a, b], margin_m=max(2500.0, direct * 0.6))
    gdf = load_hiking_lines(gpkg, layer, bbox, alpine_forbidden=config.alpine_forbidden)
    graph = build_graph(gdf, config)
    route = route_between(graph, a, b, config.max_snap_m)
    gs = path_graph_stats(graph, route)
    profile = fetch_elevation_profile(route, session=session)
    ps = profile_stats(profile, gs["distance_m"], window_m=config.steepness_window_m)
    stats = RouteStats(
        distance_km=gs["distance_m"] / 1000.0,
        ascent_m=ps["ascent_m"],
        descent_m=ps["descent_m"],
        duration_minutes=ps["duration_minutes"],
        max_sustained_grade_percent=ps["max_sustained_grade_percent"],
        p95_grade_percent=ps["p95_grade_percent"],
        official_share_percent=gs["official_share_percent"],
    )
    limit = stats.max_sustained_grade_percent * config.steepness_tolerance
    return limit, stats, route, profile


def plan_loops(
    gpkg: Path,
    layer: str,
    start_latlon: tuple[float, float],
    direction_latlon: tuple[float, float],
    config: PlannerConfig,
    reference_grade_limit: float | None = None,
    session: requests.Session | None = None,
) -> tuple[list[RouteCandidate], nx.Graph]:
    start = latlon_to_lv95(start_latlon)
    direction = latlon_to_lv95(direction_latlon)

    # Three hours on Swiss hiking-time rules can mean anything from a short steep loop
    # to roughly 12 km flat. This radius is intentionally generous but local.
    nominal_flat_km = config.duration_hours * 4.0
    radius_m = max(6000.0, nominal_flat_km * 1000.0 * 0.8)

    last_error: Exception | None = None
    for factor in (1.0, 1.35, 1.8):
        bbox = _bbox_around(start, radius_m * factor)
        try:
            gdf = load_hiking_lines(gpkg, layer, bbox, alpine_forbidden=config.alpine_forbidden)
            graph = build_graph(gdf, config)
            candidates = generate_loop_candidates(
                graph,
                start,
                direction,
                config,
                reference_grade_limit=reference_grade_limit,
                session=session,
            )
            return candidates, graph
        except HikingPlannerError as exc:
            last_error = exc
    raise HikingPlannerError(f"Could not generate a loop after expanding the search area. Last reason: {last_error}")


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def candidate_geojson(candidate: RouteCandidate, name: str = "Hiking loop") -> bytes:
    coords = [lv95_to_lonlat(p) for p in candidate.nodes]
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": name, **asdict(candidate.stats)},
                "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in coords]},
            }
        ],
    }
    return json.dumps(fc, ensure_ascii=False, indent=2).encode("utf-8")


def candidate_gpx(candidate: RouteCandidate, name: str = "Hiking loop") -> bytes:
    ns = "http://www.topografix.com/GPX/1/1"
    ET.register_namespace("", ns)
    gpx = ET.Element(f"{{{ns}}}gpx", {"version": "1.1", "creator": "swiss-hiking-planner"})
    trk = ET.SubElement(gpx, f"{{{ns}}}trk")
    ET.SubElement(trk, f"{{{ns}}}name").text = name
    seg = ET.SubElement(trk, f"{{{ns}}}trkseg")

    if candidate.profile:
        points = []
        for item in candidate.profile:
            try:
                x = float(item["easting"])
                y = float(item["northing"])
            except (KeyError, TypeError, ValueError):
                continue
            lon, lat = lv95_to_lonlat((x, y))
            points.append((lat, lon, _profile_height(item)))
    else:
        points = []
        for p in candidate.nodes:
            lon, lat = lv95_to_lonlat(p)
            points.append((lat, lon, None))

    for lat, lon, ele in points:
        pt = ET.SubElement(seg, f"{{{ns}}}trkpt", {"lat": f"{lat:.8f}", "lon": f"{lon:.8f}"})
        if ele is not None:
            ET.SubElement(pt, f"{{{ns}}}ele").text = f"{ele:.1f}"

    ET.indent(gpx, space="  ")
    return ET.tostring(gpx, encoding="utf-8", xml_declaration=True)


def route_latlon(nodes: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for p in nodes:
        lon, lat = lv95_to_lonlat(p)
        out.append((lat, lon))
    return out
