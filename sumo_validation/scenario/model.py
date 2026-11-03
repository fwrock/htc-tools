"""Canonical, simulator-agnostic scenario definitions.

These dataclasses are the single source of truth for topology, vehicle-type (Krauss)
parameters, and vehicle departures. Both `htc_export` and `sumo_export` read from the
*same* `Scenario` instance so that link length/speed and car-following parameters are
asserted identical on both sides instead of being translated through a converter script.

Units: SI throughout (meters, seconds, m/s, m/s^2) except `Link.speed_limit_kmh`, which
is stored in km/h because that is the unit HTC's `LinkState.speedLimit` field uses on
disk (see hyperbolic-time-chamber CarMicroHandler.scala comment: "speedLimit from
LinkState is stored in km/h; Link micro physics converts with /3.6"). The exporters do
the km/h <-> m/s conversion at the boundary, not the scenario model.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    node_id: str
    x: float  # meters, local planar coordinates (also reused as SUMO x/y directly)
    y: float


@dataclass(frozen=True)
class Link:
    link_id: str
    from_node: str
    to_node: str
    length_m: float
    speed_limit_kmh: float
    lanes: int = 1


@dataclass(frozen=True)
class VehicleType:
    """Krauss car-following parameters.

    Defaults are deliberately set to the values HTC's live MICRO simulation path
    (`DefaultMicroSimulationStrategy.processMicroLane`) actually uses internally,
    which are NOT read from any per-vehicle scenario config today (see
    tools/sumo_validation/README.md "Known gap: Krauss params are hardcoded on the
    HTC side" for the full trace). Keeping these as the scenario's vehicle-type
    values means the SUMO side is deliberately matched to what HTC *actually does*,
    not to what a scenario author might naively configure on the HTC side and have
    silently ignored.
    """

    type_id: str = "car_krauss"
    max_accel_mps2: float = 2.6
    max_decel_mps2: float = 4.5
    min_gap_m: float = 2.0
    reaction_time_s: float = 1.0  # SUMO "tau"
    length_m: float = 4.5
    sigma: float = 0.0  # SUMO driver imperfection; 0 = deterministic


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    depart_tick: int  # simulated seconds (HTC startTick == SUMO depart, both integer seconds)
    route_link_ids: tuple[str, ...]  # ordered link ids from origin to destination
    vehicle_type: str = "car_krauss"
    initial_velocity_mps: float = 0.0


@dataclass(frozen=True)
class TrafficLight:
    """A single signalized approach.

    `node_id` is the intersection node. `controlled_link_id` is the single incoming
    link whose exit is gated (HTC's Node/TrafficSignal model gates one *outgoing
    connection* per phase.origin at a time; a single-approach light is sufficient for
    scenario B, which isolates Krauss-vs-TLS interaction on ONE approach).
    `cycle_s` is the full cycle length; `green_s` is the green duration starting at
    the beginning of the cycle (phase 0..green_s = Green, green_s..cycle_s = Red).
    `offset_s` shifts the cycle start.
    """

    signal_id: str
    node_id: str
    controlled_link_id: str
    cycle_s: int
    green_s: int
    offset_s: int = 0


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]
    vehicle_types: tuple[VehicleType, ...]
    vehicles: tuple[Vehicle, ...]
    duration_s: int
    traffic_lights: tuple[TrafficLight, ...] = field(default_factory=tuple)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(f"Unknown node: {node_id}")

    def link(self, link_id: str) -> Link:
        for l in self.links:
            if l.link_id == link_id:
                return l
        raise KeyError(f"Unknown link: {link_id}")

    def vehicle_type(self, type_id: str) -> VehicleType:
        for vt in self.vehicle_types:
            if vt.type_id == type_id:
                return vt
        raise KeyError(f"Unknown vehicle type: {type_id}")
