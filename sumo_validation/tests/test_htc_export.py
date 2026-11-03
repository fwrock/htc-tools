import json

from htc_export.export import export_htc
from scenario.scenario_a import build_scenario as build_a
from scenario.scenario_b import build_scenario as build_b


def test_export_htc_writes_link_length_and_speed_1to1(tmp_path):
    scenario = build_a()
    paths = export_htc(scenario, tmp_path / "a")

    links = json.loads((tmp_path / "a" / "data" / "links_0.json").read_text())
    assert len(links) == 1
    content = links[0]["data"]["content"]
    expected_link = scenario.links[0]
    assert content["length"] == expected_link.length_m
    assert content["speedLimit"] == expected_link.speed_limit_kmh
    assert content["simulationMode"] == "MICRO"
    assert content["lanes"] == expected_link.lanes


def test_export_htc_car_route_matches_scenario_link_sequence(tmp_path):
    scenario = build_a()
    export_htc(scenario, tmp_path / "a")

    cars = json.loads((tmp_path / "a" / "data" / "cars_0.json").read_text())
    assert len(cars) == len(scenario.vehicles)

    by_depart = {v.depart_tick: v for v in scenario.vehicles}
    links = json.loads((tmp_path / "a/data/links_0.json").read_text())
    link_ids = {link.link_id: actor["id"] for link, actor in zip(scenario.links, links)}
    for car in cars:
        content = car["data"]["content"]
        depart = content["startTick"]
        vehicle = by_depart[depart]
        route_links_in_json = [pair[0] for pair in content["bestRoute"]]
        expected_links = [link_ids[lid] for lid in vehicle.route_link_ids]
        assert route_links_in_json == expected_links


def test_export_htc_scenario_b_wires_traffic_signal_to_node_connection(tmp_path):
    scenario = build_b()
    export_htc(scenario, tmp_path / "b")

    nodes = json.loads((tmp_path / "b" / "data" / "nodes_0.json").read_text())
    signals = json.loads((tmp_path / "b" / "data" / "traffic_signals_0.json").read_text())

    assert len(signals) == 1
    signal_content = signals[0]["data"]["content"]
    phase_origin = signal_content["phases"][0]["origin"]
    assert isinstance(phase_origin, int)
    assert str(phase_origin) in signal_content["signalStates"]

    node_ids = {node.node_id: actor["id"] for node, actor in zip(scenario.nodes, nodes)}
    links = json.loads((tmp_path / "b/data/links_0.json").read_text())
    link_ids = {link.link_id: actor["id"] for link, actor in zip(scenario.links, links)}
    approach, outgoing = str(link_ids["link_ab"]), str(link_ids["link_bc"])
    node_b = next(n for n in nodes if n["id"] == node_ids["n_b"])
    connections = node_b["data"]["content"]["connections"]
    # Keyed by the OUTGOING link (link_bc, what a car is about to enter next), not the
    # SUMO-style approach edge (link_ab) `controlled_link_id` names — that's how
    # NodeEventHandler.handleRequestSignalState/CarSignalHandler's getNextLinkFn actually look
    # it up. Using the approach edge directly used to silently mismatch the lookup, so every
    # car's signal-state request always fell through to "assume Green" regardless of phase.
    assert approach not in connections
    assert connections[outgoing]["id"] == phase_origin

    # approachConnections is the opposite direction: keyed by the SUMO-style approach edge
    # (link_ab, the link a car is arriving ON), used by NodeEventHandler.handleReceiveSignalChangeStatus
    # to notify the correct Link actor's MICRO signalAtExit braking. Getting this one keyed
    # by the outgoing link instead would brake vehicles already past the intersection.
    approach_connections = node_b["data"]["content"]["approachConnections"]
    assert outgoing not in approach_connections
    assert approach_connections[approach]["id"] == phase_origin

    signal_nodes = signal_content["nodes"]
    assert signal_nodes == [node_ids["n_b"]]
    # TrafficSignal actor must declare the node as a dependency, or getDependency()
    # throws ShardActorId-not-found at runtime (verified empirically, see README).
    assert str(node_ids["n_b"]) in signals[0]["dependencies"]


def test_export_htc_scenario_without_signal_has_no_traffic_signal_file(tmp_path):
    scenario = build_a()
    export_htc(scenario, tmp_path / "a")
    assert not (tmp_path / "a" / "data" / "traffic_signals_0.json").exists()
