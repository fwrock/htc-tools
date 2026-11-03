"""Scenario B: small signalized intersection.

Same vehicle/Krauss params as scenario A. Adds one traffic-signal-controlled crossing on
the approach link, so HTC's signal-gating interacts with Krauss stopping behavior, and can
be checked against SUMO's TLS logic. Topology: A -> B (signalized approach, MICRO) ->
C (short departure link, kept MICRO too so the whole trip is directly comparable).

Signal cycle: 60s, Green for the first 30s of each cycle, Red for the last 30s. Vehicle
departures are spread across the cycle so some vehicles arrive during Green (pass through
close to free-flow) and some during Red (must queue and release with the next Green).
"""

from __future__ import annotations

from .model import Link, Node, Scenario, TrafficLight, Vehicle, VehicleType

APPROACH_LENGTH_M = 200.0
DEPART_LENGTH_M = 100.0
SPEED_LIMIT_KMH = 50.0
CYCLE_S = 60
GREEN_S = 30
DEPART_TICKS = (0, 10, 20, 30, 40, 50)
DURATION_S = 240


def build_scenario() -> Scenario:
    nodes = (
        Node(node_id="n_a", x=0.0, y=0.0),
        Node(node_id="n_b", x=APPROACH_LENGTH_M, y=0.0),
        Node(node_id="n_c", x=APPROACH_LENGTH_M + DEPART_LENGTH_M, y=0.0),
    )
    links = (
        Link(
            link_id="link_ab",
            from_node="n_a",
            to_node="n_b",
            length_m=APPROACH_LENGTH_M,
            speed_limit_kmh=SPEED_LIMIT_KMH,
            lanes=1,
        ),
        Link(
            link_id="link_bc",
            from_node="n_b",
            to_node="n_c",
            length_m=DEPART_LENGTH_M,
            speed_limit_kmh=SPEED_LIMIT_KMH,
            lanes=1,
        ),
    )
    vehicle_types = (VehicleType(type_id="car_krauss"),)
    vehicles = tuple(
        Vehicle(
            vehicle_id=f"car_{i}",
            depart_tick=depart,
            route_link_ids=("link_ab", "link_bc"),
        )
        for i, depart in enumerate(DEPART_TICKS)
    )
    traffic_lights = (
        TrafficLight(
            signal_id="sig_b",
            node_id="n_b",
            controlled_link_id="link_ab",
            cycle_s=CYCLE_S,
            green_s=GREEN_S,
        ),
    )
    return Scenario(
        scenario_id="scenario_b",
        description=(
            "Small signalized intersection: A->B (signalized 200m approach) -> B->C "
            "(100m departure), same Krauss params as scenario A, 60s signal cycle "
            "(30s green / 30s red), 6 vehicles spread across the cycle."
        ),
        nodes=nodes,
        links=links,
        vehicle_types=vehicle_types,
        vehicles=vehicles,
        duration_s=DURATION_S,
        traffic_lights=traffic_lights,
    )
