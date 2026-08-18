from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import altair as alt
import folium
import pandas as pd
import requests
import streamlit as st
from streamlit_folium import st_folium

from i18n import t
from router import (
    HikingPlannerError,
    LocationResult,
    PlannerConfig,
    candidate_geojson,
    candidate_gpx,
    compute_reference_grade,
    download_and_extract_dataset,
    fetch_herding_dog_areas,
    herding_area_geojson,
    latlon_to_lv95,
    lv95_to_lonlat,
    plan_loops,
    plan_point_to_point,
    route_latlon,
    search_locations,
    select_line_layer,
)


st.set_page_config(
    page_title=t("app.page_title"),
    page_icon="🥾",
    layout="wide",
)

def resolve_cache_dir() -> Path:
    """Where the swissTLM3D extract lives.

    Docker Compose mounts a volume at /data; a managed host such as Streamlit Cloud has
    no such volume, so fall back to the home cache. Anything explicit wins over both.
    """
    explicit = os.environ.get("HIKING_CACHE_DIR")
    if explicit:
        return Path(explicit)
    docker_volume = Path("/data")
    if docker_volume.is_dir() and os.access(docker_volume, os.W_OK):
        return docker_volume / "cache"
    return Path.home() / ".cache" / "swiss-hiking-planner"


CACHE_DIR = resolve_cache_dir()


@st.cache_data(ttl=600, show_spinner=False)
def cached_search(query: str) -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": "swiss-hiking-planner-gui/2.0"})
    return [r.__dict__ for r in search_locations(query, limit=10, session=session)]


@st.cache_resource(show_spinner=False)
def cached_dataset(cache_path: str, refresh_counter: int) -> tuple[str, str]:
    gpkg = download_and_extract_dataset(Path(cache_path), refresh=refresh_counter > 0)
    layer = select_line_layer(gpkg)
    return str(gpkg), layer


@st.cache_data(ttl=3600, show_spinner=False)
def cached_herding_areas(bbox: tuple[float, float, float, float]) -> list[dict]:
    """Guarded pastures for a bbox, as GeoJSON-ready dicts.

    Shapely geometry does not survive Streamlit's cache serialisation, so the cached
    value keeps GeoJSON and the shapes are rebuilt from it on use.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "swiss-hiking-planner-gui/2.0"})
    areas = fetch_herding_dog_areas(bbox, session=session)
    collection = herding_area_geojson(areas)
    return [
        {**feature["properties"], "geojson": feature["geometry"]}
        for feature in collection["features"]
    ]


def herding_areas_lv95(cached: list[dict]) -> list[dict]:
    """Rebuild LV95 shapes from the cached WGS84 GeoJSON, for the router."""
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import transform as shapely_transform

    rebuilt = []
    for area in cached:
        wgs84 = shapely_shape(area["geojson"])
        lv95 = shapely_transform(
            lambda x, y, z=None: latlon_to_lv95((y, x)), wgs84
        )
        rebuilt.append({**area, "geometry": lv95})
    return rebuilt


PICK_ROLES = ("Start", "Destination", "Waypoint")


def next_pick_role(points: dict, roles: list[str]) -> str:
    """What the next click should place: the first thing still missing."""
    if not points.get("start"):
        return "Start"
    if not points.get("destination"):
        return "Destination"
    return "Waypoint" if "Waypoint" in roles else "Destination"


def apply_pick(points: dict, role: str, point: tuple[float, float] | None) -> dict:
    """Fold a map click into the chosen points.

    Kept free of Streamlit so the rules are testable: a click on Start or Destination
    replaces that point, a click in Waypoint mode appends, and st_folium replaying the
    same click on a later rerun must not add the same waypoint twice.
    """
    updated = {
        "start": points.get("start"),
        "destination": points.get("destination"),
        "waypoints": list(points.get("waypoints") or []),
        "last": points.get("last"),
    }
    if point is None:
        return updated
    if updated["last"] == (role, point):
        return updated

    updated["last"] = (role, point)
    if role == "Waypoint":
        updated["waypoints"].append(point)
    elif role == "Start":
        updated["start"] = point
    elif role == "Destination":
        updated["destination"] = point
    return updated


def picked(role: str):
    """Points chosen on the map. Start/Destination hold one, Waypoint holds a list."""
    if role == "Waypoint":
        return st.session_state.setdefault("pick_waypoints", [])
    return st.session_state.get(f"pick_{role.lower()}")


def map_point_picker(loop_mode: bool, herding_areas=None) -> None:
    """Click the map to place the start, the destination and any waypoints."""
    roles = ["Start", "Destination", "Waypoint"] if not loop_mode else ["Start", "Destination"]
    labels = {
        "Start": t("points.map.role_start"),
        "Destination": t("points.map.role_direction") if loop_mode else t("points.map.role_destination"),
        "Waypoint": t("points.map.role_waypoint"),
    }

    pending = st.session_state.pop("pick_role_next", None)
    if pending in roles:
        st.session_state["pick_role"] = pending

    start, dest = picked("Start"), picked("Destination")
    second = labels["Destination"].lower()
    if not start:
        st.info(t("points.map.step1", second=second))
    elif not dest:
        st.info(t("points.map.step2_loop" if loop_mode else "points.map.step2_oneway", second=second))
    else:
        st.success(
            t("points.map.ready",
              second=second,
              button=t("actions.generate_loops") if loop_mode else t("actions.plan_route"))
            + ("" if loop_mode else t("points.map.ready_waypoint_hint"))
        )

    controls, actions = st.columns([3, 1])
    with controls:
        role = st.radio(
            t("points.map.next_click"),
            roles,
            format_func=lambda r: labels[r],
            horizontal=True,
            key="pick_role",
            help=t("points.map.next_click_help"),
        )
    with actions:
        if st.button(t("points.map.undo"), use_container_width=True, disabled=not picked("Waypoint")):
            st.session_state["pick_waypoints"].pop()
            st.rerun()
        if st.button(t("points.map.clear"), use_container_width=True):
            for key in ("pick_start", "pick_destination", "pick_waypoints", "pick_last_click"):
                st.session_state.pop(key, None)
            st.session_state["pick_role_next"] = "Start"
            st.rerun()

    waypoints = picked("Waypoint")
    centre = start or dest or (46.6961, 8.8278)

    # Guarded pastures matter most while choosing where to start, so fetch them for the
    # area on view rather than waiting until a route has been calculated.
    if herding_areas is None:
        cx, cy = latlon_to_lv95(centre)
        span = 15000.0
        try:
            herding_areas = cached_herding_areas((cx - span, cy - span, cx + span, cy + span))
        except Exception as exc:
            herding_areas = []
            st.caption(t("points.map.pastures_failed", error=exc))

    m = folium.Map(location=list(centre), zoom_start=12, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="© swisstopo",
        name=t("map.layer_swisstopo"),
        max_zoom=18,
    ).add_to(m)
    add_herding_dog_layer(m, herding_areas)

    def pin(point, colour, label):
        folium.CircleMarker(
            location=list(point),
            radius=9,
            color="#ffffff",
            weight=3,
            fill=True,
            fill_color=colour,
            fill_opacity=1.0,
            tooltip=label,
        ).add_to(m)

    if start:
        pin(start, "#2ca02c", t("map.tooltip_start"))
    if dest:
        pin(dest, "#d62728", labels["Destination"])
    for i, point in enumerate(waypoints, start=1):
        pin(point, "#1f77b4", t("map.tooltip_waypoint", number=i))

    placed = [p for p in ([start] + list(waypoints) + [dest]) if p]
    if len(placed) > 1:
        folium.PolyLine(placed, weight=2, opacity=0.6, dash_array="6,8", color="#888888").add_to(m)
        m.fit_bounds(placed)

    # In Streamlit's wide layout st_folium only reports a height when it sizes itself
    # to the container; passing an explicit pixel width yields a zero-height iframe.
    event = st_folium(
        m,
        height=420,
        use_container_width=True,
        returned_objects=["last_clicked"],
        key="picker_map",
    )

    click = (event or {}).get("last_clicked")
    if not click:
        return
    point = (round(float(click["lat"]), 6), round(float(click["lng"]), 6))
    before = {
        "start": picked("Start"),
        "destination": picked("Destination"),
        "waypoints": list(picked("Waypoint")),
        "last": st.session_state.get("pick_last_click"),
    }
    after = apply_pick(before, role, point)
    if after == before:
        return
    st.session_state["pick_start"] = after["start"]
    st.session_state["pick_destination"] = after["destination"]
    st.session_state["pick_waypoints"] = after["waypoints"]
    st.session_state["pick_last_click"] = after["last"]
    # Hand the next role to the following run rather than writing "pick_role" here:
    # the radio already exists by now, and Streamlit rejects writes to a live widget key.
    st.session_state["pick_role_next"] = next_pick_role(after, roles)
    st.rerun()


def waypoint_editor() -> None:
    """Type waypoints as coordinates.

    The map is the pleasant way to add them, but it needs Leaflet from a CDN. On a
    restricted network that never loads, and waypoints would be unreachable with no
    other way in.
    """
    current = picked("Waypoint")
    with st.expander(t("points.waypoints.expander", count=len(current)), expanded=bool(current)):
        for i, point in enumerate(current, start=1):
            st.caption(t("points.waypoints.listed", index=i, lat=f"{point[0]:.5f}", lon=f"{point[1]:.5f}"))
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            lat = st.number_input(t("points.waypoints.latitude"), value=46.7000000, format="%.7f", key="wp_lat")
        with c2:
            lon = st.number_input(t("points.waypoints.longitude"), value=8.8500000, format="%.7f", key="wp_lon")
        with c3:
            st.caption("")
            if st.button(t("points.waypoints.add"), use_container_width=True, key="wp_add"):
                st.session_state.setdefault("pick_waypoints", []).append(
                    (round(float(lat), 6), round(float(lon), 6))
                )
                st.rerun()
        if current and st.button(t("points.waypoints.remove_all"), key="wp_clear"):
            st.session_state["pick_waypoints"] = []
            st.rerun()


def location_picker(label: str, default_query: str, key: str, manual_default: tuple[float, float] = (46.70, 8.85)) -> tuple[tuple[float, float] | None, str]:
    query = st.text_input(label, value=default_query, key=f"{key}_query")
    manual = st.toggle(t("points.search.manual_toggle"), value=False, key=f"{key}_manual")

    if manual:
        c1, c2 = st.columns(2)
        with c1:
            lat = st.number_input(t("points.search.latitude"), value=float(manual_default[0]), format="%.7f", key=f"{key}_lat")
        with c2:
            lon = st.number_input(t("points.search.longitude"), value=float(manual_default[1]), format="%.7f", key=f"{key}_lon")
        return (float(lat), float(lon)), f"{lat:.6f}, {lon:.6f}"

    if not query.strip():
        st.caption(t("points.search.enter_place"))
        return None, ""

    try:
        raw = cached_search(query.strip())
        results = [LocationResult(**x) for x in raw]
    except Exception as exc:
        st.error(t("errors.search_failed", error=exc))
        return None, ""

    if not results:
        st.warning(t("points.search.no_result"))
        return None, ""

    options = {f"{r.display}  [{r.lat:.5f}, {r.lon:.5f}]": r for r in results}
    selected_label = st.selectbox(t("points.search.resolved"), list(options.keys()), key=f"{key}_select")
    selected = options[selected_label]
    return selected.latlon, selected.display


def profile_frame(profile: list[dict] | None) -> pd.DataFrame | None:
    """Profile points with their map position, so a point on the chart can be located."""
    if not profile:
        return None
    rows = []
    for p in profile:
        alts = p.get("alts") or {}
        z = alts.get("COMB")
        if z is None:
            z = alts.get("DTM2")
        if z is None:
            z = alts.get("DTM25")
        if z is None or p.get("dist") is None:
            continue
        easting, northing = p.get("easting"), p.get("northing")
        if easting is None or northing is None:
            continue
        lon, lat = lv95_to_lonlat((float(easting), float(northing)))
        rows.append(
            {
                "km": float(p["dist"]) / 1000.0,
                "elevation": float(z),
                "lat": lat,
                "lon": lon,
            }
        )
    return pd.DataFrame(rows) if rows else None


def elevation_chart(frame: pd.DataFrame, marker_km: float | None) -> alt.Chart:
    """Elevation profile scaled to its own range, so the terrain fills the plot.

    A domain anchored at sea level would draw a Swiss valley walk as a flat line in
    the top eighth of the chart.
    """
    low = float(frame["elevation"].min())
    high = float(frame["elevation"].max())
    padding = max((high - low) * 0.12, 5.0)
    domain = [low - padding, high + padding]

    x = alt.X("km:Q", title=t("results.profile_axis_distance"), scale=alt.Scale(nice=False, domain=[0, float(frame["km"].max())]))
    y = alt.Y("elevation:Q", title=t("results.profile_axis_elevation"), scale=alt.Scale(domain=domain, nice=False, clamp=True))

    base = alt.Chart(frame)
    area = base.mark_area(
        line={"color": "#4c9be8", "strokeWidth": 2},
        color=alt.Gradient(
            gradient="linear",
            stops=[
                alt.GradientStop(color="#4c9be8", offset=1),
                alt.GradientStop(color="#4c9be8", offset=0),
            ],
            x1=1, x2=1, y1=1, y2=0,
        ),
        opacity=0.35,
    ).encode(x=x, y=y)

    picker = alt.selection_point(
        name="pick",
        encodings=["x"],
        nearest=True,
        on="click",
        clear=False,
    )
    # Invisible wide hit area: clicking anywhere selects the nearest profile point.
    hits = base.mark_point(size=120, opacity=0).encode(x=x, y=y).add_params(picker)

    layers = [area, hits]
    if marker_km is not None:
        picked = frame.iloc[(frame["km"] - marker_km).abs().argmin()]
        highlight = pd.DataFrame([{"km": float(picked["km"]), "elevation": float(picked["elevation"])}])
        layers.append(alt.Chart(highlight).mark_rule(color="#ff2b2b", strokeWidth=2).encode(x=x))
        layers.append(
            alt.Chart(highlight)
            .mark_point(color="#ff2b2b", size=140, filled=True)
            .encode(x=x, y=y)
        )

    return alt.layer(*layers).properties(height=240)


def picked_km(event) -> float | None:
    """Read the clicked distance out of a Vega point selection."""
    selection = getattr(event, "selection", None) or {}
    picks = selection.get("pick") if isinstance(selection, dict) else None
    if not picks:
        return None
    first = picks[0]
    if isinstance(first, dict):
        for key in ("km", "km:Q"):
            if key in first:
                return float(first[key])
        for value in first.values():
            if isinstance(value, (int, float)):
                return float(value)
    elif isinstance(first, (int, float)):
        return float(first)
    return None


def add_herding_dog_layer(m, cached_areas):
    """Draw the guarded pastures so it is visible why a route detours around them."""
    if not cached_areas:
        return
    group = folium.FeatureGroup(name=t("map.layer_pastures"), show=True)
    for area in cached_areas:
        contact = " · ".join(
            str(v) for v in (area.get("contact_name"), area.get("contact_phone"), area.get("contact_email")) if v
        )
        popup = t("map.pasture_popup", name=area.get("name", "Alpweide"))
        if contact:
            popup += "<br>" + t("map.pasture_contact", contact=contact)
        if area.get("url"):
            popup += f"<br><a href='{area['url']}' target='_blank'>{t('map.pasture_link')}</a>"
        folium.GeoJson(
            area["geojson"],
            style_function=lambda _: {
                "fillColor": "#ff7f0e",
                "color": "#ff7f0e",
                "weight": 2,
                "fillOpacity": 0.30,
                "dashArray": "5,5",
            },
            tooltip=t("map.pasture_tooltip", name=area.get("name", "Alpweide")),
            popup=folium.Popup(popup, max_width=320),
        ).add_to(group)
    group.add_to(m)


def route_map(candidate, start_latlon, direction_latlon, frame=None, marker_km=None,
              herding_areas=None, waypoints=(), loop=True):
    route = route_latlon(candidate.nodes)
    m = folium.Map(location=list(start_latlon), zoom_start=13, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="© swisstopo",
        name=t("map.layer_swisstopo"),
        max_zoom=18,
    ).add_to(m)
    add_herding_dog_layer(m, herding_areas)
    folium.PolyLine(route, weight=6, opacity=0.9, tooltip=t("map.tooltip_route")).add_to(m)

    road_spans = getattr(candidate, "road_spans", None) or []
    if road_spans:
        roads = folium.FeatureGroup(name=t("results.metric_cars"), show=True)
        for span in road_spans:
            folium.PolyLine(
                route_latlon(span),
                weight=7,
                opacity=0.95,
                color="#d62728",
                tooltip=t("map.tooltip_road"),
            ).add_to(roads)
        roads.add_to(m)

    if frame is not None and not frame.empty:
        ticks = folium.FeatureGroup(name=t("map.layer_km"), show=True)
        for km in range(1, int(frame["km"].max()) + 1):
            point = frame.iloc[(frame["km"] - km).abs().argmin()]
            folium.CircleMarker(
                location=[float(point["lat"]), float(point["lon"])],
                radius=4,
                color="#ffffff",
                weight=2,
                fill=True,
                fill_color="#1f77b4",
                fill_opacity=1.0,
                tooltip=t("map.tooltip_km", number=km, elevation=f"{float(point['elevation']):.0f}"),
            ).add_to(ticks)
        ticks.add_to(m)

    folium.Marker(
        start_latlon, tooltip=t("map.tooltip_start"), icon=folium.Icon(color="green", icon="home", prefix="fa")
    ).add_to(m)
    for i, point in enumerate(waypoints or (), start=1):
        folium.Marker(
            point, tooltip=t("map.tooltip_waypoint", number=i), icon=folium.Icon(color="blue", icon="location-dot", prefix="fa")
        ).add_to(m)
    folium.Marker(
        direction_latlon,
        tooltip=t("map.tooltip_direction") if loop else t("map.tooltip_destination"),
        icon=folium.Icon(color="red", icon="compass" if loop else "flag-checkered", prefix="fa"),
    ).add_to(m)

    if frame is not None and marker_km is not None and not frame.empty:
        point = frame.iloc[(frame["km"] - marker_km).abs().argmin()]
        folium.CircleMarker(
            location=[float(point["lat"]), float(point["lon"])],
            radius=10,
            color="#ff2b2b",
            weight=3,
            fill=True,
            fill_color="#ff2b2b",
            fill_opacity=0.9,
            tooltip=f"{float(point['km']):.2f} km · {float(point['elevation']):.0f} m",
        ).add_to(m)

    if route:
        m.fit_bounds(route)
    folium.LayerControl().add_to(m)
    return m


def zip_candidates(candidates) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, c in enumerate(candidates, start=1):
            name = f"route_{i}"
            zf.writestr(f"{name}.gpx", candidate_gpx(c, t("results.export_track_name", number=i)))
            zf.writestr(f"{name}.geojson", candidate_geojson(c, t("results.export_track_name", number=i)))
    return buffer.getvalue()


st.title(t("app.title"))
st.caption(t("app.tagline"))

with st.sidebar:
    st.header(t("sidebar.route_target.header"))
    loop_mode = st.checkbox(
        t("sidebar.route_target.loop"),
        value=True,
        help=t("sidebar.route_target.loop_help"),
    )
    duration = st.slider(
        t("sidebar.route_target.duration"),
        min_value=1.0,
        max_value=7.0,
        value=3.0,
        step=0.25,
        format="%.2f h",
        disabled=not loop_mode,
        help=(
            t("sidebar.route_target.duration_help_loop")
            if loop_mode
            else t("sidebar.route_target.duration_help_oneway")
        ),
    )
    alpine_forbidden = st.checkbox(
        t("sidebar.route_target.alpine"),
        value=True,
        help=(
            t("sidebar.route_target.alpine_help")
        ),
    )
    if not alpine_forbidden:
        st.warning(t("sidebar.route_target.alpine_warning"))
    st.caption(t("sidebar.route_target.duration_caption"))

    st.header(t("sidebar.roads_dogs.header"))
    avoid_herding_dogs = st.checkbox(
        t("sidebar.roads_dogs.avoid_dogs"),
        value=True,
        help=(
            t("sidebar.roads_dogs.avoid_dogs_help")
        ),
    )
    road_tolerance_pct = st.slider(
        t("sidebar.roads_dogs.road_share"),
        min_value=0,
        max_value=40,
        value=20,
        step=1,
        format="%d%%",
        help=(
            t("sidebar.roads_dogs.road_share_help")
        ),
    )
    herding_margin_m = st.slider(
        t("sidebar.roads_dogs.margin"),
        min_value=0,
        max_value=200,
        value=0,
        step=10,
        format="%d m",
        disabled=not avoid_herding_dogs,
        help=(
            t("sidebar.roads_dogs.margin_help")
        ),
    )
    st.caption(t("sidebar.roads_dogs.caption"))

    st.header(t("sidebar.steepness.header"))
    use_reference = st.checkbox(t("sidebar.steepness.limit"), value=True)

    with st.expander(t("sidebar.advanced.header"), expanded=False):
        other_penalty = st.slider(
            t("sidebar.advanced.other_penalty"),
            min_value=1.0,
            max_value=20.0,
            value=8.0,
            step=0.5,
            help=t("sidebar.advanced.other_penalty_help"),
        )
        steepness_tolerance_pct = st.slider(
            t("sidebar.advanced.steepness_tolerance"),
            min_value=0,
            max_value=25,
            value=5,
            step=1,
        )
        direction_cone = st.slider(t("sidebar.advanced.direction_cone"), 30, 120, 75, 5, format="%d°")
        repeated_pct = st.slider(t("sidebar.advanced.repeated"), 5, 50, 28, 1, format="%d%%")
        candidate_pivots = st.slider(t("sidebar.advanced.pivots"), 6, 48, 28, 1)
        duration_tolerance_pct = st.slider(
            t("sidebar.advanced.duration_tolerance"),
            min_value=5,
            max_value=50,
            value=30,
            step=5,
            format="%d%%",
            help=t("sidebar.advanced.duration_tolerance_help"),
        )
        route_count = st.slider(t("sidebar.advanced.route_count"), 1, 10, 7, 1)

    if "refresh_counter" not in st.session_state:
        st.session_state.refresh_counter = 0
    if st.button(t("sidebar.dataset.refresh_button")):
        st.session_state.refresh_counter += 1
        cached_dataset.clear()
        st.success(t("sidebar.dataset.refresh_done"))

waypoints: list[tuple[float, float]] = []

st.subheader(t("points.heading_oneway") if not loop_mode else t("points.heading_loop"))
input_mode = st.radio(
    t("points.choose_by"),
    ["Search", "Map"],
    format_func=lambda m: t("points.mode_search") if m == "Search" else t("points.mode_map"),
    horizontal=True,
    key="input_mode",
    help=t("points.choose_by_help"),
)

if input_mode == "Map":
    map_point_picker(loop_mode, herding_areas=st.session_state.get("herding_areas"))
    start_latlon = picked("Start")
    direction_latlon = picked("Destination")
    waypoints = list(picked("Waypoint")) if not loop_mode else []
    start_name = f"{start_latlon[0]:.5f}, {start_latlon[1]:.5f}" if start_latlon else ""
    direction_name = f"{direction_latlon[0]:.5f}, {direction_latlon[1]:.5f}" if direction_latlon else ""

    placed = []
    if start_latlon:
        placed.append(t("points.map.summary_start", coords=start_name))
    if waypoints:
        placed.append(t("points.map.summary_waypoints", count=len(waypoints)))
    if direction_latlon:
        placed.append(t("points.map.summary_destination" if not loop_mode else "points.map.summary_direction", coords=direction_name))
    st.caption(" · ".join(placed) if placed else t("points.map.empty_hint"))
    if not loop_mode:
        waypoint_editor()
else:
    left, right = st.columns(2)
    with left:
        start_latlon, start_name = location_picker(
            "Start", "Lagerhaus Alpina Segnas", "start", manual_default=(46.6961, 8.8278)
        )
    with right:
        direction_latlon, direction_name = location_picker(
            "Destination" if not loop_mode else "Direction", "Disentis/Mustér", "direction"
        )
    if not loop_mode:
        waypoints = list(picked("Waypoint"))
        waypoint_editor()

st.subheader(t("steepness.heading"))
if use_reference:
    ref_left, ref_right = st.columns(2)
    with ref_left:
        ref_start_latlon, ref_start_name = location_picker(t("steepness.ref_start"), "Sontga Gada", "ref_start")
    with ref_right:
        ref_end_latlon, ref_end_name = location_picker(t("steepness.ref_end"), "Mumpé Medel", "ref_end")
else:
    ref_start_latlon = ref_end_latlon = None
    ref_start_name = ref_end_name = ""
    st.info(t("steepness.off_notice"))

config = PlannerConfig(
    duration_hours=float(duration),
    other_path_penalty=float(other_penalty),
    max_car_road_share=float(road_tolerance_pct) / 100.0,
    avoid_herding_dogs=bool(avoid_herding_dogs),
    herding_dog_buffer_m=float(herding_margin_m),
    alpine_forbidden=bool(alpine_forbidden),
    direction_cone_deg=float(direction_cone),
    max_repeated_share=float(repeated_pct) / 100.0,
    candidate_pivots=int(candidate_pivots),
    steepness_tolerance=1.0 + float(steepness_tolerance_pct) / 100.0,
    max_duration_error=float(duration_tolerance_pct) / 100.0,
)

second_label = "direction target" if loop_mode else "destination"
missing = []
if start_latlon is None:
    missing.append(t("actions.missing_start"))
if direction_latlon is None:
    missing.append(t("actions.missing_second", second=second_label))
if use_reference and (ref_start_latlon is None or ref_end_latlon is None):
    missing.append(t("actions.missing_reference"))
ready = not missing

st.divider()
if missing:
    # A greyed-out button with no reason is the most common way to get stuck here.
    st.warning(
        t("actions.missing_prefix")
        + ", ".join(missing)
        + ("." if input_mode == "Search" else
           t("actions.missing_map_hint"))
    )
button_label = t("actions.generate_loops") if loop_mode else t("actions.plan_route")
if st.button(button_label, type="primary", disabled=not ready, use_container_width=True):
    try:
        with st.status(t("status.preparing"), expanded=True) as status:
            gpkg_str, layer = cached_dataset(str(CACHE_DIR), st.session_state.refresh_counter)
            gpkg = Path(gpkg_str)
            st.write(t("status.using_layer", layer=layer))

            herding_cached: list[dict] = []
            herding_lv95: list[dict] | None = None
            if avoid_herding_dogs:
                status.update(label=t("status.loading_pastures"))
                # One generous bbox around start and direction covers every search
                # radius plan_loops tries, so the federal layer is fetched once.
                sx, sy = latlon_to_lv95(start_latlon)
                dx, dy = latlon_to_lv95(direction_latlon)
                margin = max(20000.0, float(duration) * 6000.0)
                herding_bbox = (
                    min(sx, dx) - margin, min(sy, dy) - margin,
                    max(sx, dx) + margin, max(sy, dy) + margin,
                )
                herding_cached = cached_herding_areas(herding_bbox)
                herding_lv95 = herding_areas_lv95(herding_cached)
                st.write(
                    t("status.pastures_found", count=len(herding_cached))
                    if herding_cached
                    else t("status.pastures_none")
                )

            reference_limit = None
            reference_stats = None
            if use_reference:
                status.update(label=t("status.measuring_reference"))
                reference_limit, reference_stats, _, _ = compute_reference_grade(
                    gpkg,
                    layer,
                    ref_start_latlon,
                    ref_end_latlon,
                    config,
                    herding_dog_areas=herding_lv95,
                )
                st.write(
                    t("status.reference_line",
                      **{"from": ref_start_name, "to": ref_end_name,
                         "measured": f"{reference_stats.max_sustained_grade_percent:.1f}",
                         "limit": f"{reference_limit:.1f}"})
                )

            if loop_mode:
                status.update(label=t("status.searching_loops"))
                candidates, _ = plan_loops(
                    gpkg,
                    layer,
                    start_latlon,
                    direction_latlon,
                    config,
                    reference_grade_limit=reference_limit,
                    herding_dog_areas=herding_lv95,
                )
            else:
                status.update(
                    label=t("status.routing_oneway", count=len(waypoints))
                )
                candidates, _ = plan_point_to_point(
                    gpkg,
                    layer,
                    start_latlon,
                    direction_latlon,
                    config,
                    waypoints_latlon=waypoints,
                    reference_grade_limit=reference_limit,
                    herding_dog_areas=herding_lv95,
                )
            status.update(label=t("status.ready"), state="complete", expanded=False)

        st.session_state["candidates"] = candidates[:route_count]
        st.session_state["reference_stats"] = reference_stats
        st.session_state["reference_limit"] = reference_limit
        st.session_state["resolved_names"] = (start_name, direction_name, ref_start_name, ref_end_name)
        st.session_state["resolved_points"] = (start_latlon, direction_latlon)
        st.session_state["route_waypoints"] = list(waypoints)
        st.session_state["was_loop"] = bool(loop_mode)
        st.session_state["herding_areas"] = herding_cached
        st.session_state["herding_checked"] = bool(avoid_herding_dogs)
    except HikingPlannerError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)

candidates = st.session_state.get("candidates")
if candidates:
    reference_stats = st.session_state.get("reference_stats")
    reference_limit = st.session_state.get("reference_limit")
    start_latlon, direction_latlon = st.session_state["resolved_points"]

    herding_areas = st.session_state.get("herding_areas") or []
    if herding_areas:
        names = ", ".join(sorted({str(a.get("name")) for a in herding_areas if a.get("name")})[:6])
        st.warning(
            t("warnings.herding_nearby",
              count=len(herding_areas),
              names=names,
              more=" …" if len(herding_areas) > 6 else "")
        )
    elif st.session_state.get("herding_checked"):
        st.success(t("warnings.herding_none"))

    if reference_stats is not None:
        st.info(
            t("steepness.ceiling",
              limit=f"{reference_limit:.1f}",
              measured=f"{reference_stats.max_sustained_grade_percent:.1f}")
        )

    # Only the selected route is rendered. st.tabs keeps inactive tab bodies in the
    # DOM but hidden, which makes Leaflet measure a zero-size viewport and never
    # load tiles for routes 2 and 3.
    labels = [f"Route {i}" for i in range(1, len(candidates) + 1)]
    shown = {label: t("results.route_label", number=i) for i, label in enumerate(labels, start=1)}
    if st.session_state.get("selected_route") not in labels:
        st.session_state.pop("selected_route", None)
    # st.radio rather than st.segmented_control: the latter clears the selection
    # when the active option is clicked again, leaving the strip with nothing marked.
    selected_label = st.radio(
        "Route",
        labels,
        horizontal=True,
        key="selected_route",
        label_visibility="collapsed",
    )

    idx = labels.index(selected_label) + 1
    candidate = candidates[idx - 1]

    s = candidate.stats
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric(t("sidebar.route_target.duration"), f"{s.duration_minutes / 60:.2f} h")
    c2.metric(t("results.metric_distance"), f"{s.distance_km:.1f} km")
    c3.metric(t("results.metric_ascent"), f"{s.ascent_m:.0f} m")
    c4.metric(t("results.metric_grade"), f"{s.max_sustained_grade_percent:.1f}%")
    c5.metric(t("results.metric_official"), f"{s.official_share_percent:.0f}%")
    c6.metric(
        t("results.metric_cars"),
        f"{s.car_road_share_percent:.0f}%",
        help=t("results.metric_cars_help"),
    )

    road_note = (
        t("results.road_note_some", metres=f"{s.car_road_share_percent * s.distance_km * 10:.0f}")
        if s.car_road_share_percent
        else t("results.road_note_none")
    )
    if s.major_road_m > 0:
        road_note += t("results.road_note_major", metres=f"{s.major_road_m:.0f}")
    st.caption(
        t("results.detail_caption",
          descent=f"{s.descent_m:.0f}",
          repeated=f"{s.repeated_share_percent:.1f}",
          p95=f"{s.p95_grade_percent:.1f}",
          road_note=road_note)
    )

    profile = profile_frame(candidate.profile)
    stored_km = st.session_state.get(f"marker_km_{idx}")
    marker_km = stored_km
    if profile is None:
        marker_km = None
    elif marker_km is not None:
        marker_km = min(max(marker_km, 0.0), float(profile["km"].max()))

    st_folium(
        route_map(
            candidate,
            start_latlon,
            direction_latlon,
            frame=profile,
            marker_km=marker_km,
            herding_areas=st.session_state.get("herding_areas"),
            waypoints=st.session_state.get("route_waypoints") or (),
            loop=bool(st.session_state.get("was_loop", True)),
        ),
        height=560,
        use_container_width=True,
        returned_objects=[],
        # The key carries the marker so a new selection rebuilds the map iframe.
        key=f"map_{idx}_{marker_km if marker_km is None else round(marker_km, 3)}",
    )

    if profile is not None:
        if marker_km is None:
            st.caption(t("results.profile_hint"))
        else:
            point = profile.iloc[(profile["km"] - marker_km).abs().argmin()]
            st.caption(
                t("results.profile_marked",
                  km=f"{float(point['km']):.2f}",
                  elevation=f"{float(point['elevation']):.0f}",
                  lat=f"{float(point['lat']):.5f}",
                  lon=f"{float(point['lon']):.5f}")
            )

        event = st.altair_chart(
            elevation_chart(profile, marker_km),
            use_container_width=True,
            on_select="rerun",
            key=f"profile_{idx}",
        )
        clicked = picked_km(event)
        # Compare against what was stored, not the clamped marker: clamping a value
        # would otherwise make every rerun look like a fresh click and loop forever.
        if clicked is not None and clicked != stored_km:
            st.session_state[f"marker_km_{idx}"] = clicked
            st.rerun()

    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            t("results.download_gpx"),
            data=candidate_gpx(candidate, t("results.export_track_name", number=idx)),
            file_name=f"hiking_loop_{idx}.gpx",
            mime="application/gpx+xml",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            t("results.download_geojson"),
            data=candidate_geojson(candidate, t("results.export_track_name", number=idx)),
            file_name=f"hiking_loop_{idx}.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )

    st.download_button(
        t("results.download_zip", count=len(candidates)),
        data=zip_candidates(candidates),
        file_name="swiss_hiking_loops.zip",
        mime="application/zip",
        use_container_width=True,
    )

st.divider()
st.caption(
    t("footer.safety")
)
