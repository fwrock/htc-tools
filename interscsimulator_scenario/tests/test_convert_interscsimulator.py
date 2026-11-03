"""Tests for convert_interscsimulator.py.

Uses small inline XML fixtures (mirroring the real structure confirmed against
tools/interscsimulator_scenario/input/base_scenario/*.xml) plus the repo's own
mini-map.xml/mini-trips.xml fixtures for an end-to-end smoke test.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import convert_interscsimulator as conv  # noqa: E402

BASE_SCENARIO_DIR = Path(__file__).resolve().parent.parent / "input" / "base_scenario"


# ---------------------------------------------------------------------------
# Coordinate reprojection
# ---------------------------------------------------------------------------


def test_matsim_sp_xy_to_lonlat_matches_known_osm_node():
    # Node 60609643 in base_scenario/network.xml: x=-7343944.451395443,
    # y=-2848345.467788374. Confirmed live via OSM API
    # (api.openstreetmap.org/api/0.6/node/60609643.json) as
    # lon=-46.6618937, lat=-23.5564177, tagged highway=traffic_signals —
    # matching its role as a signals.xml node ("Paulista x Bela Cintra").
    lon, lat = conv.matsim_sp_xy_to_lonlat(-7343944.451395443, -2848345.467788374)

    assert lon == pytest.approx(-46.6618937, abs=1e-3)
    assert lat == pytest.approx(-23.5564177, abs=1e-3)


def test_matsim_sp_xy_to_lonlat_stays_within_sao_paulo_bounds():
    # A separate node (out-of-sample vs. the affine fit's own training set): 25928755,
    # x=? — instead just check the transform lands in the greater São Paulo metro region
    # for the full network's coordinate range, catching gross sign/axis-swap regressions.
    for x, y in [
        (-7357441.957691746, -2853839.551546164),
        (-7327418.924698025, -2854288.583849282),
    ]:
        lon, lat = conv.matsim_sp_xy_to_lonlat(x, y)
        assert -47.5 < lon < -46.0
        assert -24.0 < lat < -23.0


def test_local_xy_to_lonlat_offsets_from_anchor():
    lon, lat = conv.local_xy_to_lonlat(0.0, 0.0, anchor_lat=-23.55, anchor_lon=-46.63)
    assert (lon, lat) == (-46.63, -23.55)

    # 111.32 km north of the anchor should be ~1 degree of latitude away.
    lon2, lat2 = conv.local_xy_to_lonlat(0.0, conv.METERS_PER_DEGREE_LAT, anchor_lat=-23.55, anchor_lon=-46.63)
    assert lat2 == pytest.approx(-22.55, abs=1e-6)
    assert lon2 == pytest.approx(-46.63, abs=1e-9)


def test_project_xy_auto_picks_local_mode_for_small_coordinates():
    lon, lat = conv.project_xy(10.0, 20.0, "auto", anchor_lat=-23.55, anchor_lon=-46.63)
    expected_lon, expected_lat = conv.local_xy_to_lonlat(10.0, 20.0, -23.55, -46.63)
    assert (lon, lat) == (expected_lon, expected_lat)


def test_project_xy_auto_picks_matsim_mode_for_city_scale_coordinates():
    lon, lat = conv.project_xy(-7343944.45, -2848345.47, "auto", anchor_lat=-23.55, anchor_lon=-46.63)
    expected_lon, expected_lat = conv.matsim_sp_xy_to_lonlat(-7343944.45, -2848345.47)
    assert (lon, lat) == (expected_lon, expected_lat)


def test_project_xy_rejects_unknown_mode():
    with pytest.raises(ValueError):
        conv.project_xy(0.0, 0.0, "bogus-mode", anchor_lat=0.0, anchor_lon=0.0)


# ---------------------------------------------------------------------------
# network.xml parsing
# ---------------------------------------------------------------------------

MINI_NETWORK_XML = """<?xml version="1.0" encoding="UTF-8"?>
<network>
    <nodes>
        <node id="1" x="0" y="0"/>
        <node id="2" x="100" y="0"/>
        <node id="3" x="100" y="100"/>
    </nodes>
    <links capperiod="01:00:00" effectivecellsize="7.5" effectivelanewidth="3.75">
        <link id="12" from="1" to="2" length="100.0" freespeed="8.333333333333334"
              capacity="1800.0" permlanes="2.0" oneway="1" modes="car">
            <attributes>
                <attribute name="origid" class="java.lang.String">262851680</attribute>
            </attributes>
        </link>
        <link id="23" from="2" to="3" length="100.0" freespeed="13.88888888888889"
              capacity="1800.0" permlanes="1.0" oneway="1" modes="car"/>
    </links>
</network>
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


def test_parse_network_reads_nodes_and_links(tmp_path):
    path = _write(tmp_path, "network.xml", MINI_NETWORK_XML)
    nodes, links = conv.parse_network(path)

    assert set(nodes) == {"1", "2", "3"}
    assert nodes["2"] == {"x": 100.0, "y": 0.0}

    assert set(links) == {"12", "23"}
    assert links["12"]["from"] == "1"
    assert links["12"]["to"] == "2"
    assert links["12"]["length"] == 100.0
    assert links["12"]["permlanes"] == 2.0
    assert links["23"]["freespeed"] == pytest.approx(13.88888888888889)


def test_parse_network_against_real_mini_map_fixture():
    nodes, links = conv.parse_network(BASE_SCENARIO_DIR / "mini-map.xml")
    assert set(nodes) == {"1", "2", "3", "4", "5", "6"}
    assert links["12"]["from"] == "1"
    assert links["12"]["to"] == "2"
    assert links["56"]["to"] == "6"


# ---------------------------------------------------------------------------
# trips.xml parsing
# ---------------------------------------------------------------------------

MINI_TRIPS_XML = """<?xml version="1.0" ?>
<scsimulator_matrix>
<trip name="t0" origin="1" destination="3" link_origin="12" count="1" start="6"
      mode="car" digital_rails_capable="false"/>
<trip name="t1" origin="1" destination="3" link_origin="12" count="1" start="8"
      mode="bike" digital_rails_capable="false"/>
</scsimulator_matrix>
"""


def test_parse_trips_reads_all_fields(tmp_path):
    path = _write(tmp_path, "trips.xml", MINI_TRIPS_XML)
    trips = conv.parse_trips(path)

    assert len(trips) == 2
    assert trips[0] == {
        "name": "t0",
        "origin": "1",
        "destination": "3",
        "link_origin": "12",
        "count": 1,
        "start": 6.0,
        "mode": "car",
        "digital_rails_capable": False,
    }
    assert trips[1]["mode"] == "bike"


def test_parse_trips_against_real_mini_trips_fixture():
    trips = conv.parse_trips(BASE_SCENARIO_DIR / "mini-trips.xml")
    assert len(trips) == 3
    assert {t["mode"] for t in trips} == {"car", "bike"}


# ---------------------------------------------------------------------------
# signals.xml parsing + traffic-signal actor generation
# ---------------------------------------------------------------------------

MINI_SIGNALS_XML = """<traffic-signals>
  <signal cycle_duration="90" offset="10">
    <nodes>
      <node id="2"/>
    </nodes>
    <phases>
      <phase origin="1" green_duration="45" green_start="0"/>
      <phase origin="3" green_duration="40" green_start="46"/>
    </phases>
  </signal>
</traffic-signals>
"""


def test_parse_signals_reads_nodes_and_phases(tmp_path):
    path = _write(tmp_path, "signals.xml", MINI_SIGNALS_XML)
    signals = conv.parse_signals(path)

    assert len(signals) == 1
    signal = signals[0]
    assert signal["cycle_duration"] == 90.0
    assert signal["offset"] == 10.0
    assert signal["nodes"] == ["2"]
    assert signal["phases"] == [
        {"origin": "1", "green_duration": 45.0, "green_start": 0.0},
        {"origin": "3", "green_duration": 40.0, "green_start": 46.0},
    ]


def test_parse_signals_skips_empty_placeholder_fixture():
    signals = conv.parse_signals(BASE_SCENARIO_DIR / "empty-signals.xml")
    assert signals == []


def test_parse_signals_against_real_base_scenario_fixture():
    signals = conv.parse_signals(BASE_SCENARIO_DIR / "signals.xml")
    assert len(signals) == 13
    assert signals[0]["nodes"] == ["2098758071", "60609643"]


def test_build_traffic_signal_actors_resolves_origin_to_incoming_link_id(tmp_path):
    nodes, links = conv.parse_network(_write(tmp_path, "network.xml", MINI_NETWORK_XML))
    signals = conv.parse_signals(_write(tmp_path, "signals.xml", MINI_SIGNALS_XML))

    actors = conv.build_traffic_signal_actors(signals, links)

    assert len(actors) == 1
    actor = actors[0]
    assert actor["typeActor"] == "hybrid.actor.TrafficSignal"
    assert actor["data"]["dataType"] == "model.hybrid.entity.state.TrafficSignalState"
    content = actor["data"]["content"]
    assert content["cycleDuration"] == 90
    assert content["offset"] == 10
    assert content["nodes"] == ["htcaid:node;2"]

    # Phase 0: origin node "1" -> incoming link "12" (1 -> 2, matches the signal's node "2").
    assert content["phases"][0]["origin"] == "htcaid:link;12"
    assert content["phases"][0]["greenDuration"] == 45
    # Phase 1: origin node "3" has no link into node "2" in this fixture (link 23 goes 2->3,
    # the wrong direction) — falls back to the node id rather than a bogus link id.
    assert content["phases"][1]["origin"] == "htcaid:node;3"

    assert set(content["signalStates"]) == {"htcaid:link;12", "htcaid:node;3"}
    assert content["signalStates"]["htcaid:link;12"] == {
        "state": "Red", "remainingTime": 0, "nextTick": 0,
    }
    assert actor["dependencies"] == {
        "htcaid:node;2": {"id": "htcaid:node;2", "classType": "hybrid.actor.Node"}
    }


def test_wire_node_approach_connections_keys_by_approach_link_on_the_controlled_node(tmp_path):
    # Regression coverage for docs/KNOWN_GAPS.md's "Production Scenario Generators Never Wire
    # Node.connections/approachConnections" finding: node actors used to always emit
    # "connections": {} unconditionally, so a converted scenario's traffic signals had zero
    # effect on any vehicle (MICRO's signalAtExit braking was never notified). This locks in
    # that wire_node_approach_connections patches the controlled node's approachConnections,
    # keyed by the actual APPROACH link (not the outgoing link, and not the signals.xml origin
    # node id) -- the same key build_traffic_signal_actors resolves each phase's origin to.
    nodes, links = conv.parse_network(_write(tmp_path, "network.xml", MINI_NETWORK_XML))
    signals = conv.parse_signals(_write(tmp_path, "signals.xml", MINI_SIGNALS_XML))
    node_actors, _ = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)

    conv.wire_node_approach_connections(node_actors, signals, links)

    by_id = {n["id"]: n for n in node_actors}
    node2 = by_id["htcaid:node;2"]
    approach = node2["data"]["content"]["approachConnections"]
    # Phase origin="1" resolves to link "12" (1->2, into node 2) -- wired.
    assert approach == {"htcaid:link;12": {
        "id": "htcaid:link;12", "resourceId": "", "classType": "", "actorRef": "", "actorType": "",
    }}
    # connections (outgoing-link keyed, needed for MESO admission control) is deliberately left
    # untouched -- still empty, since resolving it needs turn-movement data this format lacks.
    assert node2["data"]["content"]["connections"] == {}

    # Nodes with no signal phase resolving to them keep an empty approachConnections, same as
    # before this function ran -- no spurious entries anywhere else.
    node1 = by_id["htcaid:node;1"]
    node3 = by_id["htcaid:node;3"]
    assert node1["data"]["content"]["approachConnections"] == {}
    assert node3["data"]["content"]["approachConnections"] == {}


def test_wire_node_approach_connections_against_real_base_scenario():
    nodes, links = conv.parse_network(BASE_SCENARIO_DIR / "network.xml")
    signals = conv.parse_signals(BASE_SCENARIO_DIR / "signals.xml")
    node_actors, _ = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)

    conv.wire_node_approach_connections(node_actors, signals, links)

    wired = [
        n for n in node_actors if n["data"]["content"]["approachConnections"]
    ]
    assert len(wired) > 0
    # Every wired approachConnections entry's key must be a real link actor id that this node
    # actually owns as an approach (not a bogus node-id fallback, which build_traffic_signal_actors
    # itself falls back to for phases with no resolvable link -- this function must skip those).
    for node_actor in wired:
        for link_id in node_actor["data"]["content"]["approachConnections"]:
            assert link_id.startswith("htcaid:link;")


def test_build_traffic_signal_actors_against_real_base_scenario():
    nodes, links = conv.parse_network(BASE_SCENARIO_DIR / "network.xml")
    signals = conv.parse_signals(BASE_SCENARIO_DIR / "signals.xml")

    actors = conv.build_traffic_signal_actors(signals, links)

    assert len(actors) == 13
    # At least some phases in the real fixture should resolve to a real incoming link
    # (not fall back to the node-id placeholder) — this is the "robust" priority path.
    resolved = [
        p["origin"] for actor in actors for p in actor["data"]["content"]["phases"]
        if p["origin"].startswith("htcaid:link;")
    ]
    assert len(resolved) > 0


# ---------------------------------------------------------------------------
# Node/Link actor + city_map generation
# ---------------------------------------------------------------------------


def test_build_node_actors_projects_coordinates_and_lists_outgoing_links(tmp_path):
    nodes, links = conv.parse_network(_write(tmp_path, "network.xml", MINI_NETWORK_XML))

    node_actors, coords = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)

    assert len(node_actors) == 3
    by_id = {n["id"]: n for n in node_actors}
    assert "htcaid:node;1" in by_id
    node1 = by_id["htcaid:node;1"]
    assert node1["typeActor"] == "hybrid.actor.Node"
    assert node1["data"]["content"]["links"] == ["htcaid:link;12"]
    assert node1["data"]["content"]["latitude"] == pytest.approx(-23.55)
    assert node1["data"]["content"]["longitude"] == pytest.approx(-46.63)

    node3 = by_id["htcaid:node;3"]
    assert node3["data"]["content"]["links"] == []  # node 3 has no outgoing link
    assert "1" in coords and "2" in coords and "3" in coords


def test_build_link_actors_converts_mps_freespeed_to_kmh_speed_limit(tmp_path):
    nodes, links = conv.parse_network(_write(tmp_path, "network.xml", MINI_NETWORK_XML))

    link_actors = conv.build_link_actors(links)

    by_id = {l["id"]: l for l in link_actors}
    link12 = by_id["htcaid:link;12"]
    assert link12["data"]["content"]["from"] == "htcaid:node;1"
    assert link12["data"]["content"]["to"] == "htcaid:node;2"
    assert link12["data"]["content"]["length"] == 100.0
    assert link12["data"]["content"]["lanes"] == 2
    # freespeed=8.333.. m/s -> 30 km/h
    assert link12["data"]["content"]["speedLimit"] == pytest.approx(30.0)
    assert link12["data"]["content"]["freeSpeed"] == pytest.approx(30.0)
    assert link12["dependencies"]["from_node"]["id"] == "htcaid:node;1"


def test_build_city_map_shape(tmp_path):
    nodes, links = conv.parse_network(_write(tmp_path, "network.xml", MINI_NETWORK_XML))
    node_actors, _ = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)
    link_actors = conv.build_link_actors(links)

    city_map = conv.build_city_map(node_actors, link_actors)

    assert city_map["directed"] is True
    assert len(city_map["nodes"]) == 3
    assert len(city_map["edges"]) == 2
    edge = next(e for e in city_map["edges"] if e["label"]["id"] == "htcaid:link;12")
    assert edge["source_id"] == "htcaid:node;1"
    assert edge["target_id"] == "htcaid:node;2"
    assert edge["weight"] == 100.0


# ---------------------------------------------------------------------------
# Person/Car actor generation (best-effort)
# ---------------------------------------------------------------------------


def test_build_person_and_vehicle_actors_creates_a_pair_per_trip():
    trips = [
        {
            "name": "t0", "origin": "1", "destination": "3", "link_origin": "12",
            "count": 1, "start": 7.0, "mode": "car", "digital_rails_capable": False,
        }
    ]
    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids={"1", "3"})

    assert len(actors) == 2
    person, car = actors
    assert person["typeActor"] == "hybrid.actor.Person"
    assert person["id"] == "htcaid:person;t0"
    content = person["data"]["content"]
    assert content["startTick"] == 7 * 60  # default start_unit=minutes
    assert person["data"]["dataType"] == "model.hybrid.entity.state.PersonState"

    # New PersonState shape: no top-level origin/destination — they live inside originalPlan's
    # Activity nodes (PersonState.scala no longer has origin/destination fields at all).
    assert "origin" not in content
    assert "destination" not in content
    assert "dailySchedule" not in content
    assert "currentActivityIndex" not in content

    plan = content["originalPlan"]
    assert [step["kind"] for step in plan] == ["Activity", "PrivateVehicleLeg", "Activity"]
    origin_activity, leg, dest_activity = plan
    assert origin_activity["nodeId"] == "htcaid:node;1"
    assert origin_activity["endTime"] == {"kind": "AtTick", "tick": 7 * 60}
    assert leg["mode"] == "Car"
    assert leg["vehicle"] == {"id": "htcaid:car;t0", "classType": "hybrid.actor.Car"}
    assert dest_activity["nodeId"] == "htcaid:node;3"
    # Absolute end-of-day tick, not a Duration — see SIMULATION_DAY_END_TICK's comment in
    # build_person_and_vehicle_actors for why Duration(86400) would be wrong here (it would
    # anchor 86400s after actual arrival, not to the simulated day boundary).
    assert dest_activity["endTime"] == {"kind": "AtTick", "tick": 86400}

    assert content["ownedVehicles"] == {"car": {"id": "htcaid:car;t0", "classType": "hybrid.actor.Car"}}
    assert content["vehicleCurrentNode"] == {"car": "htcaid:node;1"}
    # cursor/tripExecution deliberately absent — Person.internStateStrings seeds cursor from
    # originalPlan on load (see PersonStateJsonSpec's "scenario-shaped payload" test).
    assert "cursor" not in content
    assert "tripExecution" not in content

    assert car["typeActor"] == "hybrid.actor.Car"
    assert car["id"] == "htcaid:car;t0"
    assert car["data"]["dataType"] == "model.hybrid.entity.state.CarState"
    assert car["data"]["content"]["origin"] == "htcaid:node;1"
    assert car["data"]["content"]["destination"] == "htcaid:node;3"
    assert car["data"]["content"]["actorType"] == "Car"
    assert car["data"]["content"]["precomputedRoute"] is None


def test_build_person_and_vehicle_actors_respects_start_unit_seconds():
    trips = [
        {
            "name": "t0", "origin": "1", "destination": "3", "link_origin": "12",
            "count": 1, "start": 42.0, "mode": "car", "digital_rails_capable": False,
        }
    ]
    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids={"1", "3"}, start_unit="seconds")
    assert actors[0]["data"]["content"]["startTick"] == 42


def test_build_person_and_vehicle_actors_maps_bike_mode_to_bicycle_actor_type():
    trips = [
        {
            "name": "b0", "origin": "1", "destination": "3", "link_origin": "12",
            "count": 1, "start": 1.0, "mode": "bike", "digital_rails_capable": False,
        }
    ]
    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids={"1", "3"})
    vehicle = actors[1]
    assert vehicle["typeActor"] == "hybrid.actor.Bicycle"
    assert vehicle["data"]["dataType"] == "model.hybrid.entity.state.BicycleState"
    assert vehicle["data"]["content"]["actorType"] == "Bicycle"
    # BicycleState has no precomputedRoute field (unlike CarState) — must not be emitted.
    assert "precomputedRoute" not in vehicle["data"]["content"]

    person_plan = actors[0]["data"]["content"]["originalPlan"]
    leg = person_plan[1]
    assert leg["mode"] == "Bicycle"
    assert leg["vehicle"]["classType"] == "hybrid.actor.Bicycle"


def test_build_person_and_vehicle_actors_maps_motorcycle_mode():
    trips = [
        {
            "name": "m0", "origin": "1", "destination": "3", "link_origin": "12",
            "count": 1, "start": 1.0, "mode": "motorcycle", "digital_rails_capable": False,
        }
    ]
    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids={"1", "3"})
    vehicle = actors[1]
    assert vehicle["typeActor"] == "hybrid.actor.Motorcycle"
    assert vehicle["data"]["dataType"] == "model.hybrid.entity.state.MotorcycleState"


def test_build_person_and_vehicle_actors_skips_trips_with_unknown_nodes():
    trips = [
        {
            "name": "bad", "origin": "999", "destination": "3", "link_origin": "12",
            "count": 1, "start": 1.0, "mode": "car", "digital_rails_capable": False,
        }
    ]
    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids={"1", "3"})
    assert actors == []


# ---------------------------------------------------------------------------
# Bike-trip synthesis
# ---------------------------------------------------------------------------


def test_synthesize_bike_trips_no_op_at_zero_fraction():
    trips = [
        {"name": "t0", "origin": "1", "destination": "3", "link_origin": "12", "count": 1, "start": 1.0, "mode": "car", "digital_rails_capable": False},
        {"name": "t1", "origin": "1", "destination": "3", "link_origin": "12", "count": 1, "start": 2.0, "mode": "car", "digital_rails_capable": False},
    ]
    result = conv.synthesize_bike_trips(trips, 0.0)
    assert result == trips


def test_synthesize_bike_trips_converts_expected_fraction_deterministically():
    trips = [
        {"name": f"t{i}", "origin": "1", "destination": "3", "link_origin": "12", "count": 1, "start": float(i), "mode": "car", "digital_rails_capable": False}
        for i in range(20)
    ]
    result = conv.synthesize_bike_trips(trips, 0.5, seed=7)

    assert len(result) == 20
    bike_count = sum(1 for t in result if t["mode"] == "bike")
    assert bike_count == 10

    # Deterministic for a fixed seed.
    result2 = conv.synthesize_bike_trips(trips, 0.5, seed=7)
    assert [t["mode"] for t in result2] == [t["mode"] for t in result]

    # Original list is untouched.
    assert all(t["mode"] == "car" for t in trips)


# ---------------------------------------------------------------------------
# Extra synthetic OD trips (beyond trips.xml)
# ---------------------------------------------------------------------------


def test_synthesize_extra_od_trips_generates_requested_count_with_distinct_od():
    node_ids = [str(i) for i in range(10)]
    trips = conv.synthesize_extra_od_trips(node_ids, count=5, seed=1)

    assert len(trips) == 5
    for trip in trips:
        assert trip["origin"] != trip["destination"]
        assert trip["origin"] in node_ids
        assert trip["destination"] in node_ids
        assert trip["mode"] in ("car", "bike")
        assert trip["name"].startswith("synthetic_extra_")
        assert 0 <= trip["start"] < conv.SIMULATION_DAY_END_TICK // 60  # minutes range


def test_synthesize_extra_od_trips_is_deterministic_for_a_fixed_seed():
    node_ids = [str(i) for i in range(20)]
    trips1 = conv.synthesize_extra_od_trips(node_ids, count=8, seed=99)
    trips2 = conv.synthesize_extra_od_trips(node_ids, count=8, seed=99)
    assert trips1 == trips2


def test_synthesize_extra_od_trips_respects_car_fraction_at_the_extremes():
    node_ids = [str(i) for i in range(10)]
    all_car = conv.synthesize_extra_od_trips(node_ids, count=20, seed=5, car_fraction=1.0)
    assert all(t["mode"] == "car" for t in all_car)

    all_bike = conv.synthesize_extra_od_trips(node_ids, count=20, seed=5, car_fraction=0.0)
    assert all(t["mode"] == "bike" for t in all_bike)


def test_synthesize_extra_od_trips_zero_count_or_insufficient_nodes_returns_empty():
    assert conv.synthesize_extra_od_trips(["1", "2", "3"], count=0) == []
    assert conv.synthesize_extra_od_trips(["1"], count=5) == []
    assert conv.synthesize_extra_od_trips([], count=5) == []


def test_synthesize_extra_od_trips_feed_directly_into_person_and_vehicle_actors():
    node_ids = [str(i) for i in range(6)]
    trips = conv.synthesize_extra_od_trips(node_ids, count=3, seed=3)

    actors = conv.build_person_and_vehicle_actors(trips, node_raw_ids=set(node_ids))

    # Every synthetic trip should have produced exactly one Person + one vehicle actor.
    person_count = sum(1 for a in actors if a["typeActor"] == "hybrid.actor.Person")
    assert person_count == 3


# ---------------------------------------------------------------------------
# Synthetic transit network (bus/subway) generation from topology
# ---------------------------------------------------------------------------


def test_compute_node_degrees_counts_both_directions():
    nodes, links = conv.parse_network(_write_test_network())
    degree = conv.compute_node_degrees(links)
    # Node "2" is both the `to` of link 12 and the `from` of link 23.
    assert degree["2"] == 2
    assert degree["1"] == 1
    assert degree["3"] == 1


def _write_test_network():
    import tempfile
    path = Path(tempfile.mkstemp(suffix=".xml")[1])
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<network>
    <nodes>
        <node id="1" x="0" y="0"/>
        <node id="2" x="100" y="0"/>
        <node id="3" x="100" y="100"/>
    </nodes>
    <links capperiod="01:00:00" effectivecellsize="7.5" effectivelanewidth="3.75">
        <link id="12" from="1" to="2" length="100.0" freespeed="8.333333333333334"
              capacity="1800.0" permlanes="2.0" oneway="1" modes="car"/>
        <link id="23" from="2" to="3" length="100.0" freespeed="13.88888888888889"
              capacity="1800.0" permlanes="1.0" oneway="1" modes="car"/>
    </links>
</network>
"""
    )
    return path


def test_select_hub_nodes_prefers_higher_degree_and_respects_spacing():
    degree = {"a": 5, "b": 4, "c": 3, "d": 1}
    coords = {"a": (0.0, 0.0), "b": (0.0, 0.0001), "c": (1.0, 1.0), "d": (2.0, 2.0)}
    # "b" is within a few meters of "a" — should be skipped in favor of "c" at min_spacing=1000m.
    chosen = conv.select_hub_nodes(degree, coords, count=2, min_spacing_m=1000.0)
    assert chosen == ["a", "c"]


def test_select_hub_nodes_falls_back_to_ignoring_spacing_when_too_few_candidates():
    degree = {"a": 5, "b": 4}
    coords = {"a": (0.0, 0.0), "b": (0.0, 0.00001)}  # a few meters apart
    chosen = conv.select_hub_nodes(degree, coords, count=2, min_spacing_m=10_000.0)
    assert set(chosen) == {"a", "b"}


def test_partition_into_lines_groups_by_geography_and_drops_short_lines():
    coords = {"a": (0.0, 0.0), "b": (0.0, 1.0), "c": (0.0, 2.0), "d": (0.0, 3.0)}
    lines = conv.partition_into_lines(["d", "a", "c", "b"], coords, num_lines=2, stops_per_line=2)
    assert lines == [["a", "b"], ["c", "d"]]


def test_build_synthetic_bus_actors_produces_stop_station_and_bus_actors():
    coords = {"1": (-23.55, -46.63), "2": (-23.551, -46.631), "3": (-23.552, -46.632)}
    actors, stats = conv.build_synthetic_bus_actors([["1", "2", "3"]], coords, buses_per_line=2)

    stops = [a for a in actors if a["typeActor"] == "hybrid.actor.BusStop"]
    stations = [a for a in actors if a["typeActor"] == "hybrid.actor.BusStation"]
    buses = [a for a in actors if a["typeActor"] == "hybrid.actor.Bus"]

    assert len(stops) == 3
    assert len(stations) == 1
    assert len(buses) == 2
    assert stats == {"bus_lines": 1, "bus_stops": 3, "bus_stations": 1, "buses": 2}

    station_content = stations[0]["data"]["content"]
    assert station_content["origin"] == "htcaid:node;1"
    assert station_content["destination"] == "htcaid:node;3"
    assert len(station_content["buses"]) == 2
    assert list(station_content["busStops"].keys()) == [
        "htcaid:busstop;1", "htcaid:busstop;2", "htcaid:busstop;3",
    ]
    # BusStationState.goingRoute/returningRoute default to Some(Map.empty), whose correct JSON
    # is `{}` — not `null` (confirmed via a real Scala JsonUtil.convertValue roundtrip against
    # BusStationState during this change; see the comment at the emission site).
    assert station_content["goingRoute"] == {}
    assert station_content["returningRoute"] == {}

    bus_content = buses[0]["data"]["content"]
    assert bus_content["actorType"] == "Bus"
    assert bus_content["origin"] == "htcaid:node;1"
    assert bus_content["destination"] == "htcaid:node;3"
    assert buses[0]["data"]["dataType"] == "model.hybrid.entity.state.BusState"


def test_build_synthetic_bus_actors_dedupes_stops_shared_across_lines():
    coords = {"1": (-23.55, -46.63), "2": (-23.551, -46.631), "3": (-23.552, -46.632)}
    actors, stats = conv.build_synthetic_bus_actors(
        [["1", "2"], ["2", "3"]], coords, buses_per_line=1
    )
    stops = [a for a in actors if a["typeActor"] == "hybrid.actor.BusStop"]
    assert len(stops) == 3  # node "2" shared by both lines, created once
    assert stats["bus_stops"] == 3
    assert stats["bus_lines"] == 2


def test_build_synthetic_subway_actors_produces_stations_and_rail_links():
    coords = {
        "1": (-23.55, -46.63), "2": (-23.56, -46.64), "3": (-23.57, -46.65),
    }
    actors, stats = conv.build_synthetic_subway_actors([["1", "2", "3"]], coords, trains_per_line=2)

    stations = [a for a in actors if a["typeActor"] == "hybrid.actor.SubwayStation"]
    rail_links = [a for a in actors if a["typeActor"] == "hybrid.actor.RailLink"]
    trains = [a for a in actors if a["typeActor"] == "hybrid.actor.Subway"]

    assert len(stations) == 3
    assert len(rail_links) == 2
    assert len(trains) == 2  # standalone SubwayState vehicle actors, not just the descriptors
    assert stats == {"subway_lines": 1, "subway_stations": 3, "rail_links": 2, "subway_trains": 2}

    by_node = {s["data"]["content"]["nodeId"]: s for s in stations}
    garage = by_node["htcaid:node;1"]
    assert garage["data"]["content"]["garage"] is True
    assert garage["data"]["content"]["terminal"] is True
    assert "line0" in garage["data"]["content"]["lines"]
    assert len(garage["data"]["content"]["subways"]["line0"]) == 2
    assert len(garage["data"]["content"]["linesRoute"]["line0"]) == 2

    train_content = trains[0]["data"]["content"]
    assert trains[0]["data"]["dataType"] == "model.hybrid.entity.state.SubwayState"
    assert train_content["origin"] == "htcaid:node;1"
    assert train_content["destination"] == "htcaid:node;3"
    assert train_content["line"] == "line0"
    assert train_content["actorType"] == "Subway"
    # Every descriptor registered in the garage's dispatch table must have a matching
    # standalone actor — otherwise the line has infrastructure but nothing running on it.
    descriptor_ids = {t["actorId"] for t in garage["data"]["content"]["subways"]["line0"]}
    assert descriptor_ids == {t["id"] for t in trains}

    terminal_only = by_node["htcaid:node;3"]
    assert terminal_only["data"]["content"]["garage"] is False
    assert terminal_only["data"]["content"]["terminal"] is True
    assert terminal_only["data"]["content"]["lines"] == {}

    rail_link_content = rail_links[0]["data"]["content"]
    # RailLinkState.startTick has no default in the case class — must always be present,
    # unlike tools/sao-paulo/generate_hybrid_input.py's reference (which omits it).
    assert rail_link_content["startTick"] == 0
    assert rail_link_content["from"] == "htcaid:node;1"
    assert rail_link_content["to"] == "htcaid:node;2"
    assert rail_link_content["length"] > 0
    assert "laneConfigurations" not in rail_link_content
    assert "vehiclesByLane" not in rail_link_content


def test_synthesize_transit_network_end_to_end_on_mini_fixture():
    nodes, links = conv.parse_network(BASE_SCENARIO_DIR / "mini-map.xml")
    node_actors, coords = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)

    actors, stats = conv.synthesize_transit_network(
        nodes, links, coords,
        num_bus_lines=1, bus_stops_per_line=2, bus_headway_s=600, bus_capacity=80,
        bus_ports=2, buses_per_line=1,
        num_subway_lines=1, subway_stations_per_line=2, subway_headway_s=300,
        subway_capacity=300, subway_ports=4, subway_speed_ms=22.22, subway_stop_time_s=30,
        subway_trains_per_line=1, hub_min_spacing_m=1.0,
    )

    type_counts: dict = {}
    for a in actors:
        type_counts[a["typeActor"]] = type_counts.get(a["typeActor"], 0) + 1

    assert type_counts.get("hybrid.actor.BusStop", 0) >= 2
    assert type_counts.get("hybrid.actor.BusStation", 0) == 1
    assert type_counts.get("hybrid.actor.Bus", 0) == 1
    assert type_counts.get("hybrid.actor.SubwayStation", 0) >= 2
    assert type_counts.get("hybrid.actor.RailLink", 0) >= 1
    assert type_counts.get("hybrid.actor.Subway", 0) == 1
    assert stats["bus_lines"] == 1
    assert stats["subway_lines"] == 1


def test_synthesize_transit_network_disabled_when_line_counts_are_zero():
    nodes, links = conv.parse_network(BASE_SCENARIO_DIR / "mini-map.xml")
    node_actors, coords = conv.build_node_actors(nodes, links, "none", -23.55, -46.63)

    actors, stats = conv.synthesize_transit_network(
        nodes, links, coords,
        num_bus_lines=0, bus_stops_per_line=6, bus_headway_s=600, bus_capacity=80,
        bus_ports=2, buses_per_line=1,
        num_subway_lines=0, subway_stations_per_line=6, subway_headway_s=300,
        subway_capacity=300, subway_ports=4, subway_speed_ms=22.22, subway_stop_time_s=30,
        subway_trains_per_line=1, hub_min_spacing_m=300.0,
    )
    assert actors == []
    assert stats == {}


# ---------------------------------------------------------------------------
# Best-effort transit schedule parsing
# ---------------------------------------------------------------------------

TRANSIT_SCHEDULE_XML = """<transitSchedule>
  <transitStops>
    <stop id="stop_a" linkRefId="12" x="0" y="0" name="Terminal A"/>
    <stop id="stop_b" linkRefId="23" x="100" y="100" name="Terminal B"/>
  </transitStops>
  <transitLine id="line_1">
    <transitRoute id="route_1">
      <routeProfile>
        <stop refId="stop_a" departureOffset="0" arrivalOffset="0"/>
        <stop refId="stop_b" departureOffset="600" arrivalOffset="590"/>
      </routeProfile>
    </transitRoute>
  </transitLine>
</transitSchedule>
"""


def test_parse_transit_schedule_reads_stops_and_lines(tmp_path):
    path = _write(tmp_path, "transitSchedule.xml", TRANSIT_SCHEDULE_XML)
    schedule = conv.parse_transit_schedule(path)

    assert set(schedule["stops"]) == {"stop_a", "stop_b"}
    assert schedule["stops"]["stop_a"]["name"] == "Terminal A"
    assert "line_1" in schedule["lines"]
    assert schedule["lines"]["line_1"][0]["route_id"] == "route_1"
    assert [s["ref_id"] for s in schedule["lines"]["line_1"][0]["stops"]] == ["stop_a", "stop_b"]


def test_build_transit_actors_creates_bus_stops_and_station(tmp_path):
    path = _write(tmp_path, "transitSchedule.xml", TRANSIT_SCHEDULE_XML)
    schedule = conv.parse_transit_schedule(path)

    actors = conv.build_transit_actors(schedule, "none", -23.55, -46.63)

    stop_actors = [a for a in actors if a["typeActor"] == "hybrid.actor.BusStop"]
    station_actors = [a for a in actors if a["typeActor"] == "hybrid.actor.BusStation"]
    assert len(stop_actors) == 2
    assert len(station_actors) == 1
    assert station_actors[0]["data"]["content"]["name"] == "line_1 / route_1"


# ---------------------------------------------------------------------------
# End-to-end: JSON output against the real base_scenario + mini fixtures
# ---------------------------------------------------------------------------


def test_end_to_end_json_against_base_scenario(tmp_path):
    output_dir = tmp_path / "out"
    args = conv.parse_args(
        [
            "--network", str(BASE_SCENARIO_DIR / "network.xml"),
            "--trips", str(BASE_SCENARIO_DIR / "trips.xml"),
            "--signals", str(BASE_SCENARIO_DIR / "signals.xml"),
            "--format", "json",
            "--output", str(output_dir),
        ]
    )
    conv.run(args)

    city_map = json.loads((output_dir / "city_map.json").read_text())
    actors = json.loads((output_dir / "actor_simulation.json").read_text())

    assert len(city_map["nodes"]) == 4544
    assert len(city_map["edges"]) == 7072
    assert city_map["directed"] is True

    type_counts: dict[str, int] = {}
    for actor in actors:
        type_counts[actor["typeActor"]] = type_counts.get(actor["typeActor"], 0) + 1

    assert type_counts["hybrid.actor.Node"] == 4544
    assert type_counts["hybrid.actor.Link"] == 7072
    assert type_counts["hybrid.actor.TrafficSignal"] == 13
    assert type_counts.get("hybrid.actor.Person", 0) > 0
    assert type_counts.get("hybrid.actor.Car", 0) > 0
    # Synthetic transit is on by default (no --no-synth-transit passed).
    assert type_counts.get("hybrid.actor.Bus", 0) > 0
    assert type_counts.get("hybrid.actor.BusStop", 0) > 0
    assert type_counts.get("hybrid.actor.BusStation", 0) > 0
    assert type_counts.get("hybrid.actor.SubwayStation", 0) > 0
    assert type_counts.get("hybrid.actor.RailLink", 0) > 0
    assert type_counts.get("hybrid.actor.Subway", 0) > 0

    # Every node latitude/longitude should land within greater São Paulo, not some
    # nonsensical projection artifact (e.g. accidental Web Mercator misprojection).
    sample = city_map["nodes"][:50]
    for node in sample:
        assert -24.2 < node["latitude"] < -23.0
        assert -47.5 < node["longitude"] < -46.0


def test_end_to_end_json_against_base_scenario_with_all_synthesis_enabled(tmp_path):
    """Coordinator-mandated validation: a single run against base_scenario/ must produce a
    representative of every entity type — person, car, bike, bus (+ stop + station), subway
    (+ station), traffic-signal, node, link — with --synth-bike-fraction and
    --synth-extra-od-trips explicitly enabled on top of the defaults-on synthetic transit."""
    output_dir = tmp_path / "out_full"
    args = conv.parse_args(
        [
            "--network", str(BASE_SCENARIO_DIR / "network.xml"),
            "--trips", str(BASE_SCENARIO_DIR / "trips.xml"),
            "--signals", str(BASE_SCENARIO_DIR / "signals.xml"),
            "--synth-bike-fraction", "0.05",
            "--synth-extra-od-trips", "20",
            "--format", "json",
            "--output", str(output_dir),
        ]
    )
    conv.run(args)

    actors = json.loads((output_dir / "actor_simulation.json").read_text())
    type_counts: dict[str, int] = {}
    for actor in actors:
        type_counts[actor["typeActor"]] = type_counts.get(actor["typeActor"], 0) + 1

    required_types = [
        "hybrid.actor.Node", "hybrid.actor.Link", "hybrid.actor.TrafficSignal",
        "hybrid.actor.Person", "hybrid.actor.Car", "hybrid.actor.Bicycle",
        "hybrid.actor.Bus", "hybrid.actor.BusStop", "hybrid.actor.BusStation",
        "hybrid.actor.SubwayStation", "hybrid.actor.RailLink", "hybrid.actor.Subway",
    ]
    missing = [t for t in required_types if type_counts.get(t, 0) == 0]
    assert missing == [], f"missing entity types in output: {missing} (counts: {type_counts})"


def test_end_to_end_json_against_mini_fixtures(tmp_path):
    output_dir = tmp_path / "out_mini"
    args = conv.parse_args(
        [
            "--network", str(BASE_SCENARIO_DIR / "mini-map.xml"),
            "--trips", str(BASE_SCENARIO_DIR / "mini-trips.xml"),
            "--format", "json",
            "--output", str(output_dir),
        ]
    )
    conv.run(args)

    city_map = json.loads((output_dir / "city_map.json").read_text())
    actors = json.loads((output_dir / "actor_simulation.json").read_text())

    assert len(city_map["nodes"]) == 6
    assert len(city_map["edges"]) == 5
    assert any(a["typeActor"] == "hybrid.actor.Person" for a in actors)
    assert any(a["typeActor"] == "hybrid.actor.Bicycle" for a in actors)


def test_end_to_end_sqlite_output_matches_scenario_db_converter_schema(tmp_path):
    output_dir = tmp_path / "out_sqlite"
    args = conv.parse_args(
        [
            "--network", str(BASE_SCENARIO_DIR / "mini-map.xml"),
            "--trips", str(BASE_SCENARIO_DIR / "mini-trips.xml"),
            "--format", "sqlite",
            "--output", str(output_dir),
        ]
    )
    conv.run(args)

    city_map_db = output_dir / "city_map.db"
    actors_db = output_dir / "actor_simulation.db"
    assert city_map_db.exists()
    assert actors_db.exists()

    conn = sqlite3.connect(city_map_db)
    try:
        node_count = conn.execute("SELECT COUNT(*) FROM city_map_node").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM city_map_edge").fetchone()[0]
    finally:
        conn.close()
    assert node_count == 6
    assert edge_count == 5

    conn = sqlite3.connect(actors_db)
    try:
        actor_count = conn.execute("SELECT COUNT(*) FROM actor_simulation").fetchone()[0]
        type_actors = {
            row[0] for row in conn.execute("SELECT DISTINCT type_actor FROM actor_simulation")
        }
    finally:
        conn.close()
    assert actor_count > 0
    assert "hybrid.actor.Node" in type_actors
    assert "hybrid.actor.Link" in type_actors
