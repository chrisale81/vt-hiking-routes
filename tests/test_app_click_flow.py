"""Drive a real map click through the app, without a browser.

st_folium needs Leaflet from a CDN and a live browser, so the click path used to be
unverifiable here -- which is how a write to a live widget key reached production. The
component is stubbed with a canned click so the whole rerun cycle runs in-process.
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest

CLICK = {"lat": 46.7000000, "lng": 8.8300000}


def _stub_streamlit_folium(click):
    """Replace the real component with one that reports a click and nothing else."""
    module = types.ModuleType("streamlit_folium")

    def st_folium(fig, **kwargs):
        if kwargs.get("returned_objects") == ["last_clicked"]:
            return {"last_clicked": dict(click) if click else None}
        return {}

    module.st_folium = st_folium
    sys.modules["streamlit_folium"] = module


@pytest.fixture
def app(monkeypatch):
    _stub_streamlit_folium(CLICK)
    # The BAFU layer is a live federal service; the click flow must not depend on it.
    import router

    monkeypatch.setattr(router, "fetch_herding_dog_areas", lambda *a, **k: [])
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["input_mode"] = "Map"
    return at


def _run(at):
    at.run()
    assert not at.exception, f"app raised: {[str(e.value) for e in at.exception]}"
    return at


def test_map_click_does_not_raise_and_advances_the_role(app):
    """The regression: writing pick_role after the radio exists raised StreamlitAPIException."""
    at = _run(app)

    assert at.session_state["pick_start"] == (46.7, 8.83), "the click became the start"
    # Having placed a start, the picker must now be asking for the second point.
    assert at.session_state["pick_role"] == "Destination"


def test_a_second_click_sets_the_destination_without_touching_the_radio(app):
    at = _run(app)
    assert at.session_state["pick_role"] == "Destination"

    _stub_streamlit_folium({"lat": 46.7400000, "lng": 8.8500000})
    at = _run(at)

    assert at.session_state["pick_start"] == (46.7, 8.83), "the start is left alone"
    assert at.session_state["pick_destination"] == (46.74, 8.85)


def test_a_replayed_click_does_not_loop_or_duplicate(app):
    """st_folium hands back the same click every rerun; that must settle, not spin."""
    at = _run(app)
    first = at.session_state["pick_start"]

    at = _run(at)
    at = _run(at)

    assert at.session_state["pick_start"] == first
    assert at.session_state["pick_waypoints"] == []
