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

from router import (
    HikingPlannerError,
    LocationResult,
    PlannerConfig,
    candidate_geojson,
    candidate_gpx,
    compute_reference_grade,
    download_and_extract_dataset,
    lv95_to_lonlat,
    plan_loops,
    route_latlon,
    search_locations,
    select_line_layer,
)


st.set_page_config(
    page_title="Swiss Hiking Loop Planner",
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


def location_picker(label: str, default_query: str, key: str, manual_default: tuple[float, float] = (46.70, 8.85)) -> tuple[tuple[float, float] | None, str]:
    query = st.text_input(label, value=default_query, key=f"{key}_query")
    manual = st.toggle("Use manual coordinates", value=False, key=f"{key}_manual")

    if manual:
        c1, c2 = st.columns(2)
        with c1:
            lat = st.number_input("Latitude", value=float(manual_default[0]), format="%.7f", key=f"{key}_lat")
        with c2:
            lon = st.number_input("Longitude", value=float(manual_default[1]), format="%.7f", key=f"{key}_lon")
        return (float(lat), float(lon)), f"{lat:.6f}, {lon:.6f}"

    if not query.strip():
        st.caption("Enter a Swiss place, address or mapped name.")
        return None, ""

    try:
        raw = cached_search(query.strip())
        results = [LocationResult(**x) for x in raw]
    except Exception as exc:
        st.error(f"Swiss location search failed: {exc}")
        return None, ""

    if not results:
        st.warning("No official geo.admin.ch location result. Refine the name or switch to manual coordinates.")
        return None, ""

    options = {f"{r.display}  [{r.lat:.5f}, {r.lon:.5f}]": r for r in results}
    selected_label = st.selectbox("Resolved location", list(options.keys()), key=f"{key}_select")
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

    x = alt.X("km:Q", title="Distance (km)", scale=alt.Scale(nice=False, domain=[0, float(frame["km"].max())]))
    y = alt.Y("elevation:Q", title="Elevation (m)", scale=alt.Scale(domain=domain, nice=False, clamp=True))

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


def route_map(candidate, start_latlon, direction_latlon, frame=None, marker_km=None):
    route = route_latlon(candidate.nodes)
    m = folium.Map(location=list(start_latlon), zoom_start=13, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles=(
            "https://wmts.geo.admin.ch/1.0.0/"
            "ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg"
        ),
        attr="© swisstopo",
        name="swisstopo",
        max_zoom=18,
    ).add_to(m)
    folium.PolyLine(route, weight=6, opacity=0.9, tooltip="Generated hiking loop").add_to(m)

    road_spans = getattr(candidate, "road_spans", None) or []
    if road_spans:
        roads = folium.FeatureGroup(name="Shared with cars", show=True)
        for span in road_spans:
            folium.PolyLine(
                route_latlon(span),
                weight=7,
                opacity=0.95,
                color="#d62728",
                tooltip="Shared with cars — keep the dog on the lead",
            ).add_to(roads)
        roads.add_to(m)

    if frame is not None and not frame.empty:
        ticks = folium.FeatureGroup(name="Kilometre marks", show=True)
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
                tooltip=f"km {km} · {float(point['elevation']):.0f} m",
            ).add_to(ticks)
        ticks.add_to(m)

    folium.Marker(start_latlon, tooltip="Start", icon=folium.Icon(icon="home", prefix="fa")).add_to(m)
    folium.Marker(direction_latlon, tooltip="Direction target", icon=folium.Icon(icon="compass", prefix="fa")).add_to(m)

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
            zf.writestr(f"{name}.gpx", candidate_gpx(c, f"Swiss hiking loop {i}"))
            zf.writestr(f"{name}.geojson", candidate_geojson(c, f"Swiss hiking loop {i}"))
    return buffer.getvalue()


st.title("🥾 Swiss Hiking Loop Planner")
st.caption("Official swissTLM3D hiking network · hard ban on alpine hiking trails · GPX / GeoJSON export")

with st.sidebar:
    st.header("Route target")
    duration = st.slider("Duration", min_value=1.0, max_value=7.0, value=3.0, step=0.25, format="%.2f h")
    st.checkbox("Loop", value=True, disabled=True)
    st.checkbox("Alpine hiking trails forbidden", value=True, disabled=True)
    st.caption("Duration uses the Swiss Hiking Trails rule and excludes breaks.")

    st.header("Roads and dogs")
    road_tolerance_pct = st.slider(
        "Max share on roads with cars",
        min_value=0,
        max_value=40,
        value=20,
        step=1,
        format="%d%%",
        help=(
            "Roads shared with motor traffic are avoided as far as the network allows. "
            "This is how much of the loop may still run on one - a loop usually has to "
            "leave the village somehow."
        ),
    )
    st.caption("Width and driving bans come from swissTLM3D; farm tracks closed to cars do not count.")

    st.header("Steepness reference")
    use_reference = st.checkbox("Limit steepness to reference route", value=True)

    with st.expander("Advanced routing", expanded=False):
        other_penalty = st.slider(
            "Penalty for ‘andere’ paths",
            min_value=1.0,
            max_value=20.0,
            value=8.0,
            step=0.5,
            help="Higher means official Wanderweg/Bergwanderweg segments are preferred more strongly.",
        )
        steepness_tolerance_pct = st.slider(
            "Reference steepness tolerance",
            min_value=0,
            max_value=25,
            value=5,
            step=1,
        )
        direction_cone = st.slider("Direction cone", 30, 120, 75, 5, format="%d°")
        repeated_pct = st.slider("Maximum repeated trail", 5, 50, 28, 1, format="%d%%")
        candidate_pivots = st.slider("Turnaround points to evaluate", 6, 48, 28, 1)
        duration_tolerance_pct = st.slider(
            "Duration tolerance",
            min_value=5,
            max_value=50,
            value=30,
            step=5,
            format="%d%%",
            help="Loops whose measured walking time misses the target by more than this are discarded.",
        )
        route_count = st.slider("Routes to show", 1, 10, 7, 1)

    if "refresh_counter" not in st.session_state:
        st.session_state.refresh_counter = 0
    if st.button("Refresh hiking dataset"):
        st.session_state.refresh_counter += 1
        cached_dataset.clear()
        st.success("A fresh opendata.swiss download will be used on the next route calculation.")

left, right = st.columns(2)
with left:
    st.subheader("Start and direction")
    start_latlon, start_name = location_picker(
        "Start", "Lagerhaus Alpina Segnas", "start", manual_default=(46.6961, 8.8278)
    )
    direction_latlon, direction_name = location_picker("Direction", "Disentis/Mustér", "direction")

with right:
    st.subheader("Steepness X")
    if use_reference:
        ref_start_latlon, ref_start_name = location_picker("Reference start", "Sontga Gada", "ref_start")
        ref_end_latlon, ref_end_name = location_picker("Reference end", "Mumpé Medel", "ref_end")
    else:
        ref_start_latlon = ref_end_latlon = None
        ref_start_name = ref_end_name = ""
        st.info("Reference filtering is disabled. Candidates will still show measured steepness.")

config = PlannerConfig(
    duration_hours=float(duration),
    other_path_penalty=float(other_penalty),
    max_car_road_share=float(road_tolerance_pct) / 100.0,
    alpine_forbidden=True,
    direction_cone_deg=float(direction_cone),
    max_repeated_share=float(repeated_pct) / 100.0,
    candidate_pivots=int(candidate_pivots),
    steepness_tolerance=1.0 + float(steepness_tolerance_pct) / 100.0,
    max_duration_error=float(duration_tolerance_pct) / 100.0,
)

ready = start_latlon is not None and direction_latlon is not None
if use_reference:
    ready = ready and ref_start_latlon is not None and ref_end_latlon is not None

st.divider()
if st.button("Generate hiking loops", type="primary", disabled=not ready, use_container_width=True):
    try:
        with st.status("Preparing official hiking data…", expanded=True) as status:
            gpkg_str, layer = cached_dataset(str(CACHE_DIR), st.session_state.refresh_counter)
            gpkg = Path(gpkg_str)
            st.write(f"Using layer: `{layer}`")

            reference_limit = None
            reference_stats = None
            if use_reference:
                status.update(label="Measuring reference steepness…")
                reference_limit, reference_stats, _, _ = compute_reference_grade(
                    gpkg,
                    layer,
                    ref_start_latlon,
                    ref_end_latlon,
                    config,
                )
                st.write(
                    f"Reference: {ref_start_name} → {ref_end_name}: "
                    f"{reference_stats.max_sustained_grade_percent:.1f}% sustained max; "
                    f"allowed {reference_limit:.1f}% with tolerance."
                )

            status.update(label="Searching loops toward the requested direction…")
            candidates, _ = plan_loops(
                gpkg,
                layer,
                start_latlon,
                direction_latlon,
                config,
                reference_grade_limit=reference_limit,
            )
            status.update(label="Routes ready", state="complete", expanded=False)

        st.session_state["candidates"] = candidates[:route_count]
        st.session_state["reference_stats"] = reference_stats
        st.session_state["reference_limit"] = reference_limit
        st.session_state["resolved_names"] = (start_name, direction_name, ref_start_name, ref_end_name)
        st.session_state["resolved_points"] = (start_latlon, direction_latlon)
    except HikingPlannerError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)

candidates = st.session_state.get("candidates")
if candidates:
    reference_stats = st.session_state.get("reference_stats")
    reference_limit = st.session_state.get("reference_limit")
    start_latlon, direction_latlon = st.session_state["resolved_points"]

    if reference_stats is not None:
        st.info(
            f"Steepness ceiling: **{reference_limit:.1f}% over a 50 m window** "
            f"(reference measured at {reference_stats.max_sustained_grade_percent:.1f}%)."
        )

    # Only the selected route is rendered. st.tabs keeps inactive tab bodies in the
    # DOM but hidden, which makes Leaflet measure a zero-size viewport and never
    # load tiles for routes 2 and 3.
    labels = [f"Route {i}" for i in range(1, len(candidates) + 1)]
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
    c1.metric("Duration", f"{s.duration_minutes / 60:.2f} h")
    c2.metric("Distance", f"{s.distance_km:.1f} km")
    c3.metric("Ascent", f"{s.ascent_m:.0f} m")
    c4.metric("Max 50 m grade", f"{s.max_sustained_grade_percent:.1f}%")
    c5.metric("Official paths", f"{s.official_share_percent:.0f}%")
    c6.metric(
        "Shared with cars",
        f"{s.car_road_share_percent:.0f}%",
        help="Share of the loop on roads open to motor traffic, drawn in red on the map.",
    )

    road_note = (
        f"car-shared road {s.car_road_share_percent * s.distance_km * 10:.0f} m"
        if s.car_road_share_percent
        else "no road shared with cars"
    )
    if s.major_road_m > 0:
        road_note += f" (of which {s.major_road_m:.0f} m on a road 6 m or wider)"
    st.caption(
        f"Descent {s.descent_m:.0f} m · repeated trail {s.repeated_share_percent:.1f}% · "
        f"95th-percentile sampled grade {s.p95_grade_percent:.1f}% · {road_note}"
    )

    profile = profile_frame(candidate.profile)
    stored_km = st.session_state.get(f"marker_km_{idx}")
    marker_km = stored_km
    if profile is None:
        marker_km = None
    elif marker_km is not None:
        marker_km = min(max(marker_km, 0.0), float(profile["km"].max()))

    st_folium(
        route_map(candidate, start_latlon, direction_latlon, frame=profile, marker_km=marker_km),
        height=560,
        use_container_width=True,
        returned_objects=[],
        # The key carries the marker so a new selection rebuilds the map iframe.
        key=f"map_{idx}_{marker_km if marker_km is None else round(marker_km, 3)}",
    )

    if profile is not None:
        if marker_km is None:
            st.caption("Click the elevation profile to mark that spot on the map.")
        else:
            point = profile.iloc[(profile["km"] - marker_km).abs().argmin()]
            st.caption(
                f"Marked: **{float(point['km']):.2f} km** into the loop at "
                f"**{float(point['elevation']):.0f} m** "
                f"({float(point['lat']):.5f}, {float(point['lon']):.5f}) — shown in red on the map."
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
            "Download GPX",
            data=candidate_gpx(candidate, f"Swiss hiking loop {idx}"),
            file_name=f"hiking_loop_{idx}.gpx",
            mime="application/gpx+xml",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "Download GeoJSON",
            data=candidate_geojson(candidate, f"Swiss hiking loop {idx}"),
            file_name=f"hiking_loop_{idx}.geojson",
            mime="application/geo+json",
            use_container_width=True,
        )

    st.download_button(
        f"Download all {len(candidates)} as ZIP",
        data=zip_candidates(candidates),
        file_name="swiss_hiking_loops.zip",
        mime="application/zip",
        use_container_width=True,
    )

st.divider()
st.caption(
    "Safety: this planner evaluates mapped route category, time, elevation and steepness. "
    "It does not assess current closures, snow, weather, livestock, dog restrictions or trail surface conditions."
)
