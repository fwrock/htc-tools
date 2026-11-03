"""Canonical Scenario -> HTC scenario JSON (hand-written, not routed through the buggy
scripts/sumo_convert/ OSM converter — see tools/sumo_validation/README.md).

JSON shapes below were verified against actual HTC source/behavior, not guessed:
  - Node/Link/Car JSON shape: hyperbolic-time-chamber/simulations/input/sqlite_validation_test/
    data/{nodes,links,cars}_0.json (existing working example) plus a hand-run smoke test
    against the assembled jar (see README "How this was verified").
  - city_map.json shape: same directory's data/city_map.json, consumed by
    model.hybrid.util.CityMapUtil (nodesById / edgeLabelsById).
  - TrafficSignalState / Node.connections / Node.signals / Identify: read directly from
    TrafficSignal.scala, TrafficSignalPhaseHandler.scala, NodeEventHandler.scala,
    core/entity/actor.proto (Identify), and confirmed working via smoke test.
  - `sourceType: "json"` (as opposed to "sqlite") is a supported DataSourceTypeEnum value
    (core/enumeration/DataSourceTypeEnum.scala) backed by JsonLoadData, which reads the
    plain JSON array files directly — no sqlite database needed for this scenario size.

Known HTC behavior baked into this exporter (see README for full discussion):
  - `bestRoute` is pre-populated per vehicle (list of [linkId, nodeId] pairs) so cars skip
    A*/GPS routing entirely and follow the scenario's exact intended path
    (Car.requestRoute() branch: `if (state.bestRoute.nonEmpty) ...`).
  - Every Link is set to MICRO mode from tick 0 (`"simulationMode": "MICRO"`), confirmed
    read by `LinkState.isMicroMode` / `Link.onInitialize`.
  - Krauss params (maxAcceleration/maxDeceleration/minGap/reactionTime/desiredVelocity) are
    NOT threaded through Car/MicroCarState JSON on this code path — the live MICRO tick
    loop (DefaultMicroSimulationStrategy.processMicroLane) hardcodes minGap=2.0m,
    reactionTime=1.0s, desiredVelocity=link.speedLimit/3.6, and reads
    maxAcceleration/maxDeceleration from `Movable.microMaxAcceleration/microMaxDeceleration`
    (Car does not override these — fixed at 2.6 / 4.5 m/s^2). We still emit `microState`
    with those same values on the Car JSON for documentation/forward-compat, but the
    scenario's `VehicleType` defaults are deliberately set to match what HTC actually uses
    so the comparison is apples-to-apples regardless.
"""

from __future__ import annotations

import json
from pathlib import Path

from scenario.model import Scenario

class ScenarioIdRegistry:
    """Assigns a globally-unique `long` id per (kind, source) pair (see the
    sao-paulo/toulouse generators — identical convention). Deterministic: first
    request allocates, later requests return the same id.
    """

    def __init__(self) -> None:
        self._next: int = 1
        self._ids: dict = {}

    def id_for(self, kind: str, source_id) -> int:
        key = (kind, str(source_id))
        v = self._ids.get(key)
        if v is None:
            v = self._next
            self._next += 1
            self._ids[key] = v
        return v


_IDS = ScenarioIdRegistry()


def _nid(n: str) -> int:
    return _IDS.id_for("node", n)


def _lid(l: str) -> int:
    return _IDS.id_for("link", l)


def _cid(c: str) -> int:
    return _IDS.id_for("car", c)


def _sid(s: str) -> int:
    return _IDS.id_for("signal", s)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _build_city_map(scenario: Scenario) -> dict:
    nodes = [
        {
            "id": _nid(n.node_id),
            "classType": "hybrid.actor.Node",
            "resourceId": _nid(n.node_id),
            "latitude": n.y,
            "longitude": n.x,
        }
        for n in scenario.nodes
    ]
    edges = [
        {
            "source_id": _nid(l.from_node),
            "target_id": _nid(l.to_node),
            "weight": l.length_m,
            "label": {
                "id": _lid(l.link_id),
                "resourceId": _lid(l.link_id),
                "classType": "hybrid.actor.Link",
                "length": l.length_m,
            },
        }
        for l in scenario.links
    ]
    return {"nodes": nodes, "edges": edges, "directed": True}


def _node_connections_and_signals(scenario: Scenario, node_id: str, outgoing_links: list[str]) -> dict:
    """Node.connections: HTC keys this by the OUTGOING link a car is about to enter next
    (`NodeEventHandler.handleRequestLinkAccess` looks it up via `CarSignalHandler`'s
    `getNextLinkFn` — see NodeEventHandler.scala). `TrafficLight.controlled_link_id` is the
    SUMO-style APPROACH edge instead (the link a car is arriving ON, matching SUMO's own
    `.tll.xml` convention) — using it directly as the connections key silently mismatched HTC's
    lookup, so every car's signal-state request fell through the "no connection entry —
    uncontrolled intersection" fallback and always got Green, regardless of the actual signal
    phase (see docs/KNOWN_GAPS.md, "TrafficSignalPhaseHandler Never Transitions Past Its First
    Phase" investigation notes). Map the phase identity onto this node's outgoing link(s)
    instead; empty for uncontrolled nodes/links."""
    connections = {}
    for tl in scenario.traffic_lights:
        if tl.node_id == node_id:
            for out_link in outgoing_links:
                connections[out_link] = {
                    "id": _phase_origin(tl),
                    "resourceId": "",
                    "classType": "",
                    "actorRef": "",
                    "actorType": "",
                }
    return connections


def _node_approach_connections(scenario: Scenario, node_id: str) -> dict:
    """Node.approachConnections: keyed by the APPROACH link (`TrafficLight.controlled_link_id`,
    the link a car is arriving ON — SUMO's own `.tll.xml` convention), the opposite direction of
    `connections` above. `NodeEventHandler.handleReceiveSignalChangeStatus` uses this map (not
    `connections`) to find which Link actor to notify of a phase change for MICRO's
    `signalAtExit` virtual-stopped-leader braking — notifying the outgoing link instead brakes
    vehicles already past the intersection, mid-transit on a link with no signal of its own. See
    docs/KNOWN_GAPS.md, "NodeEventHandler Notifies the Wrong Link on Signal Phase Change"."""
    approach_connections = {}
    for tl in scenario.traffic_lights:
        if tl.node_id == node_id:
            approach_connections[_lid(tl.controlled_link_id)] = {
                "id": _phase_origin(tl),
                "resourceId": "",
                "classType": "",
                "actorRef": "",
                "actorType": "",
            }
    return approach_connections


def _phase_origin(tl) -> int:
    # Phase.origin and the protobuf Identify.id both require Long identities.
    return _IDS.id_for("phase", tl.signal_id)


def _build_nodes(scenario: Scenario) -> list[dict]:
    incoming_links: dict[str, list[str]] = {n.node_id: [] for n in scenario.nodes}
    outgoing_links: dict[str, list[str]] = {n.node_id: [] for n in scenario.nodes}
    for l in scenario.links:
        incoming_links[l.to_node].append(_lid(l.link_id))
        outgoing_links[l.from_node].append(_lid(l.link_id))

    out = []
    for n in scenario.nodes:
        out.append(
            {
                "id": _nid(n.node_id),
                "typeActor": "hybrid.actor.Node",
                "data": {
                    "dataType": "model.hybrid.entity.state.NodeState",
                    "content": {
                        "startTick": 0,
                        "scheduleOnTimeManager": False,
                        "latitude": n.y,
                        "longitude": n.x,
                        "links": incoming_links[n.node_id],
                        "connections": _node_connections_and_signals(
                            scenario, n.node_id, outgoing_links[n.node_id]
                        ),
                        "approachConnections": _node_approach_connections(scenario, n.node_id),
                        "signals": {},
                        "busStops": {},
                        "subwayStations": {},
                    },
                },
                "dependencies": {},
            }
        )
    return out


def _build_links(scenario: Scenario) -> list[dict]:
    out = []
    for l in scenario.links:
        out.append(
            {
                "id": _lid(l.link_id),
                "typeActor": "hybrid.actor.Link",
                "data": {
                    "dataType": "model.hybrid.entity.state.LinkState",
                    "content": {
                        "startTick": 0,
                        "scheduleOnTimeManager": False,
                        "from": _nid(l.from_node),
                        "to": _nid(l.to_node),
                        "length": l.length_m,
                        "lanes": l.lanes,
                        # LinkState.speedLimit is km/h (Link micro physics divides by 3.6).
                        "speedLimit": l.speed_limit_kmh,
                        "capacity": 600.0,
                        "freeSpeed": l.speed_limit_kmh,
                        "currentSpeed": l.speed_limit_kmh,
                        "congestionFactor": 1.0,
                        "registered": [],
                        "simulationMode": "MICRO",
                        "microTimeStep": 0.1,
                        "microTicksPerGlobalTick": 10,
                        "vehiclesByLane": {},
                        "laneConfigurations": [
                            {"laneId": i, "type": "normal", "width": 3.5, "speedLimit": None}
                            for i in range(l.lanes)
                        ],
                    },
                },
                "dependencies": {
                    "from_node": {"id": _nid(l.from_node), "classType": "hybrid.actor.Node"},
                    "to_node": {"id": _nid(l.to_node), "classType": "hybrid.actor.Node"},
                },
            }
        )
    return out


def _build_cars(scenario: Scenario) -> list[dict]:
    out = []
    for v in scenario.vehicles:
        vt = scenario.vehicle_type(v.vehicle_type)
        route_links = [scenario.link(lid) for lid in v.route_link_ids]
        origin_node = route_links[0].from_node
        destination_node = route_links[-1].to_node
        best_route = []
        for l in route_links:
            best_route.append([_lid(l.link_id), _nid(l.to_node)])

        out.append(
            {
                "id": _cid(v.vehicle_id),
                "typeActor": "hybrid.actor.Car",
                "data": {
                    "dataType": "model.hybrid.entity.state.CarState",
                    "content": {
                        "startTick": v.depart_tick,
                        "scheduleOnTimeManager": True,
                        "origin": _nid(origin_node),
                        "destination": _nid(destination_node),
                        "actorType": "Car",
                        "size": vt.length_m,
                        "currentSimulationMode": "MESO",
                        # Left None (not pre-populated): CarState.microState is only read by
                        # CarMicroHandler.handleMicroEnterLink for its *velocity* field
                        # (state.microState.map(_.velocity)), all other Krauss params are
                        # rebuilt from scratch on every link entry using hardcoded values
                        # (see module docstring / README "Known gap: Krauss params are
                        # hardcoded on the HTC side"). Pre-populating this would be
                        # misleading busywork, not a real per-vehicle configuration knob.
                        "microState": None,
                        "status": "RouteWaiting",
                        "bestRoute": best_route,
                        "currentNode": _nid(origin_node),
                        "distance": 0.0,
                    },
                },
                "dependencies": {
                    "from_node": {"id": _nid(origin_node), "classType": "hybrid.actor.Node"},
                    "to_node": {"id": _nid(destination_node), "classType": "hybrid.actor.Node"},
                },
            }
        )
    return out


def _build_traffic_signals(scenario: Scenario) -> list[dict]:
    out = []
    for tl in scenario.traffic_lights:
        origin = _phase_origin(tl)
        out.append(
            {
                "id": _sid(tl.signal_id),
                "typeActor": "hybrid.actor.TrafficSignal",
                "data": {
                    "dataType": "model.hybrid.entity.state.TrafficSignalState",
                    "content": {
                        "startTick": 0,
                        "cycleDuration": tl.cycle_s,
                        "offset": tl.offset_s,
                        "nodes": [_nid(tl.node_id)],
                        "phases": [
                            {
                                "origin": origin,
                                "greenStart": 0,
                                "greenDuration": tl.green_s,
                                "state": "Green",
                            }
                        ],
                        # Initial state deliberately Red so the first phase transition
                        # (computed from greenStart/greenDuration) detects the Green window
                        # and notifies the node/link — see TrafficSignalPhaseHandler.scala.
                        "signalStates": {
                            origin: {"state": "Red", "remainingTime": tl.green_s, "nextTick": 0}
                        },
                    },
                },
                "dependencies": {
                    _nid(tl.node_id): {"id": _nid(tl.node_id), "classType": "hybrid.actor.Node"}
                },
            }
        )
    return out


def export_htc(scenario: Scenario, out_dir: Path) -> dict[str, Path]:
    """Writes an HTC scenario directory (simulation.json + data/*.json) and returns the
    paths that the runner needs (simulation_json, city_map_json)."""
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"

    city_map_path = data_dir / "city_map.json"
    nodes_path = data_dir / "nodes_0.json"
    links_path = data_dir / "links_0.json"
    cars_path = data_dir / "cars_0.json"
    signals_path = data_dir / "traffic_signals_0.json"

    _write_json(city_map_path, _build_city_map(scenario))
    _write_json(nodes_path, _build_nodes(scenario))
    _write_json(links_path, _build_links(scenario))
    _write_json(cars_path, _build_cars(scenario))

    actors_data_sources = [
        {
            "id": "nodes_0",
            "classType": "hybrid.actor.Node",
            "creationType": "LoadBalancedDistributed",
            "loadingStrategy": "EAGER",
            "entityLifecycle": "STATIC",
            "dataSource": {"sourceType": "json", "info": {"path": str(nodes_path)}},
        },
        {
            "id": "links_0",
            "classType": "hybrid.actor.Link",
            "creationType": "LoadBalancedDistributed",
            "loadingStrategy": "EAGER",
            "entityLifecycle": "STATIC",
            "dataSource": {"sourceType": "json", "info": {"path": str(links_path)}},
        },
    ]

    if scenario.traffic_lights:
        _write_json(signals_path, _build_traffic_signals(scenario))
        actors_data_sources.append(
            {
                "id": "traffic_signals_0",
                "classType": "hybrid.actor.TrafficSignal",
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": "EAGER",
                "entityLifecycle": "STATIC",
                "dataSource": {"sourceType": "json", "info": {"path": str(signals_path)}},
            }
        )

    actors_data_sources.append(
        {
            "id": "cars_0",
            "classType": "hybrid.actor.Car",
            "creationType": "LoadBalancedDistributed",
            "loadingStrategy": "PROGRESSIVE",
            "entityLifecycle": "DYNAMIC",
            "dataSource": {"sourceType": "json", "info": {"path": str(cars_path)}},
        }
    )

    simulation = {
        "id": scenario.scenario_id,
        "name": scenario.description,
        "startTick": 0,
        "endTick": scenario.duration_s,
        "startRealTime": "2025-01-01T00:00:00.000",
        "timeUnit": "seconds",
        "timeStep": 1.0,
        "duration": scenario.duration_s,
        "tickDuration": 1.0,
        "randomSeed": 42,
        "actorsDataSources": actors_data_sources,
        "cityMapFile": str(city_map_path),
        "warmupTargets": ["org.interscity.htc.model.hybrid.util.CityMapUtil#warmUp"],
    }
    simulation_path = out_dir / "simulation.json"
    _write_json(simulation_path, simulation)

    return {
        "simulation_json": simulation_path,
        "city_map_json": city_map_path,
        "out_dir": out_dir,
    }
