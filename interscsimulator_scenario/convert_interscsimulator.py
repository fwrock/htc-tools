#!/usr/bin/env python3
"""Convert InterSCSimulator (MATSim-style) scenario XML into HTC (Hyperbolic Time Chamber)
scenario input: a `city_map` (routing graph) and an `actor_simulation` (agent) dataset, in
JSON and/or SQLite, matching the two schemas already defined by
`tools/scenario-db-converter/convert.py`.

Priority (explicit, per the platform maintainer): the road-network map (nodes/links ->
city_map + Node/Link actors) and traffic-signal actors are the robust, well-tested part of
this converter. Person/car OD-trip conversion and bus/metro support are best-effort — see
the module docstrings on `build_person_and_vehicle_actors` and `parse_transit_schedule`.

Input formats handled (confirmed against tools/interscsimulator_scenario/input/base_scenario):
  network.xml   <nodes><node id x y/></nodes>
                <links capperiod effectivecellsize effectivelanewidth>
                  <link id from to length freespeed capacity permlanes oneway modes/>
                </links>
  trips.xml     <scsimulator_matrix><trip name origin destination link_origin count start
                  mode digital_rails_capable/></scsimulator_matrix>
  signals.xml   <traffic-signals><signal cycle_duration offset>
                  <nodes><node id/></nodes>
                  <phases><phase origin green_duration green_start/></phases>
                </signal></traffic-signals>

No example bus-line/bus-stop/metro input file ships with this scenario family (confirmed by
listing tools/interscsimulator_scenario/input/base_scenario/ — only network/trips/signals/
config/arrivals/digital-rails variants are present). Best-effort MATSim
`transitSchedule.xml`-like support is implemented in `parse_transit_schedule` and exercised
only against hand-built fixtures in tests/, never against real InterSCSimulator output.

Coordinate system of network.xml: NOT assumed. Confirmed empirically by treating
base_scenario/network.xml node ids as literal OpenStreetMap node ids (e.g. node 60609643,
used both here and in signals.xml as a "Paulista x Bela Cintra" traffic-signal node, is
tagged highway=traffic_signals in live OSM — see api.openstreetmap.org/api/0.6/node/
60609643.json). Fetching live OSM lat/lon for ~17 nodes spread across the network's ~30km
extent and least-squares-fitting an affine map from EPSG:32723 (WGS84 / UTM zone 23S)
easting/northing to network.xml (x, y) gives out-of-sample residuals of 1.5-11m. The same
fit attempted against plain EPSG:3857 (Web Mercator) gave residuals >2km — ruled out. See
`matsim_sp_xy_to_lonlat` for the exact affine coefficients and derivation notes. This fit is
specific to this São Paulo (Avenida Paulista area) scenario family, not a general
InterSCSimulator convention — a network.xml from elsewhere may use a different projection
entirely, hence the `--projection` CLI flag rather than a hardcoded assumption.

Usage:
  python convert_interscsimulator.py \\
      --network input/base_scenario/network.xml \\
      --trips input/base_scenario/trips.xml \\
      --signals input/base_scenario/signals.xml \\
      --format json --output output/base_scenario_json

  python convert_interscsimulator.py --network input/base_scenario/mini-map.xml \\
      --trips input/base_scenario/mini-trips.xml \\
      --format sqlite --output output/mini.db --projection none

See README.md for full flag reference and design notes.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from pyproj import Transformer

# ---------------------------------------------------------------------------
# Coordinate reprojection
# ---------------------------------------------------------------------------

# Empirically-derived affine correction mapping this scenario family's network.xml (x, y)
# onto EPSG:32723 (WGS84 / UTM zone 23S) easting/northing:
#   x = _AFFINE_A*E + _AFFINE_B*N + _AFFINE_C
#   y = _AFFINE_D*E + _AFFINE_E*N + _AFFINE_F
# Fitted via least squares against 11 nodes' live OSM coordinates; validated out-of-sample
# against 6 further nodes with residuals of 1.5-11m over a ~30km extent. See module
# docstring for the full derivation. NOT a documented InterSCSimulator convention.
_AFFINE_A = 1.00405166
_AFFINE_B = -0.01824173
_AFFINE_C = -7_540_779.31
_AFFINE_D = 0.01883189
_AFFINE_E = 0.99532967
_AFFINE_F = -10_213_933.6

_UTM23S_TO_WGS84 = Transformer.from_crs("EPSG:32723", "EPSG:4326", always_xy=True)

# Above this magnitude (meters), (x, y) is assumed to be this scenario family's projected
# coordinate system rather than a small synthetic/local grid (e.g. demo-bikes-map.xml uses
# x/y in the 0-50 range and is not geographic at all).
LARGE_COORD_THRESHOLD_M = 10_000.0

METERS_PER_DEGREE_LAT = 111_320.0


def matsim_sp_xy_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """Invert the affine correction, then inverse-project EPSG:32723 -> lon/lat (EPSG:4326)."""
    det = _AFFINE_A * _AFFINE_E - _AFFINE_B * _AFFINE_D
    dx = x - _AFFINE_C
    dy = y - _AFFINE_F
    easting = (_AFFINE_E * dx - _AFFINE_B * dy) / det
    northing = (-_AFFINE_D * dx + _AFFINE_A * dy) / det
    lon, lat = _UTM23S_TO_WGS84.transform(easting, northing)
    return lon, lat


def local_xy_to_lonlat(x: float, y: float, anchor_lat: float, anchor_lon: float) -> Tuple[float, float]:
    """Treat (x, y) as a (east, north) meter offset from an anchor lon/lat, using a flat-earth
    equirectangular approximation. Adequate only for small synthetic/local grids (tens to a
    few hundred meters across) such as demo-bikes-map.xml — do not use for city-scale nets."""
    meters_per_degree_lon = METERS_PER_DEGREE_LAT * math.cos(math.radians(anchor_lat))
    lat = anchor_lat + y / METERS_PER_DEGREE_LAT
    lon = anchor_lon + x / (meters_per_degree_lon or 1.0)
    return lon, lat


def project_xy(
    x: float, y: float, mode: str, anchor_lat: float, anchor_lon: float
) -> Tuple[float, float]:
    if mode == "matsim-sp-utm23s":
        return matsim_sp_xy_to_lonlat(x, y)
    if mode == "none":
        return local_xy_to_lonlat(x, y, anchor_lat, anchor_lon)
    if mode == "auto":
        if abs(x) > LARGE_COORD_THRESHOLD_M or abs(y) > LARGE_COORD_THRESHOLD_M:
            return matsim_sp_xy_to_lonlat(x, y)
        return local_xy_to_lonlat(x, y, anchor_lat, anchor_lon)
    raise ValueError(f"Unknown projection mode: {mode!r}")


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def parse_network(path: Path) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Parse a MATSim/InterSCSimulator network.xml.

    Returns (nodes, links) keyed by their raw (string) InterSCSimulator ids:
      nodes[id]  = {"x": float, "y": float}
      links[id]  = {"from", "to", "length", "freespeed", "capacity", "permlanes", "oneway", "modes"}
    """
    tree = ET.parse(path)
    root = tree.getroot()

    nodes: Dict[str, dict] = {}
    for node_el in root.findall("./nodes/node"):
        nid = node_el.get("id")
        if nid is None:
            continue
        nodes[nid] = {"x": float(node_el.get("x", "0")), "y": float(node_el.get("y", "0"))}

    links: Dict[str, dict] = {}
    for link_el in root.findall("./links/link"):
        lid = link_el.get("id")
        if lid is None:
            continue
        links[lid] = {
            "from": link_el.get("from"),
            "to": link_el.get("to"),
            "length": float(link_el.get("length", "0")),
            "freespeed": float(link_el.get("freespeed", "0")),
            "capacity": float(link_el.get("capacity", "0")),
            "permlanes": float(link_el.get("permlanes", "1")),
            "oneway": link_el.get("oneway", "1"),
            "modes": link_el.get("modes", "car"),
        }
    return nodes, links


def parse_trips(path: Path) -> List[dict]:
    """Parse a scsimulator_matrix trips.xml into a list of trip dicts:
    {"name", "origin", "destination", "link_origin", "count", "start", "mode",
     "digital_rails_capable"}."""
    tree = ET.parse(path)
    root = tree.getroot()
    trips: List[dict] = []
    for trip_el in root.findall("./trip"):
        trips.append(
            {
                "name": trip_el.get("name"),
                "origin": trip_el.get("origin"),
                "destination": trip_el.get("destination"),
                "link_origin": trip_el.get("link_origin"),
                "count": int(trip_el.get("count", "1")),
                "start": float(trip_el.get("start", "0")),
                "mode": trip_el.get("mode", "car"),
                "digital_rails_capable": trip_el.get("digital_rails_capable", "false") == "true",
            }
        )
    return trips


def parse_signals(path: Path) -> List[dict]:
    """Parse a traffic-signals.xml into a list of signal dicts:
    {"cycle_duration", "offset", "nodes": [node_id, ...],
     "phases": [{"origin", "green_duration", "green_start"}, ...]}."""
    tree = ET.parse(path)
    root = tree.getroot()
    signals: List[dict] = []
    for signal_el in root.findall("./signal"):
        node_ids = [n.get("id") for n in signal_el.findall("./nodes/node") if n.get("id")]
        phases = [
            {
                "origin": p.get("origin"),
                "green_duration": float(p.get("green_duration", "0")),
                "green_start": float(p.get("green_start", "0")),
            }
            for p in signal_el.findall("./phases/phase")
        ]
        if not node_ids or node_ids == ["empty"]:
            # empty-signals.xml fixture: a placeholder signal with no real nodes/phases.
            continue
        signals.append(
            {
                "cycle_duration": float(signal_el.get("cycle_duration", "90")),
                "offset": float(signal_el.get("offset", "0")),
                "nodes": node_ids,
                "phases": phases,
            }
        )
    return signals


# ---------------------------------------------------------------------------
# Actor id helpers
# ---------------------------------------------------------------------------

class ScenarioIdRegistry:
    """Assigns a globally-unique `long` id per (kind, source) pair (see the
    sao-paulo/toulouse generators — identical convention). Deterministic: first
    request allocates, later requests return the same id.
    """

    def __init__(self) -> None:
        self._next: int = 1
        self._ids: Dict[Tuple[str, str], int] = {}

    def id_for(self, kind: str, source_id) -> int:
        key = (kind, str(source_id))
        v = self._ids.get(key)
        if v is None:
            v = self._next
            self._next += 1
            self._ids[key] = v
        return v


# Module-level registry: every build_* function shares the same counter so cross-references
# (e.g. a link's `from`/`to` node ids) resolve to the same `long` id as the node actor itself.
_IDS = ScenarioIdRegistry()


def node_actor_id(raw_id: str) -> int:
    return _IDS.id_for("node", raw_id)


def link_actor_id(raw_id: str) -> int:
    return _IDS.id_for("link", raw_id)


# ---------------------------------------------------------------------------
# city_map + Node/Link actors (priority: robust, well-tested)
# ---------------------------------------------------------------------------


def build_node_actors(
    nodes: Dict[str, dict],
    links: Dict[str, dict],
    projection: str,
    anchor_lat: float,
    anchor_lon: float,
) -> Tuple[List[dict], Dict[str, Tuple[float, float]]]:
    """Build hybrid.actor.Node actors. Returns (actors, {raw_node_id: (lat, lon)})."""
    outgoing: Dict[str, List[str]] = defaultdict(list)
    for lid, link in links.items():
        if link["from"] is not None:
            outgoing[link["from"]].append(link_actor_id(lid))

    actors: List[dict] = []
    coords: Dict[str, Tuple[float, float]] = {}
    for nid, n in nodes.items():
        lon, lat = project_xy(n["x"], n["y"], projection, anchor_lat, anchor_lon)
        coords[nid] = (lat, lon)
        actors.append(
            {
                "id": node_actor_id(nid),
                "typeActor": "hybrid.actor.Node",
                "data": {
                    "dataType": "model.hybrid.entity.state.NodeState",
                    "content": {
                        "startTick": 0,
                        "scheduleOnTimeManager": False,
                        "latitude": lat,
                        "longitude": lon,
                        "links": outgoing.get(nid, []),
                        "connections": {},
                        "approachConnections": {},
                        "signals": {},
                        "busStops": {},
                        "subwayStations": {},
                    },
                },
                "dependencies": {},
            }
        )
    return actors, coords


def _mps_to_kmh(mps: float) -> float:
    return mps * 3.6


def build_link_actors(links: Dict[str, dict]) -> List[dict]:
    """Build hybrid.actor.Link actors.

    network.xml `freespeed` is m/s (MATSim convention); converted to km/h here because both
    existing HTC scenario generators (generate_tiny_validation_scenario.py's grid helper and
    generate_hybrid_input.py's OSM importer) populate LinkState.speedLimit/freeSpeed/
    currentSpeed with km/h values in practice, even though LinkState.scala's own docstring
    says "(m/s)" — that docstring appears stale relative to the generators actually
    exercised against the engine. Followed the generators, not the docstring; flagged here
    so this isn't silently re-assumed later.
    """
    actors: List[dict] = []
    for lid, link in links.items():
        speed_kmh = _mps_to_kmh(link["freespeed"])
        lanes = max(1, round(link["permlanes"]))
        lane_configs = [
            {"laneId": i, "type": "normal", "width": 3.5, "speedLimit": None}
            for i in range(lanes)
        ]
        actors.append(
            {
                "id": link_actor_id(lid),
                "typeActor": "hybrid.actor.Link",
                "data": {
                    "dataType": "model.hybrid.entity.state.LinkState",
                    "content": {
                        "startTick": 0,
                        "scheduleOnTimeManager": False,
                        "from": node_actor_id(link["from"]),
                        "to": node_actor_id(link["to"]),
                        "length": link["length"],
                        "lanes": lanes,
                        "speedLimit": speed_kmh,
                        "capacity": link["capacity"],
                        "freeSpeed": speed_kmh,
                        "currentSpeed": speed_kmh,
                        "congestionFactor": 1.0,
                        "registered": [],
                        "simulationMode": "MESO",
                        "microTimeStep": 0.1,
                        "microTicksPerGlobalTick": 10,
                        "vehiclesByLane": {},
                        "laneConfigurations": lane_configs,
                    },
                },
                "dependencies": {
                    "from_node": {"id": node_actor_id(link["from"]), "classType": "hybrid.actor.Node"},
                    "to_node": {"id": node_actor_id(link["to"]), "classType": "hybrid.actor.Node"},
                },
            }
        )
    return actors


def build_city_map(
    node_actors: List[dict], link_actors: List[dict]
) -> dict:
    """Build the city_map.json payload (nodes/edges graph consumed by CityMapUtil)."""
    map_nodes = [
        {
            "id": node["id"],
            "classType": "hybrid.actor.Node",
            "resourceId": node["id"],
            "latitude": node["data"]["content"]["latitude"],
            "longitude": node["data"]["content"]["longitude"],
        }
        for node in node_actors
    ]
    map_edges = [
        {
            "source_id": link["data"]["content"]["from"],
            "target_id": link["data"]["content"]["to"],
            "weight": link["data"]["content"]["length"],
            "label": {
                "id": link["id"],
                "resourceId": link["id"],
                "classType": "hybrid.actor.Link",
                "length": link["data"]["content"]["length"],
            },
        }
        for link in link_actors
    ]
    return {"nodes": map_nodes, "edges": map_edges, "directed": True}


# ---------------------------------------------------------------------------
# Traffic-signal actors (priority: robust, well-tested)
# ---------------------------------------------------------------------------


def _incoming_link_id(
    upstream_node: str, controlled_nodes: List[str], links: Dict[str, dict]
) -> Optional[str]:
    """Resolve a signal phase's `origin` (an approach/upstream node id in signals.xml) to the
    actual incoming link id: the link whose `from` is the upstream node and whose `to` is one
    of the signal's controlled intersection nodes. Returns None if no such link exists (the
    phase is then skipped rather than emitting a bogus origin)."""
    controlled = set(controlled_nodes)
    for lid, link in links.items():
        if link["from"] == upstream_node and link["to"] in controlled:
            return lid
    return None


def build_traffic_signal_actors(signals: List[dict], links: Dict[str, dict]) -> List[dict]:
    """Build hybrid.actor.TrafficSignal actors from parsed signals.xml entries.

    Mirrors the shape produced by tools/sao-paulo/generate_hybrid_input.py's
    TrafficSignalGenerator: Phase.origin is resolved to the incoming *link* actor id (not the
    upstream node id signals.xml itself uses), since TrafficSignalState.signalStates and
    NodeEventHandler key signal state per approach link.
    """
    actors: List[dict] = []
    for index, signal in enumerate(signals):
        phases: List[dict] = []
        signal_states: Dict[str, dict] = {}
        for phase in signal["phases"]:
            lid = _incoming_link_id(phase["origin"], signal["nodes"], links)
            if lid is None:
                # Best-effort fallback: no matching incoming link found (e.g. truncated mini
                # fixtures) — keep the node id so the phase isn't silently dropped, but this
                # will not match any real Link actor id.
                origin_id = node_actor_id(phase["origin"])
            else:
                origin_id = link_actor_id(lid)
            phases.append(
                {
                    "origin": origin_id,
                    "greenStart": int(phase["green_start"]),
                    "greenDuration": int(phase["green_duration"]),
                    "state": "Red",
                }
            )
            signal_states[origin_id] = {"state": "Red", "remainingTime": 0, "nextTick": 0}

        node_ids = [node_actor_id(n) for n in signal["nodes"]]
        actor_id = _IDS.id_for("signal", f"signal_{index}")
        actors.append(
            {
                "id": actor_id,
                "typeActor": "hybrid.actor.TrafficSignal",
                "data": {
                    "dataType": "model.hybrid.entity.state.TrafficSignalState",
                    "content": {
                        "startTick": 0,
                        "cycleDuration": int(signal["cycle_duration"]),
                        "offset": int(signal["offset"]),
                        "nodes": node_ids,
                        "phases": phases,
                        "signalStates": signal_states,
                    },
                },
                "dependencies": {
                    node_id: {"id": node_id, "classType": "hybrid.actor.Node"} for node_id in node_ids
                },
            }
        )
    return actors


def wire_node_approach_connections(
    node_actors: List[dict], signals: List[dict], links: Dict[str, dict]
) -> None:
    """Populate each signalized Node's `approachConnections` in place (mutates the actor dicts
    already appended to the output actor list, so no re-merge step is needed by callers).

    Keyed by the APPROACH link (the link an incoming vehicle is physically traveling ON, same
    incoming-link id `build_traffic_signal_actors` already resolves each phase's `origin` to) --
    the opposite direction of `connections` below. `NodeEventHandler.handleReceiveSignalChangeStatus`
    uses this map (not `connections`) to find which Link actor to notify of a phase change for
    MICRO's `signalAtExit` virtual-stopped-leader braking; see docs/KNOWN_GAPS.md, "NodeEventHandler
    Notifies the Wrong Link on Signal Phase Change" and "Production Scenario Generators Never
    Wire Node.connections/approachConnections". Fixes real signals having zero effect on any
    MICRO-mode vehicle in a converted InterSCSimulator scenario -- previously `connections` (and,
    before this function existed, `approachConnections`) were unconditionally empty.

    `connections` (outgoing-link keyed, needed for MESO-mode admission control via
    `handleRequestLinkAccess`/`CarSignalHandler`) is deliberately left untouched here: resolving
    it correctly requires per-(incoming,outgoing) turn-movement data that neither network.xml nor
    signals.xml provide, and a wrong guess risks silently gating a legal movement or leaving one
    ungated where it shouldn't be -- worse than the current status quo of no MESO gating at all.
    Flagged as a separate, still-open architectural gap in docs/KNOWN_GAPS.md, not fixed here.
    """
    by_node_id = {a["id"]: a for a in node_actors}
    for signal in signals:
        for phase in signal["phases"]:
            lid = _incoming_link_id(phase["origin"], signal["nodes"], links)
            if lid is None:
                continue
            approach_link_id = link_actor_id(lid)
            to_node = links[lid]["to"]
            node_actor = by_node_id.get(node_actor_id(to_node))
            if node_actor is None:
                continue
            node_actor["data"]["content"]["approachConnections"][approach_link_id] = {
                "id": approach_link_id,
                "resourceId": "",
                "classType": "",
                "actorRef": "",
                "actorType": "",
            }


# ---------------------------------------------------------------------------
# Person/Car actors (best-effort — see module docstring)
# ---------------------------------------------------------------------------

# trips.xml `start` unit is NOT documented anywhere found (checked InterSCSimulator's public
# docs/README/papers — no attribute-level spec). Cross-checking base_scenario/trips.xml
# against base_scenario/arrivals.xml (real simulator output) did not cleanly confirm minutes
# vs. seconds: e.g. trip "consolacao_regular0" has start=7 but its logged arrival (trip
# "consolacao_regular0_1") is at time=283s with trip_time=255s, i.e. an actual departure
# around t=28s — consistent with neither `7` (seconds) nor `7*60=420` (minutes) directly,
# most likely because InterSCSimulator jitters/distributes each `start` bucket internally
# rather than departing at exactly that instant. Treated here as **minutes since midnight**
# (the more common convention for this kind of OD demand matrix) — override with
# --start-unit seconds if a given scenario's provenance says otherwise.
START_UNIT_TO_SECONDS = {"minutes": 60, "seconds": 1}

# Matches every other actor generator in this codebase's "end of simulated day" convention
# (simulation_payload's endTick=86400, tickDuration=1.0 second — see
# generate_tiny_validation_scenario.py) and PersonState's own `ptWaitTimeoutTicks` default.
SIMULATION_DAY_END_TICK = 86400

_MODE_TO_ACTOR_TYPE = {
    "car": "Car",
    "bike": "Bicycle",
    "bicycle": "Bicycle",
    "motorcycle": "Motorcycle",
    "moto": "Motorcycle",
}


_ACTOR_TYPE_TO_DATA_TYPE = {
    "Car": "model.hybrid.entity.state.CarState",
    "Bicycle": "model.hybrid.entity.state.BicycleState",
    "Motorcycle": "model.hybrid.entity.state.MotorcycleState",
}

# Default vehicle "size" (meters) used only as physical-footprint metadata for the micro
# car-following model; not derived from any InterSCSimulator input (none is provided).
_ACTOR_TYPE_TO_SIZE = {"Car": 4.5, "Bicycle": 1.8, "Motorcycle": 2.2}


def _vehicle_state_content(actor_type: str, origin_node: str, destination_node: str, start_tick: int) -> dict:
    """Content payload for a private-vehicle actor (Car/Bicycle/Motorcycle), matching each
    state case class's actual constructor fields (see model.hybrid.entity.state.{Car,Bicycle,
    Motorcycle}State.scala — confirmed field-by-field, not assumed). `CarState` additionally
    carries `precomputedRoute`; `BicycleState`/`MotorcycleState` do not.
    """
    content = {
        "startTick": start_tick,
        "origin": origin_node,
        "destination": destination_node,
        "distance": 0.0,
        "actorType": actor_type,
        "size": _ACTOR_TYPE_TO_SIZE.get(actor_type, 4.5),
        "currentSimulationMode": "MESO",
        "microState": None,
    }
    if actor_type == "Car":
        content["precomputedRoute"] = None
    return content


def synthesize_bike_trips(trips: List[dict], fraction: float, seed: int = 42) -> List[dict]:
    """Convert a random `fraction` of `car`-mode trips to `bike` mode in place (returns a new
    list; input is not mutated).

    InterSCSimulator's own `base_scenario/trips.xml` ships 6439 trips, every one `mode="car"`
    (confirmed: `grep -o 'mode="[^"]*"' trips.xml | sort -u` yields only `car`) — there is no
    real bike-mode demand to convert from. This synthesizes plausible bike demand from the
    existing OD topology (reusing real origin/destination/start pairs, only relabeling the
    mode) rather than inventing new trip endpoints, since a car OD pair is at minimum a
    physically connected, demand-plausible pair of network nodes. `fraction=0.0` (the CLI
    default until explicitly raised) reproduces the previous car-only behavior exactly.
    """
    if fraction <= 0.0:
        return trips
    rng = random.Random(seed)
    car_indices = [i for i, t in enumerate(trips) if t["mode"] == "car"]
    n_convert = int(round(len(car_indices) * fraction))
    chosen = set(rng.sample(car_indices, n_convert)) if n_convert > 0 else set()
    result: List[dict] = []
    for i, trip in enumerate(trips):
        if i in chosen:
            trip = dict(trip)
            trip["mode"] = "bike"
        result.append(trip)
    return result


def synthesize_extra_od_trips(
    node_raw_ids: List[str],
    count: int,
    seed: int = 42,
    car_fraction: float = 0.7,
    start_unit: str = "minutes",
) -> List[dict]:
    """Generate `count` additional car/bike OD trips beyond whatever trips.xml provides, by
    sampling random distinct (origin, destination) pairs from the road network's own node ids.

    trips.xml stays the real/primary source of car demand (per the platform maintainer's
    explicit instruction) — this only pads it with synthetic demand for scenarios that want a
    denser/more diverse population than the real OD matrix alone provides (e.g. exercising
    more of the road network, or generating enough riders to make synthetic bus/subway lines
    non-empty). Returns trip dicts in the exact shape `parse_trips` produces, so they feed
    straight into `build_person_and_vehicle_actors` unchanged.

    Start ticks are spread uniformly across a simulated day in `start_unit`'s own units (e.g.
    0-1440 for "minutes") rather than reusing `--start-unit`'s conversion after the fact, so
    the generated `start` values are meaningful on their own if this function's output is ever
    inspected/serialized independently of the rest of the pipeline.
    """
    if count <= 0 or len(node_raw_ids) < 2:
        return []
    rng = random.Random(seed)
    day_end = SIMULATION_DAY_END_TICK // START_UNIT_TO_SECONDS[start_unit]
    trips: List[dict] = []
    for i in range(count):
        origin, destination = rng.sample(node_raw_ids, 2)
        mode = "car" if rng.random() < car_fraction else "bike"
        trips.append(
            {
                "name": f"synthetic_extra_{i}",
                "origin": origin,
                "destination": destination,
                "link_origin": None,
                "count": 1,
                "start": float(rng.randint(0, max(day_end - 1, 0))),
                "mode": mode,
                "digital_rails_capable": False,
            }
        )
    return trips


def build_person_and_vehicle_actors(
    trips: List[dict], node_raw_ids: set, start_unit: str = "minutes", declare_ownership: bool = True
) -> List[dict]:
    """Build Person + private-vehicle (Car/Bicycle/Motorcycle) actor pairs from trips.xml OD
    entries.

    Emits the CURRENT `PersonState` shape (`model/hybrid/entity/state/PersonState.scala`):
    `originalPlan: List[PlanElement]` only — `cursor`/`tripExecution` are deliberately omitted
    since `Person.internStateStrings` seeds `cursor` from `originalPlan` on first load (see
    that method's own doc, and `PersonStateJsonSpec`'s
    "deserialize a scenario-shaped payload" test, which is exactly this shape: `originalPlan`
    set, `cursor`/`tripExecution` left at their case-class defaults). The plan is a single
    contiguous trip: `Activity(origin) -> PrivateVehicleLeg(mode) -> Activity(destination)`,
    since trips.xml has no return-trip semantics (each row is one directed OD leg — see the
    `START_UNIT_TO_SECONDS` caveat above for the `start` unit itself).

    This replaces the previous `dailySchedule`/`currentActivityIndex` shape, which predates
    the PersonState redesign and was never verified to load against the current engine (see
    CHANGELOG-equivalent note in README.md "Known best-effort gaps").
    """
    person_actors: List[dict] = []
    seconds_per_unit = START_UNIT_TO_SECONDS[start_unit]

    for trip in trips:
        if trip["origin"] not in node_raw_ids or trip["destination"] not in node_raw_ids:
            continue
        start_tick = int(trip["start"] * seconds_per_unit)
        origin_node = node_actor_id(trip["origin"])
        destination_node = node_actor_id(trip["destination"])
        actor_type = _MODE_TO_ACTOR_TYPE.get(trip["mode"], "Car")
        concrete_mode = actor_type  # ConcreteMode enum case names match _MODE_TO_ACTOR_TYPE values

        person_id = _IDS.id_for("person", trip['name'])
        vehicle_id = _IDS.id_for("car", trip['name'])
        vehicle_identify = {"id": vehicle_id, "classType": f"hybrid.actor.{actor_type}"}

        original_plan = [
            {
                "kind": "Activity",
                "activityType": "origin",
                "nodeId": origin_node,
                "endTime": {"kind": "AtTick", "tick": start_tick},
            },
            {
                "kind": "PrivateVehicleLeg",
                "mode": concrete_mode,
                "vehicle": vehicle_identify,
                "driverAttributes": {
                    "aggressiveness": 0.5,
                    "maxSpeedFactor": 1.0,
                    "reactionTime": 1.0,
                    "minGapFactor": 1.0,
                },
            },
            {
                "kind": "Activity",
                "activityType": "destination",
                "nodeId": destination_node,
                # Absolute end-of-simulated-day tick — NOT `Duration(86400)`. Confirmed via
                # PersonPlanManager.onArrivedAtActivity (called for every Activity, including
                # the plan's last one): it always resolves a departure tick via
                # MinimumDwellLatenessPolicy.resolveDepartureTick(activity.endTime, arrivalTick)
                # and schedules a wake-up there before the *next* `step()` call can observe
                # `remaining.isEmpty` and finish the schedule. `Duration(86400)` would mean
                # "86400s after actual arrival" (a person arriving at tick 800 would linger
                # scheduled until tick 87200); `AtTick(86400)` anchors to the actual simulated
                # day boundary regardless of arrival tick, matching every other generator's
                # "end of day" convention in this codebase.
                "endTime": {"kind": "AtTick", "tick": SIMULATION_DAY_END_TICK},
            },
        ]

        person_actors.append(
            {
                "id": person_id,
                "typeActor": "hybrid.actor.Person",
                "data": {
                    "dataType": "model.hybrid.entity.state.PersonState",
                    "content": {
                        "startTick": start_tick,
                        "scheduleOnTimeManager": True,
                        "originalPlan": original_plan,
                        "ownedVehicles": {trip["mode"]: vehicle_identify},
                        "vehicleCurrentNode": {trip["mode"]: origin_node},
                        "totalDistanceTraveled": 0.0,
                        "completedTrips": 0,
                        "ptWaitTimeoutTicks": 86400,
                    },
                },
                "dependencies": {},
            }
        )
        person_actors.append(
            {
                "id": vehicle_id,
                "typeActor": f"hybrid.actor.{actor_type}",
                "data": {
                    "dataType": _ACTOR_TYPE_TO_DATA_TYPE[actor_type],
                    "content": _vehicle_state_content(actor_type, origin_node, destination_node, start_tick),
                },
                "dependencies": {
                    "from_node": {"id": origin_node, "classType": "hybrid.actor.Node"},
                    "to_node": {"id": destination_node, "classType": "hybrid.actor.Node"},
                    # partial-simulation filter: the vehicle is a part of its owner (on by default; --no-declare-ownership turns it off)
                    **({"owner": {"id": person_id, "classType": "hybrid.actor.Person", "relation": "part_of"}}
                       if declare_ownership else {}),
                },
            }
        )
    return person_actors


# ---------------------------------------------------------------------------
# Bus/metro (best-effort, MATSim transitSchedule-like; no example input available)
# ---------------------------------------------------------------------------


def parse_transit_schedule(path: Path) -> dict:
    """Best-effort parser for a MATSim-`transitSchedule.xml`-like file.

    No InterSCSimulator bus-line/bus-stop/metro example ships with this scenario family
    (confirmed: only network/trips/signals/config/arrivals/digital-rails files exist under
    input/base_scenario/). This parser follows the *documented* MATSim transitSchedule
    structure as a best-effort reference implementation:

      <transitSchedule>
        <transitStops>
          <stop id="..." linkRefId="..." x="..." y="..." name="..."/>
        </transitStops>
        <transitLine id="...">
          <transitRoute id="...">
            <routeProfile>
              <stop refId="..." departureOffset="..." arrivalOffset="..."/>
            </routeProfile>
          </transitRoute>
        </transitLine>
      </transitSchedule>

    Never exercised against a real InterSCSimulator/MATSim export — only unit-tested against
    a hand-built fixture (tests/test_convert_interscsimulator.py). Treat any output from this
    function as unverified until confirmed against a real transitSchedule.xml.

    Returns {"stops": {stop_id: {"link_ref_id", "x", "y", "name"}},
             "lines": {line_id: [{"route_id", "stops": [{"ref_id", "departure_offset", "arrival_offset"}]}]}}
    """
    tree = ET.parse(path)
    root = tree.getroot()

    stops: Dict[str, dict] = {}
    for stop_el in root.findall("./transitStops/stop"):
        sid = stop_el.get("id")
        if sid is None:
            continue
        stops[sid] = {
            "link_ref_id": stop_el.get("linkRefId"),
            "x": float(stop_el.get("x", "0")),
            "y": float(stop_el.get("y", "0")),
            "name": stop_el.get("name", sid),
        }

    lines: Dict[str, List[dict]] = defaultdict(list)
    for line_el in root.findall("./transitLine"):
        line_id = line_el.get("id")
        if line_id is None:
            continue
        for route_el in line_el.findall("./transitRoute"):
            route_stops = [
                {
                    "ref_id": s.get("refId"),
                    "departure_offset": s.get("departureOffset"),
                    "arrival_offset": s.get("arrivalOffset"),
                }
                for s in route_el.findall("./routeProfile/stop")
            ]
            lines[line_id].append({"route_id": route_el.get("id"), "stops": route_stops})

    return {"stops": stops, "lines": dict(lines)}


def build_transit_actors(
    schedule: dict, projection: str, anchor_lat: float, anchor_lon: float
) -> List[dict]:
    """Best-effort BusStop/BusStation actor generation from a parsed transit schedule. See
    `parse_transit_schedule` docstring for the unverified/best-effort caveat."""
    actors: List[dict] = []
    stop_actor_ids: Dict[str, str] = {}

    for stop_id, stop in schedule.get("stops", {}).items():
        lon, lat = project_xy(stop["x"], stop["y"], projection, anchor_lat, anchor_lon)
        actor_id = _IDS.id_for("busstop", stop_id)
        stop_actor_ids[stop_id] = actor_id
        # No road-network node id is available from a transit schedule alone; nodeId is left
        # as the stop's own actor id placeholder — callers should snap this to the nearest
        # city_map node before use. Documented best-effort limitation.
        actors.append(
            {
                "id": actor_id,
                "typeActor": "hybrid.actor.BusStop",
                "data": {
                    "dataType": "model.hybrid.entity.state.BusStopState",
                    "content": {
                        "nodeId": None,
                        "label": stop["name"],
                        "people": {},
                        "scheduleOnTimeManager": False,
                        "latitude": lat,
                        "longitude": lon,
                    },
                },
                "dependencies": {},
            }
        )

    for line_id, routes in schedule.get("lines", {}).items():
        for route in routes:
            route_stop_ids = [stop_actor_ids.get(s["ref_id"]) for s in route["stops"]]
            actors.append(
                {
                    "id": _IDS.id_for("busstation", f"{line_id}_{route['route_id']}"),
                    "typeActor": "hybrid.actor.BusStation",
                    "data": {
                        "dataType": "model.hybrid.entity.state.BusStationState",
                        "content": {
                            "startTick": 0,
                            "name": f"{line_id} / {route['route_id']}",
                            "busStops": {sid: None for sid in route_stop_ids if sid},
                            "interval": 3600,
                            "buses": [],
                            "goingRoute": {},
                            "goingBestCost": 1_000_000_000_000.0,
                            "returningRoute": {},
                            "returningBestCost": 1_000_000_000_000.0,
                            "status": "Start",
                        },
                    },
                    "dependencies": {},
                }
            )
    return actors


# ---------------------------------------------------------------------------
# Synthetic bus/subway network generation (topology-driven — no InterSCSimulator input
# provides bus lines or metro lines for this scenario family; see module docstring / README
# "Synthetic transit" section for the heuristics used).
# ---------------------------------------------------------------------------

# (legacy `htcaid:` string id prefixes removed — ids are now opaque `long`s via `_IDS`)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters. Used only to give synthetic Link/RailLink `length`
    a physically plausible value where no real route geometry exists."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_node_degrees(links: Dict[str, dict]) -> Dict[str, int]:
    """Undirected degree (in + out link count) per raw node id — used as the connectivity
    proxy for picking plausible transit-hub locations (busier intersections make more
    plausible bus stops / subway stations than degree-1 dead ends)."""
    degree: Dict[str, int] = defaultdict(int)
    for link in links.values():
        if link["from"] is not None:
            degree[link["from"]] += 1
        if link["to"] is not None:
            degree[link["to"]] += 1
    return degree


def select_hub_nodes(
    degree: Dict[str, int],
    coords: Dict[str, Tuple[float, float]],
    count: int,
    min_spacing_m: float,
    exclude: Set[str] = frozenset(),
) -> List[str]:
    """Pick up to `count` raw node ids as transit hubs: highest-degree first, subject to a
    minimum geographic spacing so hubs don't cluster on the same intersection. Falls back to
    filling remaining slots by degree alone (ignoring spacing) if too few candidates satisfy
    the spacing constraint — small/synthetic fixtures (e.g. mini-map.xml, 6 nodes) would
    otherwise yield zero hubs.
    """
    if count <= 0:
        return []
    ranked = sorted(
        (n for n in degree if n not in exclude and n in coords),
        key=lambda n: -degree[n],
    )
    chosen: List[str] = []
    for n in ranked:
        if len(chosen) >= count:
            break
        lat, lon = coords[n]
        if all(_haversine_m(lat, lon, *coords[c]) >= min_spacing_m for c in chosen):
            chosen.append(n)
    if len(chosen) < count:
        for n in ranked:
            if len(chosen) >= count:
                break
            if n not in chosen:
                chosen.append(n)
    return chosen


def partition_into_lines(
    hub_nodes: List[str], coords: Dict[str, Tuple[float, float]], num_lines: int, stops_per_line: int
) -> List[List[str]]:
    """Split `hub_nodes` into up to `num_lines` geographically-ordered chains of
    `stops_per_line` stops each (sorted by longitude so each line reads as a roughly
    contiguous corridor rather than a random jumble of stops). Lines with fewer than 2 stops
    are dropped (a line needs at least an origin and a destination)."""
    ordered = sorted(hub_nodes, key=lambda n: coords[n][1])
    lines: List[List[str]] = []
    idx = 0
    for _ in range(num_lines):
        chunk = ordered[idx : idx + stops_per_line]
        idx += stops_per_line
        if len(chunk) >= 2:
            lines.append(chunk)
    return lines


def build_synthetic_bus_actors(
    lines: List[List[str]],
    coords: Dict[str, Tuple[float, float]],
    headway_s: int = 600,
    capacity: int = 80,
    number_of_ports: int = 2,
    buses_per_line: int = 4,
    vehicle_size_m: float = 12.0,
) -> Tuple[List[dict], Dict[str, int]]:
    """Build BusStop + BusStation + Bus actors for each synthetic line (list of raw node ids
    in stop order). Field shapes confirmed against
    `model.hybrid.entity.state.{BusStopState,BusStationState,BusState}.scala` and mirrored
    from the real, GTFS-driven reference implementation in
    `tools/sao-paulo/generate_hybrid_input.py` (`_emit_route`/`_load_and_snap_stops`) — the
    only difference is stop/line selection (topology heuristic here vs. real GTFS there).
    """
    actors: List[dict] = []
    stop_actor_created: Set[str] = set()
    stats = {"bus_lines": 0, "bus_stops": 0, "bus_stations": 0, "buses": 0}

    for line_idx, line_nodes in enumerate(lines):
        line_label = f"Bus Line {line_idx + 1}"
        for n in line_nodes:
            stop_id = _IDS.id_for("busstop", n)
            if stop_id in stop_actor_created:
                continue
            stop_actor_created.add(stop_id)
            actors.append(
                {
                    "id": stop_id,
                    "typeActor": "hybrid.actor.BusStop",
                    "data": {
                        "dataType": "model.hybrid.entity.state.BusStopState",
                        "content": {
                            "nodeId": node_actor_id(n),
                            "label": f"Bus Stop {n}",
                            "people": {},
                        },
                    },
                    "dependencies": {"node": {"id": node_actor_id(n), "classType": "hybrid.actor.Node"}},
                }
            )
            stats["bus_stops"] += 1

        bus_stops_dict = {_IDS.id_for("busstop", n): node_actor_id(n) for n in line_nodes}
        origin_node = node_actor_id(line_nodes[0])
        destination_node = node_actor_id(line_nodes[-1])

        bus_entries: List[dict] = []
        for i in range(max(1, buses_per_line)):
            bus_id = _IDS.id_for("bus", f"line{line_idx}_bus{i}")
            start_tick = i * headway_s
            actors.append(
                {
                    "id": bus_id,
                    "typeActor": "hybrid.actor.Bus",
                    "data": {
                        "dataType": "model.hybrid.entity.state.BusState",
                        "content": {
                            "startTick": start_tick,
                            "label": line_label,
                            "capacity": capacity,
                            "distance": 0.0,
                            "busStops": bus_stops_dict,
                            "numberOfPorts": number_of_ports,
                            "people": {},
                            "origin": origin_node,
                            "destination": destination_node,
                            "actorType": "Bus",
                            "size": vehicle_size_m,
                            "currentSimulationMode": "MESO",
                            "microState": None,
                        },
                    },
                    "dependencies": {
                        "from_node": {"id": origin_node, "classType": "hybrid.actor.Node"},
                        "to_node": {"id": destination_node, "classType": "hybrid.actor.Node"},
                    },
                }
            )
            bus_entries.append(
                {
                    "actorId": bus_id,
                    "label": line_label,
                    "capacity": capacity,
                    "numberOfPorts": number_of_ports,
                    "size": vehicle_size_m,
                }
            )
            stats["buses"] += 1

        deps = {
            node_actor_id(n): {"id": node_actor_id(n), "classType": "hybrid.actor.Node"} for n in line_nodes
        }
        actors.append(
            {
                "id": _IDS.id_for("busstation", f"line{line_idx}"),
                "typeActor": "hybrid.actor.BusStation",
                "data": {
                    "dataType": "model.hybrid.entity.state.BusStationState",
                    "content": {
                        "startTick": 0,
                        "name": line_label,
                        "origin": origin_node,
                        "destination": destination_node,
                        "busStops": bus_stops_dict,
                        "interval": headway_s,
                        "buses": bus_entries,
                        # BusStationState.goingRoute/returningRoute are
                        # `Option[Map[SubRoutePair, Queue[...]]] = Some(Map.empty)` by default.
                        # `{}` (not `null`) is the correct JSON for that default — confirmed via
                        # a real Scala roundtrip (JsonUtil.convertValue) during this change: a
                        # minimal BusStationState JSON omitting these two keys entirely
                        # deserializes with `goingRoute == Some(Map.empty)`; explicit JSON
                        # `null` would instead deserialize to `None`, a different (though
                        # functionally equivalent here, per BusStation.scala's `.exists`-based
                        # reads) unverified state. `{}` matches the tested default exactly.
                        "goingRoute": {},
                        "goingBestCost": 1e12,
                        "returningRoute": {},
                        "returningBestCost": 1e12,
                        "status": "Start",
                    },
                },
                "dependencies": deps,
            }
        )
        stats["bus_stations"] += 1
        stats["bus_lines"] += 1

    return actors, stats


def build_synthetic_subway_actors(
    lines: List[List[str]],
    coords: Dict[str, Tuple[float, float]],
    headway_s: int = 300,
    capacity: int = 300,
    number_of_ports: int = 4,
    speed_ms: float = 22.22,
    stop_time_s: int = 30,
    trains_per_line: int = 3,
    rail_lanes: int = 2,
) -> Tuple[List[dict], Dict[str, int]]:
    """Build SubwayStation + RailLink actors for each synthetic subway line (list of raw node
    ids in station order). Field shapes confirmed against
    `model.hybrid.entity.state.{SubwayStationState,RailLinkState}.scala`, mirroring the real
    GTFS-driven reference in `tools/sao-paulo/generate_hybrid_input.py`
    (`_create_stations`/`_create_rail_links`/`_configure_garages`/`_write_all`).

    Deliberate simplification vs. that reference: forward direction only (no separate return
    route) — a reasonable scope cut for a synthetic scenario (documented in README), not an
    oversight. Only the line's first (garage) station carries `lines`/`subways`/`linesRoute`
    entries — exactly mirroring the reference, where only garage stations dispatch trains;
    every other station on the line stays a plain stop with those maps empty.

    Unlike that reference implementation (which drops `startTick` from its `RailLinkState`
    JSON and adds two fields — `laneConfigurations`/`vehiclesByLane` — that don't exist on
    `RailLinkState` at all), this emits exactly `RailLinkState`'s real constructor fields:
    `startTick` is required (no default in the case class) and must be present, while the two
    LinkState-only fields are omitted as not applicable to rail.
    """
    actors: List[dict] = []
    station_created: Set[str] = set()
    stats = {"subway_lines": 0, "subway_stations": 0, "rail_links": 0, "subway_trains": 0}

    station_info: Dict[str, dict] = {}

    for line_idx, line_nodes in enumerate(lines):
        line_key = f"line{line_idx}"
        for pos, n in enumerate(line_nodes):
            station_id = _IDS.id_for("subwaystation", n)
            if station_id not in station_created:
                station_created.add(station_id)
                station_info[n] = {
                    "actor_id": station_id,
                    "terminal": False,
                    "garage": False,
                    "lines": {},
                    "subways": {},
                    "linesRoute": {},
                }
            if pos == 0:
                station_info[n]["garage"] = True
                station_info[n]["terminal"] = True
            elif pos == len(line_nodes) - 1:
                station_info[n]["terminal"] = True

        # Rail links for each consecutive hop.
        rail_links: List[dict] = []
        for i in range(len(line_nodes) - 1):
            from_n, to_n = line_nodes[i], line_nodes[i + 1]
            lat1, lon1 = coords[from_n]
            lat2, lon2 = coords[to_n]
            link_id = _IDS.id_for("rail_link", f"{line_key}_segment_{i}")
            length_m = max(_haversine_m(lat1, lon1, lat2, lon2), 50.0)
            speed_kmh = _mps_to_kmh(speed_ms)
            rail_links.append(
                {
                    "link_id": link_id,
                    "from_n": from_n,
                    "to_n": to_n,
                    "length_m": length_m,
                    "speed_kmh": speed_kmh,
                }
            )
            actors.append(
                {
                    "id": link_id,
                    "typeActor": "hybrid.actor.RailLink",
                    "data": {
                        "dataType": "model.hybrid.entity.state.RailLinkState",
                        "content": {
                            "startTick": 0,
                            "from": node_actor_id(from_n),
                            "to": node_actor_id(to_n),
                            "length": length_m,
                            "lanes": rail_lanes,
                            "speedLimit": speed_kmh,
                            "capacity": 20.0,
                            "freeSpeed": speed_kmh,
                            "railType": "SUBWAY",
                            "subwayLine": line_key,
                            "fromStation": station_info[from_n]["actor_id"],
                            "toStation": station_info[to_n]["actor_id"],
                            "gradient": 0.0,
                            "curvature": 0.0,
                            "simulationMode": "MESO",
                            "registered": [],
                            "scheduleOnTimeManager": False,
                        },
                    },
                    "dependencies": {
                        "from_node": {"id": node_actor_id(from_n), "classType": "hybrid.actor.Node"},
                        "to_node": {"id": node_actor_id(to_n), "classType": "hybrid.actor.Node"},
                        "from_station": {"id": station_info[from_n]["actor_id"], "classType": "hybrid.actor.SubwayStation"},
                        "to_station": {"id": station_info[to_n]["actor_id"], "classType": "hybrid.actor.SubwayStation"},
                    },
                }
            )
            stats["rail_links"] += 1

        if not rail_links:
            continue

        garage_n = line_nodes[0]
        dest_n = line_nodes[-1]
        garage = station_info[garage_n]
        garage["lines"][line_key] = {"interval": headway_s, "nextTick": 0}

        trains = []
        for t in range(max(1, trains_per_line)):
            train_id = _IDS.id_for("subway", f"{line_key}_train_{t}")
            trains.append(
                {
                    "line": line_key,
                    "actorId": train_id,
                    "capacity": capacity,
                    "numberOfPorts": number_of_ports,
                    "velocity": speed_ms,
                    "stopTime": stop_time_s,
                }
            )
            # Standalone hybrid.actor.Subway vehicle actor (SubwayState) — distinct from the
            # `SubwayInformation` descriptor above, which only registers the train's existence
            # in the garage station's dispatch table. Mirrors the Bus/BusInformation split in
            # build_synthetic_bus_actors: a real, independently-scheduled vehicle actor must
            # exist for every entry referenced there, or the line has infrastructure (stations,
            # rail links) but nothing actually running on it.
            actors.append(
                {
                    "id": train_id,
                    "typeActor": "hybrid.actor.Subway",
                    "data": {
                        "dataType": "model.hybrid.entity.state.SubwayState",
                        "content": {
                            "startTick": t * headway_s,
                            "capacity": capacity,
                            "numberOfPorts": number_of_ports,
                            "velocity": speed_ms,
                            "stopTime": stop_time_s,
                            "origin": node_actor_id(garage_n),
                            "destination": node_actor_id(dest_n),
                            "line": line_key,
                            "actorType": "Subway",
                        },
                    },
                    "dependencies": {
                        "from_node": {"id": node_actor_id(garage_n), "classType": "hybrid.actor.Node"},
                        "to_node": {"id": node_actor_id(dest_n), "classType": "hybrid.actor.Node"},
                    },
                }
            )
        garage["subways"][line_key] = trains
        stats["subway_trains"] += len(trains)

        route_entries = []
        for rl in rail_links:
            to_n = rl["to_n"]
            route_entries.append(
                {
                    "stationNode": {
                        "stationId": station_info[to_n]["actor_id"],
                        "nodeId": node_actor_id(to_n),
                    },
                    "railLinkId": rl["link_id"],
                }
            )
        garage["linesRoute"][line_key] = route_entries
        stats["subway_lines"] += 1

    for n, info in station_info.items():
        actors.append(
            {
                "id": info["actor_id"],
                "typeActor": "hybrid.actor.SubwayStation",
                "data": {
                    "dataType": "model.hybrid.entity.state.SubwayStationState",
                    "content": {
                        "startTick": 0,
                        "name": f"Subway Station {n}",
                        "nodeId": node_actor_id(n),
                        "terminal": info["terminal"],
                        "garage": info["garage"],
                        "lines": info["lines"],
                        "subways": info["subways"],
                        "linesRoute": info["linesRoute"],
                        "people": {},
                        "status": "Start",
                    },
                },
                "dependencies": {"node": {"id": node_actor_id(n), "classType": "hybrid.actor.Node"}},
            }
        )
        stats["subway_stations"] += 1

    return actors, stats


def synthesize_transit_network(
    nodes: Dict[str, dict],
    links: Dict[str, dict],
    coords: Dict[str, Tuple[float, float]],
    num_bus_lines: int,
    bus_stops_per_line: int,
    bus_headway_s: int,
    bus_capacity: int,
    bus_ports: int,
    buses_per_line: int,
    num_subway_lines: int,
    subway_stations_per_line: int,
    subway_headway_s: int,
    subway_capacity: int,
    subway_ports: int,
    subway_speed_ms: float,
    subway_stop_time_s: int,
    subway_trains_per_line: int,
    hub_min_spacing_m: float,
) -> Tuple[List[dict], Dict[str, int]]:
    """Top-level entry point: pick subway-station and bus-stop hub nodes from network
    topology (highest connectivity, spread apart — see `select_hub_nodes`), partition each
    set into lines (`partition_into_lines`), and build the corresponding actors. Subway hubs
    are chosen first (fewer, most-connected, most-spread-out nodes) so bus hubs never collide
    with them, mirroring how a real subway network gets the higher-connectivity corridors and
    buses fill in the rest.
    """
    degree = compute_node_degrees(links)
    stats: Dict[str, int] = {}
    actors: List[dict] = []

    subway_hub_count = num_subway_lines * subway_stations_per_line
    subway_hubs = select_hub_nodes(
        degree, coords, subway_hub_count, hub_min_spacing_m * 2.0
    )
    subway_lines = partition_into_lines(subway_hubs, coords, num_subway_lines, subway_stations_per_line)
    if subway_lines:
        subway_actors, subway_stats = build_synthetic_subway_actors(
            subway_lines,
            coords,
            headway_s=subway_headway_s,
            capacity=subway_capacity,
            number_of_ports=subway_ports,
            speed_ms=subway_speed_ms,
            stop_time_s=subway_stop_time_s,
            trains_per_line=subway_trains_per_line,
        )
        actors += subway_actors
        stats.update(subway_stats)

    bus_hub_count = num_bus_lines * bus_stops_per_line
    bus_hubs = select_hub_nodes(
        degree, coords, bus_hub_count, hub_min_spacing_m, exclude=set(subway_hubs)
    )
    bus_lines = partition_into_lines(bus_hubs, coords, num_bus_lines, bus_stops_per_line)
    if bus_lines:
        bus_actors, bus_stats = build_synthetic_bus_actors(
            bus_lines,
            coords,
            headway_s=bus_headway_s,
            capacity=bus_capacity,
            number_of_ports=bus_ports,
            buses_per_line=buses_per_line,
        )
        actors += bus_actors
        stats.update(bus_stats)

    return actors, stats


# ---------------------------------------------------------------------------
# Output writing (JSON + optional SQLite reuse of scenario-db-converter)
# ---------------------------------------------------------------------------


def _load_db_converter():
    """Import tools/scenario-db-converter/convert.py by relative path.

    Design choice: import (not copy/duplicate) the existing json<->sqlite schema logic so the
    two schemas (`actor-simulation`, `city-map`) stay defined in exactly one place. This
    script still runs standalone with no sbt/Scala dependency — `convert.py` itself has zero
    Scala/sbt dependency, only `ijson` (stdlib json/sqlite3 otherwise) — but it does assume
    the sibling directory `../scenario-db-converter/convert.py` exists relative to this file,
    i.e. this script must stay inside the htc-platform monorepo layout (or that file copied
    alongside it) to use --format sqlite. --format json never needs this import.
    """
    db_converter_dir = Path(__file__).resolve().parent.parent / "scenario-db-converter"
    convert_py = db_converter_dir / "convert.py"
    if not convert_py.exists():
        raise FileNotFoundError(
            f"Could not find {convert_py} — sqlite output requires "
            "tools/scenario-db-converter/convert.py to exist alongside this script's "
            "tools/ directory. Use --format json if that module isn't available."
        )
    if str(db_converter_dir) not in sys.path:
        sys.path.insert(0, str(db_converter_dir))
    import convert as db_converter  # type: ignore

    return db_converter


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _load_scenario_output_writer():
    """Import tools/scenario-db-converter/scenario_output_writer.py by relative path — the
    shared sharding + simulation.json/scenario_metadata.json/GENERATION_REPORT.md writer that
    sao-paulo's and toulouse's generate_hybrid_input.py implement independently of each other.
    Reusing it here (instead of this script's former single flat-file `write_outputs`) is what
    makes this converter's output package match theirs instead of drifting into a third shape.
    """
    writer_dir = Path(__file__).resolve().parent.parent / "scenario-db-converter"
    module_path = writer_dir / "scenario_output_writer.py"
    if not module_path.exists():
        raise FileNotFoundError(
            f"Could not find {module_path} — tools/scenario-db-converter/"
            "scenario_output_writer.py must exist alongside this script's tools/ directory."
        )
    if str(writer_dir) not in sys.path:
        sys.path.insert(0, str(writer_dir))
    import scenario_output_writer as sow  # type: ignore

    return sow


def write_outputs(
    output_dir: Path,
    city_map: dict,
    actors: List[dict],
    fmt: str,
    scenario_name: Optional[str] = None,
    docker_base: str = "/app/hyperbolic-time-chamber/simulations/input",
    network_path: Optional[Path] = None,
    trips_path: Optional[Path] = None,
    random_seed: int = 42,
    transit_stats: Optional[Dict[str, int]] = None,
) -> None:
    """Write the standard HTC scenario output package:

        <output_dir>/
          GENERATION_REPORT.md
          scenario_metadata.json
          simulation.json
          data/
            city_map.json
            <group>_<n>.json   (nodes, links, persons, cars, ...)

    matching the shape produced by tools/sao-paulo/ and tools/toulouse/'s
    generate_hybrid_input.py, via the shared tools/scenario-db-converter/
    scenario_output_writer.py writer.
    """
    sow = _load_scenario_output_writer()
    scenario_id = scenario_name or output_dir.name
    data_dir = output_dir / "data"

    group_counts = sow.shard_and_write_actors(actors, data_dir)
    sow.write_city_map_json(city_map, data_dir)

    sim = sow.build_simulation_json(
        scenario_id=scenario_id,
        name=f"InterSCSimulator - {scenario_id}",
        description=f"Generated from InterSCSimulator scenario ({scenario_id})",
        group_counts=group_counts,
        docker_base=docker_base,
        output_format=fmt,
        random_seed=random_seed,
    )
    sow.write_simulation_json(output_dir, sim)

    statistics = {group: info["count"] for group, info in group_counts.items()}
    if transit_stats:
        statistics.update(transit_stats)
    metadata = sow.build_scenario_metadata(
        name=scenario_id,
        description=f"Generated from InterSCSimulator scenario ({scenario_id})",
        source={
            "network": str(network_path) if network_path else None,
            "trips": str(trips_path) if trips_path else None,
            "format": "InterSCSimulator (MATSim-style XML)",
        },
        target={"directory": str(output_dir), "model": "Hybrid (MESO)"},
        statistics=statistics,
        configuration={"randomSeed": random_seed, "outputFormat": fmt},
    )
    sow.write_scenario_metadata(output_dir, metadata)

    sow.write_generation_report(
        output_dir,
        title="InterSCSimulator -> HTC",
        source_dir=str(network_path.parent) if network_path else "?",
        source_format="InterSCSimulator (MATSim-style XML)",
        scenario_name=scenario_id,
        group_counts=group_counts,
    )

    if fmt == "sqlite":
        db_converter = _load_db_converter()
        city_map_json = data_dir / "city_map.json"
        db_converter.city_map_json_to_db(str(city_map_json), str(data_dir / "city_map.db"))
        city_map_json.unlink()
        for group in group_counts:
            for i in range(group_counts[group]["files"]):
                json_fp = data_dir / f"{group}_{i}.json"
                db_converter.actor_simulation_json_to_db(
                    str(json_fp), str(data_dir / f"{group}_{i}.db")
                )
                json_fp.unlink()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", type=Path, required=True, help="network.xml path")
    parser.add_argument("--declare-ownership", action=argparse.BooleanOptionalAction, default=True,
                        help="Write owner/part_of on each private vehicle (partial-simulation filter) [default: on]")
    parser.add_argument("--trips", type=Path, default=None, help="trips.xml path (optional)")
    parser.add_argument("--signals", type=Path, default=None, help="traffic-signals.xml path (optional)")
    parser.add_argument(
        "--bus-lines", type=Path, default=None,
        help="best-effort MATSim transitSchedule.xml-like file for bus lines (optional)",
    )
    parser.add_argument(
        "--metro-lines", type=Path, default=None,
        help="best-effort MATSim transitSchedule.xml-like file for metro/subway lines (optional)",
    )
    parser.add_argument(
        "--projection", choices=["auto", "matsim-sp-utm23s", "none"], default="auto",
        help=(
            "How to reproject network.xml (x, y) to lon/lat. 'auto' (default) uses "
            "matsim-sp-utm23s when |x| or |y| exceeds --large-coord-threshold, else treats "
            "coordinates as a local planar grid around --anchor-lat/--anchor-lon."
        ),
    )
    parser.add_argument("--anchor-lat", type=float, default=-23.55, help="anchor latitude for --projection none")
    parser.add_argument("--anchor-lon", type=float, default=-46.63, help="anchor longitude for --projection none")
    parser.add_argument(
        "--large-coord-threshold", type=float, default=LARGE_COORD_THRESHOLD_M,
        help="meters; used by --projection auto to distinguish projected vs. local nets",
    )
    parser.add_argument(
        "--start-unit", choices=["minutes", "seconds"], default="minutes",
        help="unit of trips.xml's `start` attribute (see module docstring caveat)",
    )
    parser.add_argument(
        "--synth-bike-fraction", type=float, default=0.0,
        help=(
            "fraction (0.0-1.0) of car trips to randomly relabel as bike trips, since "
            "base_scenario/trips.xml has no real bike-mode demand (all 6439 trips are "
            "mode=car). 0.0 (default) reproduces the previous car-only behavior exactly."
        ),
    )
    parser.add_argument("--synth-seed", type=int, default=42, help="RNG seed for bike-trip/extra-OD-trip synthesis")
    parser.add_argument(
        "--synth-extra-od-trips", type=int, default=0,
        help=(
            "number of additional random car/bike OD trips to generate beyond trips.xml "
            "(0 default — trips.xml alone remains the primary/real car demand source)"
        ),
    )
    parser.add_argument(
        "--synth-extra-od-car-fraction", type=float, default=0.7,
        help="fraction of --synth-extra-od-trips assigned mode=car (rest are mode=bike)",
    )
    parser.add_argument(
        "--no-synth-transit", action="store_true",
        help="disable synthetic bus/subway network generation (on by default whenever --network is given)",
    )
    parser.add_argument("--synth-bus-lines", type=int, default=4, help="number of synthetic bus lines (0 disables)")
    parser.add_argument("--synth-bus-stops-per-line", type=int, default=6, help="stops per synthetic bus line")
    parser.add_argument("--synth-bus-headway", type=int, default=600, help="seconds between synthetic bus dispatches")
    parser.add_argument("--synth-bus-capacity", type=int, default=80, help="passenger capacity per synthetic bus")
    parser.add_argument("--synth-bus-ports", type=int, default=2, help="boarding doors per synthetic bus")
    parser.add_argument(
        "--synth-buses-per-line", type=int, default=4, help="number of Bus vehicle actors dispatched per line"
    )
    parser.add_argument(
        "--synth-subway-lines", type=int, default=1, help="number of synthetic subway lines (0 disables)"
    )
    parser.add_argument(
        "--synth-subway-stations-per-line", type=int, default=6, help="stations per synthetic subway line"
    )
    parser.add_argument(
        "--synth-subway-headway", type=int, default=300, help="seconds between synthetic train dispatches"
    )
    parser.add_argument("--synth-subway-capacity", type=int, default=300, help="passenger capacity per synthetic train")
    parser.add_argument("--synth-subway-ports", type=int, default=4, help="boarding doors per synthetic train")
    parser.add_argument(
        "--synth-subway-speed-ms", type=float, default=22.22, help="synthetic train cruise speed, m/s (~80 km/h default)"
    )
    parser.add_argument("--synth-subway-stop-time", type=int, default=30, help="seconds a synthetic train dwells per station")
    parser.add_argument(
        "--synth-subway-trains-per-line", type=int, default=3, help="trains dispatched per synthetic subway line"
    )
    parser.add_argument(
        "--synth-hub-min-spacing-m", type=float, default=300.0,
        help="minimum geographic spacing (meters) between synthetic bus-stop hub nodes (subway hubs use 2x this)",
    )
    parser.add_argument("--format", choices=["json", "sqlite"], default="json")
    parser.add_argument("--output", type=Path, required=True, help="output directory")
    parser.add_argument(
        "--scenario-name", default=None,
        help="scenario id/name used in simulation.json and data-source paths "
             "(default: --output's directory basename)",
    )
    parser.add_argument(
        "--docker-base", default="/app/hyperbolic-time-chamber/simulations/input",
        help="base path used inside Docker for simulation.json dataSource references "
             "(matches sao-paulo/toulouse generate_hybrid_input.py's --docker-base default)",
    )
    parser.add_argument("--random-seed", type=int, default=42, help="recorded in simulation.json/scenario_metadata.json")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    global LARGE_COORD_THRESHOLD_M
    LARGE_COORD_THRESHOLD_M = args.large_coord_threshold

    nodes, links = parse_network(args.network)
    node_actors, coords = build_node_actors(
        nodes, links, args.projection, args.anchor_lat, args.anchor_lon
    )
    link_actors = build_link_actors(links)
    city_map = build_city_map(node_actors, link_actors)

    actors: List[dict] = list(node_actors) + list(link_actors)

    if args.signals is not None:
        signals = parse_signals(args.signals)
        actors += build_traffic_signal_actors(signals, links)
        wire_node_approach_connections(node_actors, signals, links)

    trips: List[dict] = []
    if args.trips is not None:
        trips = parse_trips(args.trips)
        trips = synthesize_bike_trips(trips, args.synth_bike_fraction, args.synth_seed)
    if args.synth_extra_od_trips > 0:
        trips = trips + synthesize_extra_od_trips(
            list(nodes.keys()),
            args.synth_extra_od_trips,
            seed=args.synth_seed,
            car_fraction=args.synth_extra_od_car_fraction,
            start_unit=args.start_unit,
        )
    if trips:
        actors += build_person_and_vehicle_actors(trips, set(nodes.keys()), args.start_unit, args.declare_ownership)

    if args.bus_lines is not None:
        schedule = parse_transit_schedule(args.bus_lines)
        actors += build_transit_actors(schedule, args.projection, args.anchor_lat, args.anchor_lon)

    if args.metro_lines is not None:
        schedule = parse_transit_schedule(args.metro_lines)
        actors += build_transit_actors(schedule, args.projection, args.anchor_lat, args.anchor_lon)

    transit_stats: Dict[str, int] = {}
    if not args.no_synth_transit:
        transit_actors, transit_stats = synthesize_transit_network(
            nodes,
            links,
            coords,
            num_bus_lines=args.synth_bus_lines,
            bus_stops_per_line=args.synth_bus_stops_per_line,
            bus_headway_s=args.synth_bus_headway,
            bus_capacity=args.synth_bus_capacity,
            bus_ports=args.synth_bus_ports,
            buses_per_line=args.synth_buses_per_line,
            num_subway_lines=args.synth_subway_lines,
            subway_stations_per_line=args.synth_subway_stations_per_line,
            subway_headway_s=args.synth_subway_headway,
            subway_capacity=args.synth_subway_capacity,
            subway_ports=args.synth_subway_ports,
            subway_speed_ms=args.synth_subway_speed_ms,
            subway_stop_time_s=args.synth_subway_stop_time,
            subway_trains_per_line=args.synth_subway_trains_per_line,
            hub_min_spacing_m=args.synth_hub_min_spacing_m,
        )
        actors += transit_actors

    write_outputs(
        args.output, city_map, actors, args.format,
        scenario_name=args.scenario_name,
        docker_base=args.docker_base,
        network_path=args.network,
        trips_path=args.trips,
        random_seed=args.random_seed,
        transit_stats=transit_stats,
    )
    print(
        f"Wrote {len(node_actors)} nodes, {len(link_actors)} links, "
        f"{len(actors) - len(node_actors) - len(link_actors)} other actors to {args.output}"
    )
    if transit_stats:
        print(f"Synthetic transit: {transit_stats}")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
