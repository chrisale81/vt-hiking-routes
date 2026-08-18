"""Rules for turning map clicks into chosen points, tested without a browser."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import apply_pick

EMPTY = {"start": None, "destination": None, "waypoints": [], "last": None}


def test_start_and_destination_are_replaced_not_accumulated():
    state = apply_pick(EMPTY, "Start", (46.70, 8.83))
    assert state["start"] == (46.70, 8.83)

    state = apply_pick(state, "Start", (46.71, 8.84))
    assert state["start"] == (46.71, 8.84), "a second start click moves the start"
    assert state["waypoints"] == []

    state = apply_pick(state, "Destination", (46.75, 8.85))
    assert state["destination"] == (46.75, 8.85)
    assert state["start"] == (46.71, 8.84), "setting a destination leaves the start alone"


def test_waypoints_accumulate_in_order():
    state = apply_pick(EMPTY, "Waypoint", (46.70, 8.83))
    state = apply_pick(state, "Waypoint", (46.71, 8.84))
    state = apply_pick(state, "Waypoint", (46.72, 8.85))
    assert state["waypoints"] == [(46.70, 8.83), (46.71, 8.84), (46.72, 8.85)]


def test_a_replayed_click_is_ignored():
    """st_folium hands back the same last_clicked on every rerun."""
    state = apply_pick(EMPTY, "Waypoint", (46.70, 8.83))
    replayed = apply_pick(state, "Waypoint", (46.70, 8.83))
    assert replayed["waypoints"] == [(46.70, 8.83)], "the same click must not add twice"
    assert replayed == state

    # The identical spot chosen again in a different role still registers.
    moved = apply_pick(state, "Start", (46.70, 8.83))
    assert moved["start"] == (46.70, 8.83)


def test_no_click_changes_nothing():
    state = apply_pick(EMPTY, "Start", None)
    assert state == EMPTY


def test_the_same_point_can_be_reused_after_another_click():
    state = apply_pick(EMPTY, "Waypoint", (46.70, 8.83))
    state = apply_pick(state, "Waypoint", (46.71, 8.84))
    state = apply_pick(state, "Waypoint", (46.70, 8.83))
    assert state["waypoints"] == [(46.70, 8.83), (46.71, 8.84), (46.70, 8.83)]


def test_next_pick_role_walks_through_what_is_missing():
    """The picker should advance on its own: start, then the second point, then waypoints."""
    from app import next_pick_role

    loop_roles = ["Start", "Destination"]
    oneway_roles = ["Start", "Destination", "Waypoint"]

    empty = {"start": None, "destination": None, "waypoints": []}
    assert next_pick_role(empty, oneway_roles) == "Start"

    with_start = {"start": (46.7, 8.8), "destination": None, "waypoints": []}
    assert next_pick_role(with_start, oneway_roles) == "Destination"
    assert next_pick_role(with_start, loop_roles) == "Destination"

    both = {"start": (46.7, 8.8), "destination": (46.75, 8.85), "waypoints": []}
    assert next_pick_role(both, oneway_roles) == "Waypoint"
    # A loop has no waypoints, so it must not offer a role that is not on the radio.
    assert next_pick_role(both, loop_roles) in loop_roles
