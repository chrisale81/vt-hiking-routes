import math
import re

import networkx as nx
import pytest

import i18n


def message_fragment(key: str) -> str:
    """The longest literal chunk of a catalogue message.

    Tests match on this rather than on hardcoded wording, so re-phrasing a translation
    does not break them.
    """
    raw = i18n._catalogue(i18n.LANGUAGE)[key]
    return max(re.split(r"\{[^}]*\}", raw), key=len).strip()

from router import (
    CATEGORY_ALPIN,
    CATEGORY_BERG,
    CATEGORY_OTHER,
    CATEGORY_WANDERWEG,
    normalize_hiking_category,
    path_graph_stats,
    swiss_hiking_minutes,
)


def test_category_mapping():
    assert normalize_hiking_category(0) == CATEGORY_WANDERWEG
    assert normalize_hiking_category(1) == CATEGORY_BERG
    assert normalize_hiking_category(2) == CATEGORY_ALPIN
    assert normalize_hiking_category(3) == CATEGORY_OTHER
    assert normalize_hiking_category("Alpinwanderweg") == CATEGORY_ALPIN


def test_swiss_hiking_time_rule():
    # 12 km flat + 400 m ascent = 3 h + 1 h = 4 h.
    assert math.isclose(swiss_hiking_minutes(12_000, 400, 0), 240.0)
    # 1 km flat + 200 m descent = 15 + 15 minutes.
    assert math.isclose(swiss_hiking_minutes(1_000, 0, 200), 30.0)


def test_path_stats_official_share():
    g = nx.Graph()
    a, b, c = (0.0, 0.0), (100.0, 0.0), (200.0, 0.0)
    g.add_edge(a, b, length=100.0, ascent=0.0, descent=0.0, walk_minutes=1.5, official=True)
    g.add_edge(b, c, length=100.0, ascent=0.0, descent=0.0, walk_minutes=1.5, official=False)
    stats = path_graph_stats(g, [a, b, c])
    assert stats["distance_m"] == 200.0
    assert stats["official_share_percent"] == 50.0


def _diversity_graph():
    """Two genuinely different loops plus a near-clone of the first (one detoured edge)."""
    g = nx.Graph()

    def connect(u, v, minutes=15.0):
        g.add_edge(
            u,
            v,
            length=minutes * 66.7,
            ascent=0.0,
            descent=0.0,
            walk_minutes=minutes,
            category=CATEGORY_WANDERWEG,
            official=True,
            routing_cost=minutes,
        )

    return g, connect


def test_loop_similarity_scores_shared_length():
    from router import _loop_edge_profile, _loop_similarity

    g, connect = _diversity_graph()
    s, a, b, p = (0.0, 0.0), (1000.0, 500.0), (1000.0, -500.0), (2000.0, 0.0)
    connect(s, a)
    connect(a, p)
    connect(s, b)
    connect(b, p)

    loop = [s, a, p, b, s]
    same = _loop_edge_profile(g, loop)
    assert _loop_similarity(g, same, same) == 1.0

    half = _loop_edge_profile(g, [s, a, p, a, s])
    # shares the s-a-p leg only
    assert 0.4 < _loop_similarity(g, same, half) < 0.8

    other = _loop_edge_profile(g, [s, b, p, b, s])
    assert _loop_similarity(g, _loop_edge_profile(g, [s, a, p, a, s]), other) == 0.0


def test_select_diverse_candidates_drops_near_duplicates():
    from router import _select_diverse_candidates

    g, connect = _diversity_graph()
    s, a, b, p = (0.0, 0.0), (1000.0, 500.0), (1000.0, -500.0), (2000.0, 0.0)
    detour = (1000.0, 600.0)
    c = (1000.0, 1500.0)
    connect(s, a)
    connect(a, p)
    connect(s, b)
    connect(b, p)
    # a separate corridor, sharing no trail with the a/b legs
    connect(s, c)
    connect(c, p)
    # a barely-different way of walking the northern leg
    connect(s, detour, minutes=7.5)
    connect(detour, a, minutes=7.5)

    best = [s, a, p, b, s]
    clone = [s, detour, a, p, b, s]  # same walk, one segment split in two
    distinct = [s, c, p, c, s]

    candidates = [
        (1.0, best, p, 0.0),
        (1.1, clone, p, 0.0),
        (2.0, distinct, p, 0.5),
    ]
    kept = _select_diverse_candidates(g, candidates, limit=3, max_similarity=0.6)
    paths = [item[1] for item in kept]

    assert best in paths
    assert distinct in paths
    # the near-clone may be backfilled to fill the batch, but never ahead of the
    # genuinely different loop
    assert paths.index(best) < paths.index(distinct) or clone not in paths[:2]
    assert _select_diverse_candidates(g, candidates, limit=2, max_similarity=0.6) == [
        (1.0, best, p, 0.0),
        (2.0, distinct, p, 0.5),
    ]


def test_order_by_diversity_promotes_distinct_loop():
    from router import RouteCandidate, RouteStats, _order_by_diversity

    g, connect = _diversity_graph()
    s, a, b, p = (0.0, 0.0), (1000.0, 500.0), (1000.0, -500.0), (2000.0, 0.0)
    detour = (1000.0, 600.0)
    c = (1000.0, 1500.0)
    connect(s, a)
    connect(a, p)
    connect(s, b)
    connect(b, p)
    connect(s, c)
    connect(c, p)
    connect(s, detour, minutes=7.5)
    connect(detour, a, minutes=7.5)

    def candidate(nodes, score):
        return RouteCandidate(
            nodes=nodes,
            stats=RouteStats(
                distance_km=1.0,
                ascent_m=0.0,
                descent_m=0.0,
                duration_minutes=60.0,
                max_sustained_grade_percent=0.0,
                p95_grade_percent=0.0,
                official_share_percent=100.0,
            ),
            score=score,
            pivot=p,
        )

    best = candidate([s, a, p, b, s], 1.0)
    clone = candidate([s, detour, a, p, b, s], 1.1)
    distinct = candidate([s, c, p, c, s], 2.0)

    ordered = _order_by_diversity(g, [best, clone, distinct], diversity_weight=1.5, duplicate_similarity=0.9)
    assert ordered[0] is best
    assert ordered[1] is distinct  # promoted past its better-scoring near-clone
    assert ordered[2] is clone  # kept, not discarded
    assert len(ordered) == 3

    # Novelty is a surcharge, not a veto: a loop that badly misses the requested
    # duration must not be promoted over a near-duplicate that answers the request.
    way_off = candidate([s, c, p, c, s], 12.0)
    ordered = _order_by_diversity(g, [best, clone, way_off], diversity_weight=1.5, duplicate_similarity=0.9)
    assert ordered[1] is clone
    assert ordered[2] is way_off

    # An outright repeat is dropped rather than listed a second time.
    twin = candidate(list(best.nodes), 1.05)
    ordered = _order_by_diversity(
        g, [best, twin, distinct], diversity_weight=1.5, duplicate_similarity=0.9
    )
    assert ordered == [best, distinct]


def test_loop_generator_finds_two_path_loop(monkeypatch):
    import router

    g = nx.Graph()
    s = (0.0, 0.0)
    a = (1000.0, 500.0)
    b = (1000.0, -500.0)
    p = (2000.0, 0.0)

    attrs = dict(
        length=1000.0,
        ascent=0.0,
        descent=0.0,
        walk_minutes=15.0,
        category=CATEGORY_WANDERWEG,
        official=True,
        routing_cost=15.0,
    )
    g.add_edge(s, a, **attrs)
    g.add_edge(a, p, **attrs)
    g.add_edge(s, b, **attrs)
    g.add_edge(b, p, **attrs)

    def fake_profile(nodes, session=None, spacing_m=20.0):
        return [
            {"dist": 0.0, "easting": 0.0, "northing": 0.0, "alts": {"COMB": 1000.0}},
            {"dist": 2000.0, "easting": 2000.0, "northing": 0.0, "alts": {"COMB": 1000.0}},
            {"dist": 4000.0, "easting": 0.0, "northing": 0.0, "alts": {"COMB": 1000.0}},
        ]

    monkeypatch.setattr(router, "fetch_elevation_profile", fake_profile)
    config = router.PlannerConfig(
        duration_hours=1.0,
        candidate_pivots=4,
        alternate_paths_per_pivot=3,
        profile_candidates=4,
        max_repeated_share=0.1,
    )
    result = router.generate_loop_candidates(g, s, (3000.0, 0.0), config)
    assert result
    assert math.isclose(result[0].stats.distance_km, 4.0)
    assert result[0].stats.repeated_share_percent == 0.0
    assert result[0].stats.official_share_percent == 100.0


def test_traffic_classification_by_width_and_restriction():
    from router import (
        TRAFFIC_CALMED,
        TRAFFIC_FREE,
        TRAFFIC_MAJOR,
        TRAFFIC_ROAD,
        TRAFFIC_UNKNOWN,
        normalize_traffic_class,
    )

    # Narrow paths carry no cars.
    assert normalize_traffic_class("1m Weg") == TRAFFIC_FREE
    assert normalize_traffic_class("2m Weg") == TRAFFIC_FREE
    assert normalize_traffic_class("Markierte Spur") == TRAFFIC_FREE

    # Road width means shared with cars, wider means worse.
    assert normalize_traffic_class("3m Strasse") == TRAFFIC_ROAD
    assert normalize_traffic_class("4m Strasse") == TRAFFIC_ROAD
    assert normalize_traffic_class("6m Strasse") == TRAFFIC_MAJOR
    assert normalize_traffic_class("10m Strasse") == TRAFFIC_MAJOR

    # A driving ban or an undrivable record downgrades a road to calmed.
    assert normalize_traffic_class("4m Strasse", "Allgemeines Fahrverbot") == TRAFFIC_CALMED
    assert normalize_traffic_class("4m Strasse", "Allgemeine Verkehrsbeschraenkung") == TRAFFIC_CALMED
    assert normalize_traffic_class("6m Strasse", None, "Falsch") == TRAFFIC_CALMED

    # An expressway stays dangerous no matter what else the record says.
    assert normalize_traffic_class(
        "4m Strasse", "Allgemeines Fahrverbot", "Falsch", "Hochleistungsstrasse"
    ) == TRAFFIC_MAJOR

    # No road attributes: say unknown rather than pretend it is traffic-free.
    assert normalize_traffic_class(None) == TRAFFIC_UNKNOWN
    assert normalize_traffic_class("k_W") == TRAFFIC_UNKNOWN


def test_path_stats_reports_car_road_share():
    from router import TRAFFIC_FREE, TRAFFIC_MAJOR, TRAFFIC_ROAD, car_road_spans

    g = nx.Graph()
    a, b, c, d = (0.0, 0.0), (100.0, 0.0), (200.0, 0.0), (300.0, 0.0)

    def link(u, v, traffic):
        g.add_edge(
            u, v, length=100.0, ascent=0.0, descent=0.0, walk_minutes=1.5,
            official=True, traffic=traffic, car_road=traffic in {TRAFFIC_ROAD, TRAFFIC_MAJOR},
        )

    link(a, b, TRAFFIC_FREE)
    link(b, c, TRAFFIC_ROAD)
    link(c, d, TRAFFIC_MAJOR)

    stats = path_graph_stats(g, [a, b, c, d])
    assert stats["car_road_share_percent"] == pytest.approx(200.0 / 300.0 * 100.0)
    assert stats["car_road_m"] == 200.0
    assert stats["major_road_m"] == 100.0

    # The two road edges are adjacent, so they surface as one drawable span.
    assert car_road_spans(g, [a, b, c, d]) == [[b, c, d]]


def test_car_road_tolerance_rejects_road_heavy_loops():
    import router

    g = nx.Graph()
    s, north, south, p = (0.0, 0.0), (1000.0, 500.0), (1000.0, -500.0), (2000.0, 0.0)

    def link(u, v, objektart):
        traffic = router.normalize_traffic_class(objektart)
        g.add_edge(
            u, v, length=1000.0, ascent=0.0, descent=0.0, walk_minutes=15.0,
            category=CATEGORY_WANDERWEG, official=True, routing_cost=15.0,
            traffic=traffic, car_road=traffic in router.TRAFFIC_WITH_CARS,
        )

    # The only loop available runs half on a road open to cars.
    link(s, north, "1m Weg")
    link(north, p, "1m Weg")
    link(s, south, "4m Strasse")
    link(south, p, "4m Strasse")

    def fake_profile(nodes, session=None, spacing_m=20.0):
        return [
            {"dist": 0.0, "easting": 0.0, "northing": 0.0, "alts": {"COMB": 1000.0}},
            {"dist": 4000.0, "easting": 0.0, "northing": 0.0, "alts": {"COMB": 1000.0}},
        ]

    base = dict(
        duration_hours=1.0, candidate_pivots=4, alternate_paths_per_pivot=3,
        profile_candidates=4, max_repeated_share=0.1,
    )

    strict = router.PlannerConfig(max_car_road_share=0.10, **base)
    expected = re.escape(message_fragment("errors.loops_too_much_road"))
    with pytest.raises(router.HikingPlannerError, match=expected):
        router.generate_loop_candidates(g, s, (3000.0, 0.0), strict)

    # With the tolerance raised the same loop is offered, and reports its road share.
    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(router, "fetch_elevation_profile", fake_profile)
    try:
        lenient = router.PlannerConfig(max_car_road_share=0.60, **base)
        result = router.generate_loop_candidates(g, s, (3000.0, 0.0), lenient)
        assert result
        assert result[0].stats.car_road_share_percent == pytest.approx(50.0)
        assert result[0].road_spans
    finally:
        monkey.undo()


def test_gravel_road_counts_as_traffic_calmed():
    from router import TRAFFIC_CALMED, TRAFFIC_ROAD, normalize_traffic_class

    # A paved 3 m lane is a road cars use; the same width in gravel is a farm track.
    assert normalize_traffic_class("3m Strasse", belagsart="Hart") == TRAFFIC_ROAD
    assert normalize_traffic_class("3m Strasse", belagsart="Natur") == TRAFFIC_CALMED


def test_herding_dog_areas_are_cut_out_of_the_network(tmp_path):
    """A trail crossing a guarded pasture keeps only the part outside it."""
    import geopandas as gpd
    from shapely.geometry import LineString, Polygon
    from router import CATEGORY_WANDERWEG, herding_dog_exclusion, load_hiking_lines

    pasture = Polygon([(2600500, 1200000), (2601500, 1200000), (2601500, 1201000), (2600500, 1201000)])
    areas = [{"geometry": pasture, "name": "Alp Test"}]

    exclusion = herding_dog_exclusion(areas, buffer_m=0.0)
    assert exclusion is not None
    # The buffer widens the no-go zone rather than shrinking it.
    assert herding_dog_exclusion(areas, buffer_m=50.0).area > exclusion.area

    # A line running straight through the pasture, written to a real GeoPackage.
    crossing = LineString([(2600000, 1200500), (2602000, 1200500)])
    gdf = gpd.GeoDataFrame(
        {"objektart": ["1m Weg"], "wanderwege": [CATEGORY_WANDERWEG]},
        geometry=[crossing],
        crs=2056,
    )
    gpkg = tmp_path / "trails.gpkg"
    gdf.to_file(gpkg, layer="trails", driver="GPKG")
    bbox = (2599000, 1199000, 2603000, 1202000)

    without = load_hiking_lines(gpkg, "trails", bbox)
    assert without.geometry.length.sum() == pytest.approx(2000.0)

    with_ban = load_hiking_lines(gpkg, "trails", bbox, exclusion=exclusion)
    remaining = with_ban.geometry.length.sum()
    # 2 km of trail, 1 km of it inside the pasture: only the outside half survives.
    assert remaining == pytest.approx(1000.0)
    assert not with_ban.geometry.intersects(pasture.buffer(-1)).any()


def test_herding_area_geojson_reprojects_to_wgs84():
    from shapely.geometry import Polygon
    from router import herding_area_geojson

    pasture = Polygon([(2708738, 1183224), (2709738, 1183224), (2709738, 1184224), (2708738, 1184224)])
    fc = herding_area_geojson([{"geometry": pasture, "name": "Alp Cavrein", "url": "https://example.ch"}])

    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1
    feature = fc["features"][0]
    assert feature["properties"]["name"] == "Alp Cavrein"
    lon, lat = feature["geometry"]["coordinates"][0][0]
    # Somewhere in Graubünden, in degrees rather than metres.
    assert 8.5 < lon < 9.5 and 46.0 < lat < 47.0


def _corridor_graph_gpkg(tmp_path):
    """Two parallel trails between the same endpoints, so alternatives exist."""
    import geopandas as gpd
    from shapely.geometry import LineString

    north = LineString([(2600000, 1200000), (2601000, 1200400), (2602000, 1200000)])
    south = LineString([(2600000, 1200000), (2601000, 1199600), (2602000, 1200000)])
    gdf = gpd.GeoDataFrame(
        {"objektart": ["1m Weg", "1m Weg"], "wanderwege": ["Wanderweg", "Wanderweg"]},
        geometry=[north, south],
        crs=2056,
    )
    gpkg = tmp_path / "corridor.gpkg"
    gdf.to_file(gpkg, layer="trails", driver="GPKG")
    return gpkg


def test_point_to_point_route_reaches_the_destination(tmp_path, monkeypatch):
    import router
    from router import lv95_to_lonlat

    gpkg = _corridor_graph_gpkg(tmp_path)

    def fake_profile(nodes, session=None, spacing_m=20.0):
        return [
            {"dist": 0.0, "easting": 2600000.0, "northing": 1200000.0, "alts": {"COMB": 1000.0}},
            {"dist": 2000.0, "easting": 2602000.0, "northing": 1200000.0, "alts": {"COMB": 1000.0}},
        ]

    monkeypatch.setattr(router, "fetch_elevation_profile", fake_profile)

    start = tuple(reversed(lv95_to_lonlat((2600000, 1200000))))
    end = tuple(reversed(lv95_to_lonlat((2602000, 1200000))))
    config = router.PlannerConfig(avoid_herding_dogs=False)

    routes, _ = router.plan_point_to_point(gpkg, "trails", start, end, config)
    assert routes
    # A one-way route starts at the start and ends at the destination, not back home.
    first, last = routes[0].nodes[0], routes[0].nodes[-1]
    assert math.hypot(first[0] - 2600000, first[1] - 1200000) < 30
    assert math.hypot(last[0] - 2602000, last[1] - 1200000) < 30
    # Both corridors are offered rather than the same line twice.
    assert len(routes) >= 2


def test_point_to_point_visits_its_waypoint(tmp_path, monkeypatch):
    import router
    from router import lv95_to_lonlat

    gpkg = _corridor_graph_gpkg(tmp_path)
    monkeypatch.setattr(
        router, "fetch_elevation_profile",
        lambda nodes, session=None, spacing_m=20.0: [
            {"dist": 0.0, "easting": 2600000.0, "northing": 1200000.0, "alts": {"COMB": 1000.0}},
            {"dist": 2000.0, "easting": 2602000.0, "northing": 1200000.0, "alts": {"COMB": 1000.0}},
        ],
    )

    start = tuple(reversed(lv95_to_lonlat((2600000, 1200000))))
    end = tuple(reversed(lv95_to_lonlat((2602000, 1200000))))
    via_south = tuple(reversed(lv95_to_lonlat((2601000, 1199600))))
    config = router.PlannerConfig(avoid_herding_dogs=False)

    routes, _ = router.plan_point_to_point(
        gpkg, "trails", start, end, config, waypoints_latlon=[via_south]
    )
    assert routes
    # The southern bend must appear in the route that was told to go via it.
    assert any(
        math.hypot(n[0] - 2601000, n[1] - 1199600) < 30 for n in routes[0].nodes
    )
