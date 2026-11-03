"""Scenario A: minimal car-following isolation.

2 nodes, 1 straight single-lane link, no traffic signal. 4 vehicles of ONE vehicle type
enter with a fixed time gap (leader/follower pairs), so the Krauss gap-closing/following
behavior is actually exercised (not just free-flow entry).
"""

from __future__ import annotations

from .model import Link, Node, Scenario, Vehicle, VehicleType

LINK_LENGTH_M = 300.0
SPEED_LIMIT_KMH = 50.0
N_VEHICLES = 4
DEPART_GAP_S = 6
DURATION_S = 180


def build_scenario() -> Scenario:
    nodes = (
        Node(node_id="n_origin", x=0.0, y=0.0),
        Node(node_id="n_dest", x=LINK_LENGTH_M, y=0.0),
    )
    links = (
        Link(
            link_id="link_main",
            from_node="n_origin",
            to_node="n_dest",
            length_m=LINK_LENGTH_M,
            speed_limit_kmh=SPEED_LIMIT_KMH,
            lanes=1,
        ),
    )
    vehicle_types = (VehicleType(type_id="car_krauss"),)
    vehicles = tuple(
        Vehicle(
            vehicle_id=f"car_{i}",
            depart_tick=i * DEPART_GAP_S,
            route_link_ids=("link_main",),
        )
        for i in range(N_VEHICLES)
    )
    return Scenario(
        scenario_id="scenario_a",
        description=(
            "Minimal car-following isolation: 2 nodes, 1 straight 300m single-lane link, "
            "no signal, 4 vehicles entering 6s apart to exercise leader/follower gap-closing."
        ),
        nodes=nodes,
        links=links,
        vehicle_types=vehicle_types,
        vehicles=vehicles,
        duration_s=DURATION_S,
    )
