#!/usr/bin/env python3
"""
MATSim/eqasim Toulouse -> Hyperbolic Time Chamber (HTC) hybrid simulator input generator.

Converts the native MATSim Toulouse scenario (network + population + transit schedule) into
the JSON actor format the HTC engine loads at startup. Mirrors the layout and conventions of
../sao-paulo/generate_hybrid_input.py, but Toulouse's inputs are structurally different, so this
is a from-scratch converter rather than a copy:

  - Network:    toulouse_network.xml.gz    -- native MATSim network_v2 (NOT OpenStreetMap).
  - Population: toulouse_population.xml.gz -- native MATSim population_v6 plans (NOT eqasim's
                persons.csv/activities.gpkg/trips.csv triple; those are *derived* from this file,
                so this converter reads the XML directly -- see README "Why MATSim XML, not the
                eqasim CSVs" for the reasoning).
  - Transit:    toulouse_transit_schedule.xml.gz + toulouse_transit_vehicles.xml.gz -- native
                MATSim transitSchedule_v2 (NOT GTFS). Best-effort v1 (see README "Transit scope").

Output directory layout (identical shape to the São Paulo generator):
  <scenario_name>/
  |-- data/
  |   |-- nodes_*.json
  |   |-- links_*.json
  |   |-- persons_*.json
  |   |-- cars_*.json
  |   |-- bikes_*.json         (if bike trips exist)
  |   |-- motorcycles_*.json   (only with dynamic mode choice including motorcycle)
  |   |-- bus_stops_*.json     (if --transit)
  |   |-- buses_*.json         (if --transit; one routed Bus actor per departure)
  |   |-- transit_map.json / transit_routes.json  (if --transit)
  |   |-- id_registry.json
  |   `-- city_map.json
  |-- simulation.json
  |-- scenario_metadata.json
  |-- effective_config.json
  `-- GENERATION_REPORT.md

Usage:
  # Quick test -- 0.1% sample, network + population only:
  python3 generate_hybrid_input.py --scenario tl_test --sample 0.001 \
      --no-transit --no-rail --unsupported-mode walk

  # 1% with routed bus transit, unresolved modes replanned at runtime
  python3 generate_hybrid_input.py --scenario tl_1pct --sample 0.01 --transit \\
      --unsupported-mode replan --mc-modes bus walk

Dependencies: python3, pyproj, scipy, numpy (all already available in this environment).
"""

import argparse
import gc
import gzip
import hashlib
import heapq
import json
import math
import random
import shutil
import statistics
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scenario_common.traffic_signals import (
    build_signal_actor, iter_osm_signals, validate_signal_config, wire_signal_nodes,
)

# ============================================================================
# CONSTANTS
# ============================================================================

# toulouse_config.xml sets global.coordinateSystem = "Atlantis" -- eqasim's convention for
# "the true projected CRS is deliberately not named in metadata" (anonymisation), NOT an
# indication that the coordinates are fictional. The raw x/y magnitudes in
# toulouse_network.xml.gz (e.g. x=575421, y=6278866 for a node near central Toulouse) match
# EPSG:2154 (Lambert-93, the standard French national projection) almost exactly -- Toulouse's
# well-known Lambert-93 coordinates are approximately (574000, 6279000). See README
# "Coordinate system" for the verification and its limits.
_SRC_CRS = "EPSG:2154"
_T_PROJECTED_TO_WGS84 = Transformer.from_crs(_SRC_CRS, "EPSG:4326", always_xy=True)  # rebuilt in main() if --population-crs differs

# Descriptive bucket used only for city_map/report readability and --micro-strategy selection;
# MATSim links carry no OSM-style highway tag, so we approximate one from free-flow speed.
_SPEED_KMH_TO_LINKTYPE = [
    (100.0, "motorway"),
    (80.0, "trunk"),
    (60.0, "primary"),
    (45.0, "secondary"),
    (30.0, "tertiary"),
    (15.0, "residential"),
    (0.0, "living_street"),
]

# Activity types that are MATSim/eqasim routing artefacts, not real activities -- interstitial
# stubs inserted at (near-)zero duration around a mode-changing leg (e.g. "car interaction"
# brackets the actual "car" leg so the walk-to-vehicle / walk-from-vehicle legs have somewhere to
# start/end). Filtered out when reconstructing each person's real activity chain (see
# PopulationConverter._split_plan). Matched by substring, not equality, since eqasim generates
# one such stub per mode ("car interaction", "pt interaction", "bike interaction", ...).
_INTERACTION_MARKER = "interaction"


def _sanitize(v: float, default: float = 0.0, max_v: float = 1e9) -> float:
    if v is None or math.isnan(v) or math.isinf(v):
        return default
    return max(-max_v, min(max_v, v))


def _time_str_to_secs(s: str) -> int:
    """Parse HH:MM:SS (possibly > 24h, as MATSim allows) to total seconds. Invalid input raises
    instead of silently becoming tick 0 (which would make a malformed time look like midnight)."""
    try:
        parts = s.strip().split(":")
        if len(parts) != 3:
            raise ValueError("expected three time fields")
        h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
        if h < 0 or not 0 <= m < 60 or not 0 <= sec < 60:
            raise ValueError("time fields out of range")
        return round(h * 3600 + m * 60 + sec)
    except (ValueError, IndexError, AttributeError) as exc:
        raise ValueError(f"Invalid time {s!r}; expected HH:MM:SS") from exc


def _to_tick(val) -> int:
    """Convert a MATSim time (HH:MM:SS or plain seconds) to an integer tick. Missing or invalid
    values raise; callers that tolerate absence must check before calling."""
    if val is None or str(val).strip() in ("", "nan", "None", "undefined"):
        raise ValueError(f"Missing simulation time: {val!r}")
    text = str(val).strip()
    seconds = float(text) if ":" not in text else _time_str_to_secs(text)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"Invalid simulation time: {val!r}")
    return round(seconds)


def _projected_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    """Return (lat, lon) WGS-84 decimal degrees for a source-CRS (x, y) pair."""
    lon, lat = _T_PROJECTED_TO_WGS84.transform(x, y)
    return _sanitize(lat, 0.0, 90.0), _sanitize(lon, 0.0, 180.0)


def _speed_kmh_to_linktype(speed_kmh: float) -> str:
    for threshold, name in _SPEED_KMH_TO_LINKTYPE:
        if speed_kmh >= threshold:
            return name
    return "residential"


# ============================================================================
# GLOBAL ENTITY ID REGISTRY  (identical convention to sao-paulo)
# ============================================================================

class ScenarioIdRegistry:
    """Assigns a globally-unique `long` id to every scenario entity.

    Replaces the old `htcaid:<type>;<identifier>` string ids with plain, opaque,
    monotonically increasing `long` ids that are unique across all actor types.
    `id_for(kind, source_id)` is deterministic (first request allocates, later
    requests return the same id), so a rerun with the same seed + inputs is stable.
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

    def next_id(self) -> int:
        """Allocates a fresh id with no source key (for synthetic entities)."""
        v = self._next
        self._next += 1
        return v

    def save(self, path: Path) -> None:
        """Persists the mapping so a run's ids can be audited / reproduced."""
        path.write_text(json.dumps({
            "next": self._next,
            "ids": {f"{k[0]}:{k[1]}": v for k, v in self._ids.items()},
        }))

    def load(self, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text())
        self._next = data.get("next", 1)
        for key, v in data.get("ids", {}).items():
            kind, source = key.split(":", 1)
            self._ids[(kind, source)] = v


# ============================================================================
# STREAMING FILE WRITER  (identical convention to sao-paulo's StreamingWriter)
# ============================================================================


class StreamingWriter:
    """Incrementally writes items into split JSON array files of bounded size."""

    def __init__(self, output_dir: Path, base: str, items_per_file: int = 50_000):
        self.output_dir = output_dir
        self.base = base
        self.items_per_file = items_per_file
        self._buf: List[Dict] = []
        self._file_idx = 0
        self.total = 0

    def add(self, item: Dict) -> None:
        self._buf.append(item)
        self.total += 1
        if len(self._buf) >= self.items_per_file:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        fp = self.output_dir / f"{self.base}_{self._file_idx}.json"
        with open(fp, "w") as f:
            json.dump(self._buf, f, separators=(",", ":"))
        self._buf.clear()
        self._file_idx += 1
        gc.collect()

    def close(self) -> Dict:
        self._flush()
        return {"total": self.total, "files": self._file_idx}


class CityMapWriter:
    """Writes city_map.json incrementally (nodes + edges arrays), same shape as sao-paulo's."""

    def __init__(self, path: Path, ids: ScenarioIdRegistry):
        self.ids = ids
        self._path = path
        self._f = open(path, "w")
        self._f.write('{"nodes":[')
        self._first_node = True
        self._nodes_done = False
        self._first_edge = True

    def add_node(self, nid: str, lat: float, lon: float) -> None:
        entry = json.dumps(
            {
                "id": self.ids.id_for("node", nid),
                "classType": "hybrid.actor.Node",
                "resourceId": self.ids.id_for("node", nid),
                "latitude": lat,
                "longitude": lon,
            },
            separators=(",", ":"),
        )
        if not self._first_node:
            self._f.write(",")
        self._f.write(entry)
        self._first_node = False

    def _start_edges(self) -> None:
        if not self._nodes_done:
            self._f.write('],"edges":[')
            self._nodes_done = True

    def add_edge(self, lid: str, from_id: str, to_id: str, length: float, speed: float) -> None:
        self._start_edges()
        sanitized_length = _sanitize(length, 1.0)
        entry = json.dumps(
            {
                "source_id": self.ids.id_for("node", from_id),
                "target_id": self.ids.id_for("node", to_id),
                "weight": sanitized_length,
                "label": {
                    "id": self.ids.id_for("link", lid),
                    "resourceId": self.ids.id_for("link", lid),
                    "classType": "hybrid.actor.Link",
                    "length": sanitized_length,
                },
            },
            separators=(",", ":"),
        )
        if not self._first_edge:
            self._f.write(",")
        self._f.write(entry)
        self._first_edge = False

    def close(self) -> None:
        if not self._nodes_done:
            self._start_edges()
        self._f.write('],"directed":true}')
        self._f.close()


# ============================================================================
# CONFIGURATION
# ============================================================================


@dataclass
class Config:
    input_dir: Path
    output_dir: Path
    scenario_name: str

    sample_rate: float = 0.01
    sampling_strategy: str = "prefix"
    population_size: Optional[int] = None
    reuse_map: bool = True
    map_cache_dir: Optional[Path] = None
    random_seed: int = 42

    micro_ratio: float = 0.0
    micro_strategy: str = "highway"
    micro_time_step: float = 0.1
    micro_ticks_per_tick: int = 10

    include_transit: bool = False
    include_signals: bool = False
    signal_osm_file: Optional[Path] = None
    signal_max_snap_distance: float = 50.0
    signal_cycle_duration: int = 120
    signal_green_ratio: float = 0.45
    signal_yellow_duration: int = 3
    signal_all_red_duration: int = 2
    max_transit_routes: int = 0        # cap on converted bus transitRoutes (file order); 0 = all
    transit_default_headway: int = 900   # fallback headway (s) for routes with < 2 departures
    max_buses_per_route: int = 0        # Bus actors per transitRoute (earliest departures); 0 = all

    # Rail (rail / subway / tram transitRoutes of the same schedule). One dominant stop pattern per
    # transitLine becomes a line served in both directions; see RailScheduleConverter.
    include_rail: bool = False
    max_trains_per_line: int = 0         # trains dispatched per line per direction; 0 = all in horizon
    train_capacity: int = 1200           # fallback when the vehicle type is unknown
    train_velocity: float = 22.22        # m/s fallback when the schedule offsets give no speed
    train_stop_time: int = 30            # seconds dwell at each station
    train_interval: int = 300            # fallback headway (s) for lines with < 2 departures
    train_number_of_ports: int = 8       # doors per side

    driver_aggressiveness_min: float = 0.3
    driver_aggressiveness_max: float = 0.9
    driver_reaction_time_min: float = 0.8
    driver_reaction_time_max: float = 1.2
    driver_max_speed_factor_min: float = 0.9
    driver_max_speed_factor_max: float = 1.1
    driver_min_gap_factor_min: float = 0.8
    driver_min_gap_factor_max: float = 1.2

    # Dynamic mode choice. When True every trip becomes a PendingDecision plan element resolved
    # at runtime by the ModeDecisionEngine named by mode_choice_strategy. Engine ids must match
    # ModeDecisionEngineRegistry exactly: "raptor" | "nearest-stop-utility" | "travel-time".
    enable_dynamic_mode_choice: bool = False
    mode_choice_strategy: str = "nearest-stop-utility"
    mc_included_modes: List[str] = field(default_factory=lambda: ["bus", "walk"])
    mc_beta_mode: float = 1.0
    mc_beta_access: float = 0.001
    mc_beta_egress: float = 0.001
    mc_beta_private_vehicle: float = 0.00005
    mc_mode_pref_subway: float = 2.0
    mc_mode_pref_bus: float = 1.0
    mc_mode_pref_walk: float = 0.0
    mc_mode_pref_car: float = 3.0
    mc_mode_pref_bicycle: float = 1.5
    mc_mode_pref_motorcycle: float = 2.0
    mc_max_access_distance_m: float = 1500.0
    mc_max_walk_distance_m: float = 2000.0
    mc_beta_travel_time: float = 0.01
    mc_walking_speed_ms: float = 1.4
    mc_avg_bus_speed_ms: float = 8.33
    mc_avg_subway_speed_ms: float = 12.5
    mc_avg_bicycle_speed_ms: float = 5.56
    mc_avg_motorcycle_speed_ms: float = 11.11
    mc_avg_car_speed_ms: float = 13.89

    items_per_file: int = 50_000
    output_format: str = "json"  # json | sqlite
    duration: int = 86_400
    start_real_time: str = "2025-01-01T00:00:00.000"
    docker_base: str = "/app/hyperbolic-time-chamber/simulations/input"

    network_file: Optional[Path] = None
    population_file: Optional[Path] = None
    transit_schedule_file: Optional[Path] = None
    transit_vehicles_file: Optional[Path] = None

    # Source CRS shared by the network, the plans and the transit schedule (they come from the
    # same MATSim run). toulouse_config.xml says "Atlantis" (eqasim anonymisation), so this is
    # an explicit, recorded assumption -- see README "Coordinate system".
    population_crs: str = _SRC_CRS
    # What to do with a trip whose MATSim mode has no native HTC leg (pt, car_passenger, ...):
    # error (default, abort) | walk (WalkLeg, counted) | skip-person | replan (PendingDecision).
    unsupported_mode: str = "error"
    # Activities / stops farther than this from the nearest main-component node are not snapped.
    max_snap_distance: float = 1000.0
    # Write owner/part_of on each private vehicle (partial-simulation filter). On by default;
    # --no-declare-ownership restores the old output.
    declare_ownership: bool = True

    def validate(self) -> None:
        validate_signal_config(self)
        if not math.isfinite(self.signal_max_snap_distance) or self.signal_max_snap_distance <= 0:
            raise ValueError("signal-max-snap-distance must be finite and positive")
        if (not self.scenario_name or Path(self.scenario_name).name != self.scenario_name
                or self.scenario_name in (".", "..")):
            raise ValueError("scenario must be a single directory name")
        if not 0 < self.sample_rate <= 1 or not 0 <= self.micro_ratio <= 1:
            raise ValueError("sample must be in (0,1] and micro-ratio in [0,1]")
        if self.sampling_strategy not in ("prefix", "random"):
            raise ValueError("sampling-strategy must be prefix or random")
        if self.population_size is not None and (type(self.population_size) is not int or self.population_size <= 0):
            raise ValueError("population-size must be a positive integer")
        for name in ("items_per_file", "duration", "transit_default_headway",
                     "train_capacity", "train_interval", "train_stop_time"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not math.isfinite(self.max_snap_distance) or self.max_snap_distance <= 0:
            raise ValueError("max-snap-distance must be finite and positive")
        if self.unsupported_mode not in ("error", "walk", "skip-person", "replan"):
            raise ValueError("Invalid unresolved-mode policy")
        if self.output_format not in ("json", "sqlite"):
            raise ValueError("Invalid output format")
        if self.max_buses_per_route < 0 or self.max_transit_routes < 0 or self.max_trains_per_line < 0:
            raise ValueError("bus/route/train caps must be >= 0 (0 means all)")
        if not math.isfinite(self.train_velocity) or self.train_velocity <= 0:
            raise ValueError("train-velocity must be finite and positive")
        datetime.fromisoformat(self.start_real_time)
        CRS.from_user_input(self.population_crs)
        if self.unsupported_mode == "replan" and not (self.include_transit or self.include_rail):
            raise ValueError("replan requires --transit and/or --rail")
        valid_modes = set(PopulationConverter.MC_MODE_TO_CONCRETE)
        if not self.mc_included_modes or set(self.mc_included_modes) - valid_modes:
            raise ValueError(f"mc-modes must be drawn from {sorted(valid_modes)}")
        if self.enable_dynamic_mode_choice or self.unsupported_mode == "replan":
            if "bus" in self.mc_included_modes and not self.include_transit:
                raise ValueError("bus mode choice requires --transit")
            if "subway" in self.mc_included_modes and not self.include_rail:
                raise ValueError("subway mode choice requires --rail")
        for prefix in ("driver_aggressiveness", "driver_reaction_time",
                       "driver_max_speed_factor", "driver_min_gap_factor"):
            lo, hi = getattr(self, prefix + "_min"), getattr(self, prefix + "_max")
            if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi):
                raise ValueError(f"invalid {prefix} range")


# ============================================================================
# NETWORK PARSER  (single-pass streaming over MATSim network_v2 XML)
# ============================================================================


class MatsimNetworkParser:
    """
    Streams toulouse_network.xml.gz once: MATSim networks list every <node> before any <link>,
    so a single iterparse pass writes NodeState actors as nodes are seen, then switches to
    building LinkState actors once the first <link> is seen -- no OSM-style two-pass "needed
    node id" bookkeeping is required, because MATSim networks contain *only* routing-relevant
    nodes (unlike OSM, which has orders of magnitude more nodes than intersections).

    Unlike OSM ways (one way = both directions, requiring us to synthesize a reverse link),
    MATSim already encodes each direction as its own <link> element -- so one LinkState actor
    is emitted per <link>, with no reverse-link synthesis.
    """

    def __init__(self, cfg: Config, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.data_dir = data_dir
        self.ids = ids
        self._rng = random.Random(cfg.random_seed)

        self.node_xy: Dict[str, Tuple[float, float]] = {}
        self.node_latlon: Dict[str, Tuple[float, float]] = {}
        self._kd_ids: List[str] = []
        self.kdtree: Optional[cKDTree] = None

        self._adj_fwd: Dict[str, List[str]] = defaultdict(list)
        self._adj_rev: Dict[str, List[str]] = defaultdict(list)
        self.main_component: Optional[Set[str]] = None

        self.stats: Dict[str, int] = defaultdict(int)

        self._node_writer: Optional[StreamingWriter] = None
        self._link_writer: Optional[StreamingWriter] = None
        # Ids of road links a bus may use; persisted as bus_routing_links.json for BusRoadRouter.
        self._bus_link_ids: Set[int] = set()
        self._node_incoming_links: Dict[str, List[str]] = defaultdict(list)

    # Link modes that make a MATSim link part of the road network. Rail/train/tram-only links
    # (the network also carries them) are not roads: emitting them as Link actors would let cars
    # and walkers route over railway tracks.
    _ROAD_MODES = ("car", "bus")

    # ------------------------------------------------------------------
    def parse(self, network_file: Path) -> None:
        print(f"  [NET] streaming MATSim network: {network_file.name}")
        self._node_writer = StreamingWriter(self.data_dir, "nodes", self.cfg.items_per_file)
        self._link_writer = StreamingWriter(self.data_dir, "links", self.cfg.items_per_file)

        fh = gzip.open(network_file, "rb") if network_file.suffix == ".gz" else open(
            network_file, "rb"
        )
        n_nodes = 0
        n_links = 0
        with fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            root: Optional[ET.Element] = None
            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem
                    continue
                if event != "end":
                    continue

                if elem.tag == "node":
                    nid = elem.get("id")
                    x, y = float(elem.get("x")), float(elem.get("y"))
                    lat, lon = _projected_to_wgs84(x, y)
                    self.node_xy[nid] = (x, y)
                    self.node_latlon[nid] = (lat, lon)
                    self._kd_ids.append(nid)
                    self._node_writer.add(self._build_node_actor(nid, lat, lon))
                    n_nodes += 1
                    if n_nodes % 100_000 == 0:
                        print(f"        ... {n_nodes:,} nodes written", end="\r")

                elif elem.tag == "link":
                    self._emit_link(elem)
                    n_links += 1
                    if n_links % 200_000 == 0:
                        print(f"        ... {n_links:,} links written", end="\r")

                if root is not None and elem.tag in ("node", "link") and len(root) > 20_000:
                    root.clear()

            del ctx
        gc.collect()
        print(f"        {n_nodes:,} nodes, {n_links:,} links parsed" + " " * 10)

        node_r = self._node_writer.close()
        link_r = self._link_writer.close()
        self.stats["nodes"] = node_r["total"]
        self.stats["files_nodes"] = node_r["files"]
        self.stats["links"] = link_r["total"]
        self.stats["files_links"] = link_r["files"]

        (self.data_dir / "bus_routing_links.json").write_text(json.dumps(sorted(self._bus_link_ids)))
        self._find_main_component()
        self._build_kdtree()

    # ------------------------------------------------------------------
    def _build_node_actor(self, nid: str, lat: float, lon: float) -> Dict:
        return {
            "id": self.ids.id_for("node", nid),
            "typeActor": "hybrid.actor.Node",
            "data": {
                "dataType": "model.hybrid.entity.state.NodeState",
                "content": {
                    "startTick": 0,
                    "latitude": lat,
                    "longitude": lon,
                    "links": [],
                    "connections": {},
                    "signals": {},
                    "busStops": {},
                    "subwayStations": {},
                    "signalWaitingCounts": {},
                    "scheduleOnTimeManager": False,
                },
            },
            "dependencies": {},
        }

    # ------------------------------------------------------------------
    def _emit_link(self, elem: ET.Element) -> None:
        lid = elem.get("id")
        from_id = elem.get("from")
        to_id = elem.get("to")
        if from_id not in self.node_xy or to_id not in self.node_xy:
            self.stats["links_skipped_missing_node"] += 1
            return

        length = _sanitize(float(elem.get("length", "0")), 1.0)
        freespeed_ms = _sanitize(float(elem.get("freespeed", "8.33")), 8.33, 100.0)
        speed_kmh = freespeed_ms * 3.6
        capacity = _sanitize(float(elem.get("capacity", "600")), 600.0)
        permlanes = elem.get("permlanes", "1.0")
        try:
            lanes = max(1, round(float(permlanes)))
        except ValueError:
            lanes = 1
        modes_attr = elem.get("modes", "car")
        modes = [m.strip() for m in modes_attr.split(",") if m.strip()]
        if not any(m in self._ROAD_MODES for m in modes):
            self.stats["links_skipped_non_road_mode"] += 1
            return
        link_type = _speed_kmh_to_linktype(speed_kmh)
        sim_mode = self._select_mode(link_type)

        self._adj_fwd[from_id].append(to_id)
        self._adj_rev[to_id].append(from_id)

        lane_cfgs = []
        for lane_i in range(lanes):
            ltype = "normal"
            if lanes >= 3 and lane_i == 0 and "pt" in modes:
                ltype = "bus_lane"
            elif lanes >= 2 and lane_i == lanes - 1 and "bike" in modes:
                ltype = "bike_lane"
            lane_cfgs.append({"laneId": lane_i, "type": ltype, "width": 3.5, "speedLimit": None})

        link_id = self.ids.id_for("link", lid)
        if self.cfg.include_signals:
            self._node_incoming_links[to_id].append(lid)
        self._link_writer.add(
            {
                "id": link_id,
                "typeActor": "hybrid.actor.Link",
                "data": {
                    "dataType": "model.hybrid.entity.state.LinkState",
                    "content": {
                        "startTick": 0,
                        "from": self.ids.id_for("node", from_id),
                        "to": self.ids.id_for("node", to_id),
                        "length": length,
                        "lanes": lanes,
                        "speedLimit": speed_kmh,
                        "freeSpeed": speed_kmh,
                        "capacity": capacity,
                        "simulationMode": sim_mode,
                        "microTimeStep": self.cfg.micro_time_step,
                        "microTicksPerGlobalTick": self.cfg.micro_ticks_per_tick,
                        "laneConfigurations": lane_cfgs,
                        "congestionFactor": 1.0,
                        "currentSpeed": speed_kmh,
                        "registered": [],
                        "vehiclesByLane": {},
                    },
                },
                "dependencies": {
                    "from_node": {"id": self.ids.id_for("node", from_id), "classType": "hybrid.actor.Node"},
                    "to_node": {"id": self.ids.id_for("node", to_id), "classType": "hybrid.actor.Node"},
                },
            }
        )
        if "car" in modes or "bus" in modes:
            self._bus_link_ids.add(link_id)
        if sim_mode == "MICRO":
            self.stats["micro_links"] += 1
        else:
            self.stats["meso_links"] += 1

    # ------------------------------------------------------------------
    def _select_mode(self, link_type: str) -> str:
        if self.cfg.micro_ratio <= 0:
            return "MESO"
        if self.cfg.micro_ratio >= 1.0:
            return "MICRO"
        strat = self.cfg.micro_strategy
        if strat == "highway" and link_type in ("motorway", "trunk"):
            return "MICRO"
        if strat == "arterial" and link_type in ("primary", "secondary"):
            return "MICRO"
        return "MICRO" if self._rng.random() < self.cfg.micro_ratio else "MESO"

    # ------------------------------------------------------------------
    def _find_main_component(self) -> None:
        """Bidirectional BFS from the highest out-degree node -- same approach as sao-paulo's
        OsmNetworkParser, so activity/stop snapping never lands on a node with no real route
        to/from the rest of the network."""

        def bfs(adj: Dict[str, List[str]], start: str) -> Set[str]:
            visited: Set[str] = {start}
            queue: deque = deque([start])
            while queue:
                node = queue.popleft()
                for nbr in adj[node]:
                    if nbr not in visited:
                        visited.add(nbr)
                        queue.append(nbr)
            return visited

        if not self._adj_fwd:
            raise ValueError("MATSim network produced no directed road links; check the input")

        seed = max(self._adj_fwd, key=lambda n: len(self._adj_fwd[n]))
        fwd_set = bfs(self._adj_fwd, seed)
        rev_set = bfs(self._adj_rev, seed)
        self.main_component = fwd_set & rev_set

        all_nodes = set(self._adj_fwd.keys()) | set(self._adj_rev.keys())
        self.stats["main_component_nodes"] = len(self.main_component)
        self.stats["isolated_nodes"] = len(all_nodes) - len(self.main_component)
        self._adj_fwd.clear()
        self._adj_rev.clear()

    # ------------------------------------------------------------------
    def _build_kdtree(self) -> None:
        mc_ids = [nid for nid in self._kd_ids if nid in self.main_component]
        excluded = len(self._kd_ids) - len(mc_ids)
        print(
            f"  [NET] building KD-tree over {len(mc_ids):,} main-component nodes "
            f"({excluded:,} isolated nodes excluded) ..."
        )
        if mc_ids:
            self._kd_ids = mc_ids
            coords = np.array([self.node_xy[nid] for nid in self._kd_ids])
            self.kdtree = cKDTree(coords)

    # ------------------------------------------------------------------
    def nearest_node(self, x: float, y: float) -> Optional[str]:
        """Nearest main-component network node to a projected (x, y) pair, or None when the
        nearest node is farther than ``max_snap_distance`` (the point is not served by the
        network -- snapping it anyway would teleport a person/stop across the map). Population,
        stop and node coordinates come from the same MATSim run, so they already share one
        projected space and no CRS conversion is needed here (only NodeState's own lat/lon
        needs WGS-84, for the engine's A* heuristic)."""
        if self.kdtree is None or not math.isfinite(x) or not math.isfinite(y):
            return None
        distance, idx = self.kdtree.query([x, y])
        if distance > self.cfg.max_snap_distance:
            return None
        return self._kd_ids[idx]


# ============================================================================
# POPULATION CONVERTER  (streaming over MATSim population_v6 plans XML)
# ============================================================================


def _population_count_cache(path: Path) -> Path:
    return path.with_name(path.name + ".count.json")


def _save_population_count(path: Path, count: int) -> None:
    stat = path.stat()
    try:
        _population_count_cache(path).write_text(json.dumps({
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "persons": count,
        }))
    except OSError as exc:
        print(f"  [POP] could not cache population count: {exc}")


def _population_count(path: Path) -> int:
    stat = path.stat()
    try:
        cached = json.loads(_population_count_cache(path).read_text())
        if (cached["size"] == stat.st_size and cached["mtime_ns"] == stat.st_mtime_ns
                and type(cached["persons"]) is int and cached["persons"] >= 0):
            return cached["persons"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print("  [POP] counting source persons once; future runs reuse the count cache "
          "(use --population-size to avoid this pass)")
    count = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        ctx = ET.iterparse(fh, events=("start", "end"))
        _, root = next(ctx)
        for event, elem in ctx:
            if event == "end" and elem.tag == "person":
                count += 1
                root.remove(elem)
    _save_population_count(path, count)
    return count


class PopulationConverter:
    """
    Streams toulouse_population.xml.gz once via iterparse, one <person> at a time (each
    person's selected plan is fully buffered -- a day of activities/legs, not the whole
    population -- then written out immediately, so memory stays bounded regardless of size).

    Real MATSim/eqasim plans interleave "interaction" stub activities (near-zero duration, same
    coordinates as the surrounding real activity) around every mode-changing leg, e.g.:
        home -> walk -> [car interaction] -> car -> [car interaction] -> walk -> work
    _matsim_plan() collapses each stub-bracketed leg group into a single trip between two real
    activities, with the trip's main mode being the single non-walk mode of the group (the same
    "ignore access/egress walk, keep the vehicle leg" convention eqasim uses when it derives
    trips.csv from this same file). A group with two different non-walk modes is ambiguous and
    aborts rather than being guessed.

    The plan model, the unresolved-mode policy and the vehicle actors deliberately mirror
    ../sao-paulo/generate_hybrid_input.py's PopulationConverter (same PersonState.originalPlan
    elements, same CarState/BicycleState/MotorcycleState layout), so both generators emit
    interchangeable input for the HTC engine.
    """

    VEHICLE_MODES: Dict[str, Tuple[str, str, str]] = {
        # mode -> (actor_class, state_type, vehicle_class_key)
        "car":        ("hybrid.actor.Car",        "model.hybrid.entity.state.CarState",        "car"),
        "bike":       ("hybrid.actor.Bicycle",    "model.hybrid.entity.state.BicycleState",    "bike"),
        "motorcycle": ("hybrid.actor.Motorcycle", "model.hybrid.entity.state.MotorcycleState", "motorcycle"),
    }
    VEHICLE_SIZE: Dict[str, float] = {"car": 4.5, "bike": 2.0, "motorcycle": 2.5}

    # MATSim mode string -> PersonState.originalPlan's ConcreteMode name
    # (Walk|Car|Bicycle|Motorcycle|Bus|Subway). Modes absent here (pt, car_passenger, ...) have no
    # native private-vehicle/walk leg and are handled by the explicit --unsupported-mode policy.
    MODE_TO_CONCRETE: Dict[str, str] = {
        "car": "Car", "bike": "Bicycle", "motorcycle": "Motorcycle", "walk": "Walk",
    }
    CONCRETE_TO_VEHICLE_MODE: Dict[str, str] = {
        "Car": "car", "Bicycle": "bike", "Motorcycle": "motorcycle",
    }
    MC_MODE_TO_CONCRETE: Dict[str, str] = {
        "car": "Car", "bicycle": "Bicycle", "motorcycle": "Motorcycle",
        "bus": "Bus", "subway": "Subway", "walk": "Walk",
    }
    # NOT mode.capitalize(): ActorTypeEnum has "Bicycle", not "Bike" (enum deserialisation fails).
    VEHICLE_MODE_TO_ACTOR_TYPE: Dict[str, str] = {
        "car": "Car", "bike": "Bicycle", "motorcycle": "Motorcycle",
    }
    _MODE_ALIASES = {"bicycle": "bike", "transit_walk": "walk", "access_walk": "walk",
                     "egress_walk": "walk"}

    def __init__(self, cfg: Config, net: MatsimNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.net = net
        self.data_dir = data_dir
        self.ids = ids
        self._rng = random.Random(cfg.random_seed + 1)
        self._writers: Dict[str, StreamingWriter] = {
            "persons": StreamingWriter(data_dir, "persons", cfg.items_per_file),
            "cars": StreamingWriter(data_dir, "cars", cfg.items_per_file),
            "bikes": StreamingWriter(data_dir, "bikes", cfg.items_per_file),
            "motorcycles": StreamingWriter(data_dir, "motorcycles", cfg.items_per_file),
        }
        self.stats: Dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    def convert(self, population_file: Path) -> None:
        print(f"  [POP] streaming MATSim population: {population_file.name}")
        target = None
        total = None
        if self.cfg.sampling_strategy == "prefix" and self.cfg.sample_rate < 1:
            total = self.cfg.population_size
            if total is None:
                total = _population_count(population_file)
            target = math.ceil(total * self.cfg.sample_rate)
            self.stats["persons_target"] = target
            print(f"  [POP] target: {target:,} generated persons; stopping in file order")
        stopped_early = False
        opener = gzip.open if population_file.suffix == ".gz" else open
        with opener(population_file, "rb") as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            _, root = next(ctx)
            for event, elem in ctx:
                if target is not None and self.stats["persons_emitted"] >= target:
                    stopped_early = True
                    break
                if event != "end" or elem.tag != "person":
                    continue
                self.stats["persons_scanned"] += 1
                if self.stats["persons_scanned"] % 200_000 == 0:
                    print(f"        ... {self.stats['persons_scanned']:,} persons scanned", end="\r")
                if (self.cfg.sampling_strategy == "prefix" or self.cfg.sample_rate >= 1
                        or self._rng.random() < self.cfg.sample_rate):
                    plans = elem.findall("plan")
                    selected = [p for p in plans if p.get("selected") == "yes"]
                    if not selected and len(plans) == 1 and plans[0].get("selected") != "no":
                        selected = plans
                    if not plans:
                        self.stats["persons_skipped_empty_plan"] += 1
                    elif len(selected) != 1:
                        raise ValueError(f"Person {elem.get('id')}: expected one selected plan")
                    else:
                        acts, trips = self._matsim_plan(selected[0], elem.get("id"))
                        if acts:
                            self._emit_person(elem.get("id"), acts, trips)
                        else:
                            self.stats["persons_skipped_no_real_activities"] += 1
                root.remove(elem)
        print(f"        {self.stats['persons_scanned']:,} persons scanned" + " " * 20)
        self.stats["population_stopped_early"] = int(stopped_early)
        if stopped_early:
            self.stats["persons_total_in_file"] = total
        else:
            self.stats["persons_total_in_file"] = self.stats["persons_scanned"]
            _save_population_count(population_file, self.stats["persons_scanned"])
        if target is not None and self.stats["persons_emitted"] < target:
            print(f"  [POP] WARNING: source exhausted before target: "
                  f"{self.stats['persons_emitted']:,}/{target:,} valid persons generated")

        for w in self._writers.values():
            r = w.close()
            self.stats[f"files_{w.base}"] = r["files"]
            self.stats[w.base] = r["total"]

        print(f"  [POP] done - {self.stats['persons_emitted']:,} persons, {self.stats['cars']:,} cars, "
              f"{self.stats['bikes']:,} bikes")
        self._validate_reachability()

    # ------------------------------------------------------------------
    def _matsim_plan(self, plan: ET.Element, pid: str = "?") -> Tuple[List[Dict], List[Dict]]:
        """Collapse one selected <plan> into (acts, trips) -- see the class docstring."""
        acts, trips, legs = [], [], []
        for elem in plan:
            if elem.tag == "leg":
                legs.append(elem)
                continue
            if elem.tag not in ("activity", "act"):
                continue
            if (elem.get("type") or "").lower().endswith(_INTERACTION_MARKER):
                self.stats["matsim_stage_activities_collapsed"] += 1
                continue
            x, y = elem.get("x"), elem.get("y")
            if x is None or y is None:
                raise ValueError(f"Person {pid}: MATSim activity requires x/y; "
                                 "link/facility-only locations need an external lookup")
            end, dur = elem.get("end_time"), elem.get("dur") or elem.get("max_dur")
            if end and dur:
                self.stats["matsim_duration_overridden_by_end_time"] += 1
            if end and end != "undefined":
                spec = {"kind": "AtTick", "tick": _to_tick(end)}
            elif dur and dur != "undefined":
                spec = {"kind": "Duration", "ticks": _to_tick(dur)}
            else:
                spec = {"kind": "AtTick", "tick": self.cfg.duration}
                self.stats["matsim_activity_end_fallback_horizon"] += 1
            if acts:
                if not legs:
                    raise ValueError(f"Person {pid}: consecutive main activities without a leg")
                modes = [self._MODE_ALIASES.get((l.get("mode") or "").lower(), (l.get("mode") or "").lower())
                         for l in legs]
                main_modes = set(modes) - {"walk"}
                if len(main_modes) > 1:
                    raise ValueError(f"Person {pid}: mixed-mode trip needs an explicit mapping: {modes}")
                mode = next(iter(main_modes), "walk")
                departure = legs[0].get("dep_time")
                dep_tick = (_to_tick(departure) if departure and departure != "undefined"
                            else acts[-1]["end_spec"].get("tick", 0))
                if departure and departure != "undefined":
                    acts[-1]["end_spec"] = {"kind": "AtTick", "tick": dep_tick}
                trips.append({"trip_id": len(trips), "mode": mode, "dep_time": dep_tick,
                              "arr_time": 0, "prec_idx": len(acts) - 1, "foll_idx": len(acts)})
                legs = []
            acts.append({"activity_id": len(acts), "purpose": elem.get("type", ""),
                         "start_time": None, "end_time": end, "end_spec": spec,
                         "is_last": False, "x": float(x), "y": float(y)})
        if legs:
            raise ValueError(f"Person {pid}: plan ends with a leg without a destination activity")
        if acts:
            acts[-1]["is_last"] = True
        return acts, trips

    # ------------------------------------------------------------------
    def _validate_reachability(self) -> None:
        """Abort if any activity was snapped to a node outside the road network's main
        component -- such trips would have no real route. A regression tripwire for the KD-tree
        filtering in MatsimNetworkParser._build_kdtree(), not an expected outcome."""
        snapped = self.stats.get("activity_nodes_snapped", 0)
        off_mc = self.stats.get("activity_nodes_off_main_component", 0)
        if snapped == 0 or off_mc == 0:
            return
        raise RuntimeError(
            f"[POP] reachability check failed: {off_mc:,}/{snapped:,} ({off_mc / snapped:.4%}) "
            f"activity nodes fall outside the road network's main connected component -- trips "
            f"to/from them have no real route. Indicates the KD-tree was not filtered to "
            f"main-component nodes (see MatsimNetworkParser._build_kdtree())."
        )

    # ------------------------------------------------------------------
    def _emit_person(self, pid: str, acts: List[Dict], trips: List[Dict]) -> None:
        """Build and write a Person actor + any owned vehicle actors."""
        if not trips:
            self.stats["persons_skipped_no_trips"] += 1
            return

        unresolved = [t for t in trips if t["mode"] not in self.MODE_TO_CONCRETE]
        if unresolved and not self.cfg.enable_dynamic_mode_choice:
            self.stats["trips_unresolved_mode"] += len(unresolved)
            if self.cfg.unsupported_mode == "error":
                raise ValueError(
                    f"Person {pid}: unresolved modes {sorted({t['mode'] for t in unresolved})}; "
                    "choose --unsupported-mode replan, skip-person, or walk explicitly")
            if self.cfg.unsupported_mode == "skip-person":
                self.stats["persons_skipped_unsupported_mode"] += 1
                return
        if any(not (0 <= t["prec_idx"] < len(acts) and 0 <= t["foll_idx"] < len(acts)) for t in trips):
            raise ValueError(f"Person {pid}: trip references a missing activity")
        rng = self._rng

        # -- Map each activity to its nearest network node --
        node_ids: List[Optional[str]] = []
        for act in acts:
            snapped = self.net.nearest_node(act["x"], act["y"])
            if snapped is None:
                self.stats["persons_skipped_snap_distance"] += 1
                return
            node_ids.append(snapped)
            self.stats["activity_nodes_snapped"] += 1
            if self.net.main_component is not None and snapped not in self.net.main_component:
                # Never expected: the KD-tree only holds main-component nodes. Tripwire.
                self.stats["activity_nodes_off_main_component"] += 1

        # -- One vehicle actor per vehicle type used (not per trip) --
        vehicle_actors: Dict[str, int] = {}
        first_dep: Dict[str, int] = {}
        first_node: Dict[str, Optional[str]] = {}
        for t in trips:
            mode = t["mode"]
            if mode not in self.VEHICLE_MODES or mode in vehicle_actors:
                continue
            orig_node = node_ids[t["prec_idx"]]
            if orig_node is None:
                continue
            vehicle_actors[mode] = self.ids.id_for(mode.lower(), f"{pid}_v_{mode}")
            first_dep[mode] = t["dep_time"]
            first_node[mode] = orig_node

        # Runtime mode choice may pick a private vehicle the person did not use in the source
        # plan, so it must already exist (parked at the first activity).
        if self.cfg.enable_dynamic_mode_choice or (unresolved and self.cfg.unsupported_mode == "replan"):
            for name in self.cfg.mc_included_modes:
                mode = "bike" if name == "bicycle" else name
                if mode in self.VEHICLE_MODES and mode not in vehicle_actors:
                    vehicle_actors[mode] = self.ids.id_for(mode, f"{pid}_v_{mode}")
                    first_dep[mode], first_node[mode] = trips[0]["dep_time"], node_ids[0]

        for mode, v_id in vehicle_actors.items():
            wkey = {"car": "cars", "bike": "bikes", "motorcycle": "motorcycles"}[mode]
            self._writers[wkey].add(self._build_vehicle(
                v_id, mode, first_node[mode], first_dep[mode], rng,
                owner_id=self.ids.id_for("person", pid)))

        # -- Native plan: a flat List[PlanElement] (Activity / WalkLeg / PrivateVehicleLeg /
        #    PendingDecision), per PersonState.originalPlan (docs/SCENARIO_MODELING.md 4.11). --
        plan_elements: List[Dict] = []
        trip_by_prec: Dict[int, Dict] = {t["prec_idx"]: t for t in trips}

        for i, act in enumerate(acts):
            node_id = node_ids[i]
            dep_trip = trip_by_prec.get(i)
            plan_elements.append({
                "kind": "Activity",
                "activityType": act["purpose"],
                "nodeId": self.ids.id_for("node", node_id),
                "endTime": act["end_spec"],
            })
            if dep_trip is None:
                continue
            dest_idx = dep_trip["foll_idx"]
            dest_node = node_ids[dest_idx] if dest_idx < len(node_ids) else None
            if dest_node is None:
                self.stats["trips_skipped_no_dest_node"] += 1
                continue

            if self.cfg.enable_dynamic_mode_choice or (
                    self.cfg.unsupported_mode == "replan" and dep_trip["mode"] not in self.MODE_TO_CONCRETE):
                self.stats["trips_replanned"] += 1
                plan_elements.append({
                    "kind": "PendingDecision",
                    "decision": {
                        "allowedModes": [self.MC_MODE_TO_CONCRETE.get(m, m.capitalize())
                                         for m in self.cfg.mc_included_modes],
                        "strategyId": self.cfg.mode_choice_strategy,
                        "weightsOverride": None,
                    },
                })
            else:
                plan_elements.append(self._build_leg_element(
                    dep_trip["mode"], node_id, dest_node, vehicle_actors, rng))

        if not plan_elements:
            self.stats["persons_skipped_empty_schedule"] += 1
            return

        owned: Dict[str, Dict] = {}
        for mode, v_id in vehicle_actors.items():
            owned["bicycle" if mode == "bike" else mode] = {"id": v_id, "classType": self.VEHICLE_MODES[mode][0]}

        # plan_elements[0] is always the first Activity: its endTime.tick (when the person first
        # leaves home) distributes actor wake-up ticks through the day. A Duration end has no tick.
        first_act_start_tick = plan_elements[0]["endTime"].get("tick", 0)

        person = {
            "id": self.ids.id_for("person", pid),
            "typeActor": "hybrid.actor.Person",
            "data": {
                "dataType": "model.hybrid.entity.state.PersonState",
                "content": {
                    "startTick": first_act_start_tick,
                    "scheduleOnTimeManager": True,
                    "originalPlan": plan_elements,
                    "ownedVehicles": owned,
                    "totalDistanceTraveled": 0.0,
                    "completedTrips": 0,
                    "modeChoiceWeights": {
                        "includedModes": self.cfg.mc_included_modes,
                        "betaMode": self.cfg.mc_beta_mode,
                        "betaAccess": self.cfg.mc_beta_access,
                        "betaEgress": self.cfg.mc_beta_egress,
                        "betaPrivateVehicle": self.cfg.mc_beta_private_vehicle,
                        "modePrefSubway": self.cfg.mc_mode_pref_subway,
                        "modePrefBus": self.cfg.mc_mode_pref_bus,
                        "modePrefWalk": self.cfg.mc_mode_pref_walk,
                        "modePrefCar": self.cfg.mc_mode_pref_car,
                        "modePrefBicycle": self.cfg.mc_mode_pref_bicycle,
                        "modePrefMotorcycle": self.cfg.mc_mode_pref_motorcycle,
                        "maxAccessDistanceM": self.cfg.mc_max_access_distance_m,
                        "maxWalkDistanceM": self.cfg.mc_max_walk_distance_m,
                        "betaTravelTime": self.cfg.mc_beta_travel_time,
                        "walkingSpeedMs": self.cfg.mc_walking_speed_ms,
                        "avgBusSpeedMs": self.cfg.mc_avg_bus_speed_ms,
                        "avgSubwaySpeedMs": self.cfg.mc_avg_subway_speed_ms,
                        "avgBicycleSpeedMs": self.cfg.mc_avg_bicycle_speed_ms,
                        "avgMotorcycleSpeedMs": self.cfg.mc_avg_motorcycle_speed_ms,
                        "avgCarSpeedMs": self.cfg.mc_avg_car_speed_ms,
                    },
                },
            },
            "dependencies": {},
        }
        self._writers["persons"].add(person)
        self.stats["persons_emitted"] += 1

    # ------------------------------------------------------------------
    def _build_leg_element(self, matsim_mode: str, origin_node: str, dest_node: str,
                           vehicle_actors: Dict[str, int], rng: random.Random) -> Dict:
        """Emit a native private/walk leg after the caller applied its mode policy."""
        concrete = self.MODE_TO_CONCRETE.get(matsim_mode)
        vh_mode = self.CONCRETE_TO_VEHICLE_MODE.get(concrete)
        if vh_mode is not None and vh_mode in vehicle_actors:
            return {
                "kind": "PrivateVehicleLeg",
                "mode": concrete,
                "vehicle": {"id": vehicle_actors[vh_mode],
                            "classType": self.VEHICLE_MODES[vh_mode][0]},
                "driverAttributes": {
                    "aggressiveness": rng.uniform(self.cfg.driver_aggressiveness_min,
                                                  self.cfg.driver_aggressiveness_max),
                    "maxSpeedFactor": rng.uniform(self.cfg.driver_max_speed_factor_min,
                                                  self.cfg.driver_max_speed_factor_max),
                    "reactionTime": rng.uniform(self.cfg.driver_reaction_time_min,
                                                self.cfg.driver_reaction_time_max),
                    "minGapFactor": rng.uniform(self.cfg.driver_min_gap_factor_min,
                                                self.cfg.driver_min_gap_factor_max),
                },
            }
        if matsim_mode != "walk":
            self.stats["trips_mode_fallback_walk"] += 1
        return {
            "kind": "WalkLeg",
            "originNodeId": self.ids.id_for("node", origin_node),
            "destinationNodeId": self.ids.id_for("node", dest_node),
            "precomputedRoute": None,
        }

    # ------------------------------------------------------------------
    def _build_vehicle(self, v_id: int, mode: str, origin_node: str, start_tick: int,
                       rng: random.Random, owner_id=None) -> Dict:
        actor_class, state_type, _ = self.VEHICLE_MODES[mode]
        actor_type = self.VEHICLE_MODE_TO_ACTOR_TYPE[mode]
        node = self.ids.id_for("node", origin_node)
        content = {
            "startTick": start_tick,
            "scheduleOnTimeManager": False,
            "movableStatus": "Parked",
            "movableCurrentNode": node,
            "origin": node,
            "destination": node,
            "actorType": actor_type,
            "size": self.VEHICLE_SIZE.get(mode, 4.5),
            "currentSimulationMode": "MESO",
            "microState": None,
            "distance": 0.0,
        }
        # CarState has precomputedRoute; BicycleState/MotorcycleState do not.
        if actor_type == "Car":
            content["precomputedRoute"] = None
        dependencies = {
            "from_node": {"id": node, "classType": "hybrid.actor.Node"},
            "to_node": {"id": node, "classType": "hybrid.actor.Node"},
        }
        if owner_id is not None and self.cfg.declare_ownership:
            dependencies["owner"] = {"id": owner_id, "classType": "hybrid.actor.Person",
                                     "relation": "part_of"}
        return {"id": v_id, "typeActor": actor_class,
                "data": {"dataType": state_type, "content": content},
                "dependencies": dependencies}


class BusRoadRouter:
    """Offline routing through directed motor-road links, returning HTC compact routes
    ([[linkId, nodeId], ...]). Identical to ../sao-paulo's BusRoadRouter: it reads the already
    written links_*.json / nodes_*.json, restricted to bus_routing_links.json."""

    def __init__(self, data_dir: Path):
        path = data_dir / "bus_routing_links.json"
        if not path.exists():
            raise ValueError("Bus routing needs bus_routing_links.json; regenerate the network")
        eligible = set(json.loads(path.read_text()))
        self.graph = defaultdict(list)
        for file in sorted(data_dir.glob("links_*.json")):
            for actor in json.loads(file.read_text()):
                if actor["id"] in eligible:
                    c = actor["data"]["content"]
                    self.graph[c["from"]].append((c["to"], actor["id"], c["length"]))
        # Node coordinates give A* an admissible straight-line bound.
        self._coords: Dict[int, Tuple[float, float]] = {}
        for file in sorted(data_dir.glob("nodes_*.json")):
            for actor in json.loads(file.read_text()):
                c = actor["data"]["content"]
                self._coords[actor["id"]] = (c["latitude"], c["longitude"])
        self._cache: Dict[Tuple[int, int], Optional[List[List[int]]]] = {}

    # Metres per degree of latitude, shaved so the bound never overestimates road length.
    _METRES_PER_DEG = 111_000.0 * 0.99

    def _bound(self, a: int, b: int) -> float:
        pa, pb = self._coords.get(a), self._coords.get(b)
        if pa is None or pb is None:
            return 0.0
        dy = (pa[0] - pb[0]) * self._METRES_PER_DEG
        dx = (pa[1] - pb[1]) * self._METRES_PER_DEG * math.cos(math.radians(pa[0]))
        return math.hypot(dx, dy)

    def route(self, source: int, target: int) -> List[List[int]]:
        if source == target:
            return []
        key = (source, target)
        if key not in self._cache:
            self._cache[key] = self._search(source, target)
        result = self._cache[key]
        if result is None:
            raise ValueError(f"No directed motor-road path between bus stops {source} and {target}")
        return result

    def _search(self, source: int, target: int) -> Optional[List[List[int]]]:
        queue, costs, prev = [(self._bound(source, target), 0.0, source)], {source: 0.0}, {}
        while queue:
            _, cost, node = heapq.heappop(queue)
            if cost != costs[node]:
                continue
            if node == target:
                result = []
                while node != source:
                    parent, link = prev[node]
                    result.append([link, node])
                    node = parent
                return list(reversed(result))
            for dest, link, distance in self.graph.get(node, ()):
                new = cost + distance
                if new < costs.get(dest, float("inf")):
                    costs[dest], prev[dest] = new, (node, link)
                    heapq.heappush(queue, (new + self._bound(dest, target), new, dest))
        return None


# ============================================================================
# TRANSIT CONVERTER  (bus mode only, see README "Transit scope")
# ============================================================================


class TransitConverter:
    """
    Converts the native MATSim transitSchedule_v2 (NOT GTFS) into the same bus model the
    sao-paulo GTFS converter emits, so both cities load identically in the engine:

      - BusStop actors (one per physical stopFacility used by a converted route), shared by every
        route that serves it.
      - Bus actors (one per scheduled <departure>, earliest ``max_buses_per_route`` per route),
        each carrying its ordered stop map and a precomputed closed road path
        (``storedBestRoute``: stop_0 -> ... -> stop_n -> stop_0) found by BusRoadRouter. Buses
        activate at their departure time and then circulate until the horizon; arrival
        adherence to the schedule and trip-end retirement are not modelled.
      - transit_map.json (TransitStop index) and transit_routes.json (RAPTOR routes with
        per-hop travel times from the routeProfile arrival offsets).

    Bus routes are converted here; rail/subway/tram profiles are collected for
    RailScheduleConverter when rail is enabled.
    """

    def __init__(self, cfg: Config, net: MatsimNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.net = net
        self.data_dir = data_dir
        self.ids = ids
        self.stats: Dict[str, int] = defaultdict(int)
        self._stop_writer = StreamingWriter(data_dir, "bus_stops", cfg.items_per_file)
        self._bus_writer = StreamingWriter(data_dir, "buses", cfg.items_per_file)
        self._emitted_stops: Set[str] = set()
        self._stop_node: Dict[str, str] = {}
        self._stop_actor_id: Dict[str, int] = {}
        self._stop_latlon: Dict[str, Tuple[float, float]] = {}
        self._stop_lines: Dict[str, Set[str]] = defaultdict(set)
        self._route_data: List[Dict] = []
        self._labels: Set[str] = set()
        self._router: Optional[BusRoadRouter] = None
        self.bus_capacity = 80
        self.bus_size = 12.0
        self.bus_ports = 2
        # vehicleType id -> passenger capacity (seats + standing); rail routes pick theirs by mode.
        self.vehicle_capacity: Dict[str, int] = {}
        # Rail/subway/tram transitRoutes and all stop positions, handed to RailScheduleConverter.
        self.rail_routes: List[Dict] = []
        self.stop_xy: Dict[str, Tuple[float, float]] = {}
        self.stop_name: Dict[str, str] = {}

    # ------------------------------------------------------------------
    def _load_bus_vehicle_type(self, vehicles_file: Optional[Path]) -> None:
        if vehicles_file is None or not vehicles_file.exists():
            return
        try:
            fh = gzip.open(vehicles_file, "rb") if vehicles_file.suffix == ".gz" else open(vehicles_file, "rb")
            with fh:
                for _, elem in ET.iterparse(fh, events=("end",)):
                    if not elem.tag.endswith("vehicleType"):
                        continue
                    capacity, length = 0, None
                    for child in elem:
                        if child.tag.endswith("capacity"):
                            capacity = int(float(child.get("seats") or 0)) + \
                                int(float(child.get("standingRoomInPersons") or 0))
                        elif child.tag.endswith("length") and child.get("meter"):
                            length = float(child.get("meter"))
                    if capacity > 0:
                        self.vehicle_capacity[elem.get("id")] = capacity
                    if elem.get("id") == "Bus":
                        if capacity > 0:
                            self.bus_capacity = capacity
                        if length:
                            self.bus_size = length
        except (OSError, ET.ParseError, ValueError) as exc:  # metadata only: keep defaults, say so
            self.stats["vehicle_type_read_failed"] += 1
            print(f"  [TRANSIT] warning: could not read vehicle capacity ({exc}); using defaults")

    # ------------------------------------------------------------------
    def convert(self, schedule_file: Path, vehicles_file: Optional[Path]) -> None:
        self._load_bus_vehicle_type(vehicles_file)
        print(f"  [TRANSIT] streaming transit schedule: {schedule_file.name}")

        stop_xy: Dict[str, Tuple[float, float]] = {}
        routes_by_mode: Dict[str, int] = defaultdict(int)
        cap = self.cfg.max_transit_routes

        fh = gzip.open(schedule_file, "rb") if schedule_file.suffix == ".gz" else open(schedule_file, "rb")
        with fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            root: Optional[ET.Element] = None
            in_route = False
            line_id = ""
            route_id = ""
            transport_mode = ""
            profile: List[Dict] = []
            dep_times: List[int] = []

            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem
                    continue
                if event == "end" and elem.tag == "stopFacility":
                    stop_xy[elem.get("id")] = (float(elem.get("x")), float(elem.get("y")))
                    self.stop_name[elem.get("id")] = elem.get("name") or ""
                    continue
                if event == "start" and elem.tag == "transitLine":
                    line_id = elem.get("id")
                    continue
                if event == "start" and elem.tag == "transitRoute":
                    in_route, route_id, transport_mode = True, elem.get("id"), ""
                    profile, dep_times = [], []
                    continue
                if event == "end" and elem.tag == "transportMode" and in_route:
                    transport_mode = (elem.text or "").strip().lower()
                    continue
                if event == "end" and elem.tag == "stop" and in_route:
                    profile.append({"ref": elem.get("refId"),
                                    "arr": elem.get("arrivalOffset"), "dep": elem.get("departureOffset")})
                    continue
                if event == "end" and elem.tag == "departure" and in_route:
                    dep_times.append(_to_tick(elem.get("departureTime")))
                    continue
                if event == "end" and elem.tag == "transitRoute":
                    in_route = False
                    routes_by_mode[transport_mode or "unknown"] += 1
                    if transport_mode == "bus" and self.cfg.include_transit \
                            and (cap == 0 or self.stats["routes_converted"] < cap):
                        self._emit_bus_route(route_id, profile, dep_times, stop_xy)
                    elif transport_mode in ("rail", "subway", "tram") and self.cfg.include_rail:
                        self.rail_routes.append({"line": line_id, "route": route_id, "mode": transport_mode,
                                                 "profile": profile, "departures": dep_times})
                    continue
                if root is not None and elem.tag in ("stopFacility", "transitRoute", "transitLine") \
                        and len(root) > 2_000:
                    root.clear()
            del ctx
        gc.collect()

        for w, key in ((self._stop_writer, "bus_stops"), (self._bus_writer, "buses")):
            r = w.close()
            self.stats[f"files_{key}"] = r["files"]
            self.stats[key] = r["total"]
        for mode, n in routes_by_mode.items():
            self.stats[f"routes_mode_{mode}"] = n
        self.stop_xy = stop_xy
        if not self.cfg.include_transit:
            return
        print(f"  [TRANSIT] done - {self.stats['bus_stops']:,} bus stops, {self.stats['buses']:,} buses "
              f"from {self.stats['routes_converted']:,} routes (non-bus routes tallied but not "
              f"converted: {sum(n for m, n in routes_by_mode.items() if m != 'bus'):,})")
        if self.stats["routes_converted"] == 0:
            raise ValueError("Transit schedule produced no routed buses; check --max-transit-routes, "
                             "--max-snap-distance and the schedule")

    # ------------------------------------------------------------------
    def _emit_bus_route(self, route_id: str, profile: List[Dict], dep_times: List[int],
                        stop_xy: Dict[str, Tuple[float, float]]) -> bool:
        # Snap each stop to the nearest road node from its own x/y (the linkRefId often points at
        # pseudo pt-only links that are not part of the road graph).
        seq: List[Tuple[str, str, Dict]] = []  # (stopFacility id, node id, profile row)
        for row in profile:
            xy = stop_xy.get(row["ref"])
            node = self.net.nearest_node(*xy) if xy is not None else None
            if node is None:
                # Keep the pattern whole or reject it: silently dropping a stop would shortcut it.
                self.stats["routes_skipped_unsnapped_stop"] += 1
                return False
            seq.append((row["ref"], node, row))
        # Consecutive stops on the same node collapse into one; a stop revisited later (loop
        # route) cannot be represented by BusState's stop map.
        collapsed: List[Tuple[str, str, Dict]] = []
        for item in seq:
            if not collapsed or collapsed[-1][1] != item[1]:
                collapsed.append(item)
        if len({sid for sid, _, _ in collapsed}) != len(collapsed):
            self.stats["routes_skipped_repeated_stop"] += 1
            return False
        if len(collapsed) < 2:
            self.stats["routes_skipped_too_few_stops"] += 1
            return False

        deps = sorted(t for t in dep_times if 0 <= t <= self.cfg.duration)
        if self.cfg.max_buses_per_route:
            deps = deps[:self.cfg.max_buses_per_route]
        if not deps:
            self.stats["routes_skipped_no_departures"] += 1
            return False

        stops = {self.ids.id_for("busstop", sid): self.ids.id_for("node", node) for sid, node, _ in collapsed}
        nodes = list(stops.values())
        if self._router is None:
            self._router = BusRoadRouter(self.data_dir)
        route: List[List[int]] = []
        try:
            for source, target in zip(nodes, nodes[1:]):
                route.extend(self._router.route(source, target))
            route.extend(self._router.route(nodes[-1], nodes[0]))
        except ValueError:
            self.stats["routes_skipped_no_motor_route"] += 1
            return False

        label = route_id.replace(";", "_").replace(":", "_")
        if label in self._labels:
            label = f"{label}_{len(self._labels)}"
        self._labels.add(label)

        for sid, node, _ in collapsed:
            self._stop_lines[sid].add(label)
            if sid in self._emitted_stops:
                continue
            self._emitted_stops.add(sid)
            actor_id = self.ids.id_for("busstop", sid)
            node_id = self.ids.id_for("node", node)
            self._stop_node[sid] = node
            self._stop_actor_id[sid] = actor_id
            self._stop_latlon[sid] = _projected_to_wgs84(*stop_xy[sid])
            self._stop_writer.add({
                "id": actor_id,
                "typeActor": "hybrid.actor.BusStop",
                "data": {"dataType": "model.hybrid.entity.state.BusStopState", "content": {
                    "nodeId": node_id, "label": label, "people": {}, "scheduleOnTimeManager": False}},
                "dependencies": {"node": {"id": node_id, "classType": "hybrid.actor.Node"}},
            })

        for k, start in enumerate(deps):
            self._bus_writer.add({
                "id": self.ids.id_for("bus", f"{label}_{start}_{k}"),
                "typeActor": "hybrid.actor.Bus",
                "data": {"dataType": "model.hybrid.entity.state.BusState", "content": {
                    "startTick": start, "label": label, "capacity": self.bus_capacity,
                    "size": self.bus_size, "numberOfPorts": self.bus_ports,
                    "origin": nodes[0], "destination": nodes[-1], "busStops": stops, "people": {},
                    "storedBestRoute": route, "currentSimulationMode": "MESO", "microState": None}},
                "dependencies": {"from_node": {"id": nodes[0], "classType": "hybrid.actor.Node"},
                                 "to_node": {"id": nodes[-1], "classType": "hybrid.actor.Node"}},
            })

        valid = sorted(t for t in dep_times if t >= 0)
        gaps = [b - a for a, b in zip(valid, valid[1:]) if b > a]
        headway = max(60, int(statistics.median(gaps)) if gaps else self.cfg.transit_default_headway)

        # Per-hop travel time from the routeProfile arrival offsets (monotone, unlike the
        # departure offsets which the schedule resets on the last stop).
        hops, prev = [], None
        for sid, _, row in collapsed:
            offset = row["arr"] or row["dep"]
            secs = _to_tick(offset) if offset else None
            hops.append({"stopId": self.ids.id_for("stop", f"bus_{sid}"),
                         "travelTimeFromPrevSeconds":
                             0 if prev is None or secs is None else max(0, secs - prev)})
            prev = secs if secs is not None else prev
        self._route_data.append({"lineId": label, "stopType": "bus",
                                 "headwaySeconds": headway, "stops": hops})
        self.stats["routes_converted"] += 1
        return True

    # ------------------------------------------------------------------
    def transit_route_entries(self) -> List[Dict]:
        """TransitRoute-compatible dicts for RAPTOR, same shape as sao-paulo's."""
        return [r for r in self._route_data if len(r["stops"]) >= 2]

    def transit_map_entries(self) -> List[Dict]:
        """TransitStop-compatible dicts for every emitted bus stop, same shape as sao-paulo's."""
        entries = []
        for sid, actor_id in self._stop_actor_id.items():
            lat, lon = self._stop_latlon[sid]
            entries.append({
                "id": self.ids.id_for("stop", f"bus_{sid}"),
                "actorId": actor_id,
                "actorClassType": "hybrid.actor.BusStop",
                "nodeId": self.ids.id_for("node", self._stop_node[sid]),
                "latitude": lat,
                "longitude": lon,
                "stopType": "bus",
                "lines": sorted(self._stop_lines[sid]),
            })
        return entries


# ============================================================================
# RAIL SCHEDULE CONVERTER  (rail / subway / tram transitRoutes)
# ============================================================================


class RailScheduleConverter:
    """
    Turns the schedule's rail / subway / tram <transitRoute>s into the same actors the sao-paulo
    RailTransitConverter emits (SubwayStation with embedded garage trains, RailLink per segment),
    so both cities load identically in the engine.

    Source differences from São Paulo (OSM route relations with a forward and a return relation):
    the MATSim schedule has one <transitRoute> per direction *and* per stopping pattern -- the 39
    train lines of the real file have up to 16 patterns each. HTC models a line as one station
    sequence served in both directions, so per transitLine:

      - routes are grouped by identical stop pattern;
      - the pattern with the most scheduled departures is the line (ties: more stations); every
        other pattern is dropped and counted (``rail_patterns_unused``) -- an explicit
        simplification, not a hidden one;
      - the return direction is the reversed station sequence (SP's fallback when OSM has no
        return relation), so a service that only runs one way in the schedule runs both ways here.

    Stations snap to the nearest road node (within --max-snap-distance), exactly like activities;
    RailLink endpoints are those road nodes. A line with any unsnappable station is dropped whole
    rather than shortcut. Segment length is the straight-line station separation (floor 100 m),
    not the track geometry. Line speed comes from the schedule's own run time (arrival offsets)
    over that straight-line length, clipped to 3-40 m/s; headway is the median gap between the
    pattern's departures; capacity comes from the Rail/Subway/Tram vehicle type.
    """

    _RAIL_TYPE = {"rail": "HEAVY_RAIL", "subway": "SUBWAY", "tram": "LIGHT_RAIL"}
    _SPEED_KMH = {"rail": 80.0, "subway": 80.0, "tram": 50.0}   # RailLink.speedLimit (km/h)
    _VEHICLE_TYPE = {"rail": "Rail", "subway": "Subway", "tram": "Tram"}
    _MIN_SEGMENT_M = 100.0

    def __init__(self, cfg: Config, net: MatsimNetworkParser, data_dir: Path, ids: ScenarioIdRegistry,
                 routes: List[Dict], stop_xy: Dict[str, Tuple[float, float]],
                 stop_name: Dict[str, str], vehicle_capacity: Dict[str, int]):
        self.cfg = cfg
        self.net = net
        self.data_dir = data_dir
        self.ids = ids
        self.routes = routes
        self.stop_xy = stop_xy
        self.stop_name = stop_name
        self.vehicle_capacity = vehicle_capacity
        self.stats: Dict[str, int] = defaultdict(int)
        self._station_writer = StreamingWriter(data_dir, "subway_stations", cfg.items_per_file)
        self._rail_link_writer = StreamingWriter(data_dir, "rail_links", cfg.items_per_file)
        self._stations: Dict[str, Dict] = {}
        self._lines: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    def convert(self) -> None:
        print(f"  [RAIL] selecting one dominant stop pattern per line ...")
        self._select_lines()
        print(f"        {len(self._lines)} lines, {len(self._stations)} stations")
        self._build_links()
        self._configure_garages()
        self._write_all()
        st = self._station_writer.close()
        rl = self._rail_link_writer.close()
        self.stats["subway_stations"], self.stats["files_subway_stations"] = st["total"], st["files"]
        self.stats["rail_links"], self.stats["files_rail_links"] = rl["total"], rl["files"]
        print(f"  [RAIL] done - {self.stats['subway_stations']:,} stations, "
              f"{self.stats['rail_links']:,} rail links, {self.stats['subway_trains']:,} train definitions")
        if not self.stats["rail_links"]:
            raise ValueError("--rail produced no rail links; check --max-snap-distance and the schedule")

    # ------------------------------------------------------------------
    @staticmethod
    def _slug(text: str) -> str:
        out = "".join(c if c.isalnum() or c in "-_" else "_" for c in text).strip("_")
        return out[:60] or "line"

    def _snap_route(self, route: Dict) -> Optional[List[Dict]]:
        """Stations of one route in order, snapped to road nodes; None if any stop cannot snap."""
        stations: List[Dict] = []
        for row in route["profile"]:
            xy = self.stop_xy.get(row["ref"])
            node = self.net.nearest_node(*xy) if xy is not None else None
            if node is None:
                return None
            key = f"{xy[0]:.0f}_{xy[1]:.0f}"        # one station per physical position
            if stations and (stations[-1]["key"] == key or stations[-1]["node"] == node):
                continue                              # same stop twice / two stops on one node
            offset = row["arr"] or row["dep"]
            stations.append({"key": key, "node": node, "x": xy[0], "y": xy[1],
                             "name": self.stop_name.get(row["ref"]) or key,
                             "arr": _to_tick(offset) if offset else None})
        return stations

    def _select_lines(self) -> None:
        by_line: Dict[Tuple[str, str], Dict[Tuple[str, ...], Dict]] = defaultdict(dict)
        for route in self.routes:
            stations = self._snap_route(route)
            if stations is None:
                self.stats["rail_routes_skipped_unsnapped_stop"] += 1
                self.stats[f"rail_routes_skipped_unsnapped_stop_{route['mode']}"] += 1
                continue
            if len(stations) < 2:
                self.stats["rail_routes_skipped_too_few_stations"] += 1
                continue
            pattern = tuple(s["key"] for s in stations)
            slot = by_line[(route["mode"], route["line"])].setdefault(
                pattern, {"stations": stations, "departures": []})
            slot["departures"] += [t for t in route["departures"] if t >= 0]

        used_keys: Set[str] = set()
        for (mode, line_id), patterns in sorted(by_line.items()):
            best = max(patterns.values(), key=lambda p: (len(p["departures"]), len(p["stations"])))
            self.stats["rail_patterns_unused"] += len(patterns) - 1
            if not best["departures"]:
                self.stats["rail_lines_skipped_no_departures"] += 1
                continue
            key = f"{mode}_{self._slug(line_id)}"
            if key in used_keys:
                key = f"{key}_{len(used_keys)}"
            used_keys.add(key)
            stations = best["stations"]
            deps = sorted(best["departures"])
            gaps = [b - a for a, b in zip(deps, deps[1:]) if b > a]
            interval = max(60, int(statistics.median(gaps))) if gaps else self.cfg.train_interval

            distances = [max(self._MIN_SEGMENT_M, math.hypot(b["x"] - a["x"], b["y"] - a["y"]))
                         for a, b in zip(stations, stations[1:])]
            velocity = self.cfg.train_velocity
            arr = [s["arr"] for s in stations]
            if arr[0] is not None and arr[-1] is not None and len(stations) > 1:
                moving = (arr[-1] - arr[0]) - self.cfg.train_stop_time * (len(stations) - 2)
                if moving > 0:
                    velocity = min(40.0, max(3.0, sum(distances) / moving))
                else:
                    self.stats["rail_lines_speed_fallback"] += 1
            else:
                self.stats["rail_lines_speed_fallback"] += 1
            self._lines[key] = {
                "mode": mode, "stations": stations, "distances": distances, "interval": interval,
                "velocity": velocity,
                "capacity": self.vehicle_capacity.get(self._VEHICLE_TYPE[mode], self.cfg.train_capacity),
                "fwd_links": [], "ret_links": [],
            }
            self.stats[f"lines_{mode}"] += 1
            for st in stations:
                if st["key"] not in self._stations:
                    self._stations[st["key"]] = {
                        "actor_id": self.ids.id_for("subwaystation", f"station_{st['key']}"),
                        "node": st["node"], "name": st["name"], "x": st["x"], "y": st["y"],
                        "terminal": False, "garage": False, "lines": {}, "subways": {}, "linesRoute": {},
                    }
        self.stats["rail_lines"] = len(self._lines)

    # ------------------------------------------------------------------
    def _build_links(self) -> None:
        for key, line in self._lines.items():
            stations, dist = line["stations"], line["distances"]
            for suffix, seq, dseq, bucket in (
                ("segment", stations, dist, "fwd_links"),
                ("return", stations[::-1], dist[::-1], "ret_links"),
            ):
                line[bucket] = [{
                    "link_id": self.ids.id_for("rail_link", f"line_{key}_{suffix}_{i}"),
                    "from_key": a["key"], "to_key": b["key"], "distance": d,
                } for i, (a, b, d) in enumerate(zip(seq, seq[1:], dseq))]
            self._stations[stations[0]["key"]].update(terminal=True, garage=True)
            self._stations[stations[-1]["key"]].update(terminal=True, garage=True)

    def _configure_garages(self) -> None:
        for key, line in self._lines.items():
            n_trains = self.cfg.duration // line["interval"] or 1
            if self.cfg.max_trains_per_line:
                n_trains = min(self.cfg.max_trains_per_line, n_trains)
            for suffix, links, garage_key in (
                ("", line["fwd_links"] + line["ret_links"], line["stations"][0]["key"]),
                ("_ret", line["ret_links"] + line["fwd_links"], line["stations"][-1]["key"]),
            ):
                line_key = key + suffix
                garage = self._stations[garage_key]
                garage["lines"][line_key] = {"interval": line["interval"], "nextTick": 0}
                garage["subways"][line_key] = [{
                    "line": line_key,
                    "actorId": self.ids.id_for("subway", f"{line_key}_train_{t}"),
                    "capacity": line["capacity"],
                    "numberOfPorts": self.cfg.train_number_of_ports,
                    "velocity": line["velocity"],
                    "stopTime": self.cfg.train_stop_time,
                } for t in range(n_trains)]
                self.stats["subway_trains"] += n_trains
                self.stats[f"trains_{line['mode']}"] += n_trains
                garage["linesRoute"][line_key] = [{
                    "stationNode": {"stationId": self._stations[l["to_key"]]["actor_id"],
                                    "nodeId": self.ids.id_for("node", self._stations[l["to_key"]]["node"])},
                    "railLinkId": l["link_id"],
                } for l in links]

    # ------------------------------------------------------------------
    def _write_all(self) -> None:
        for st in self._stations.values():
            node = self.ids.id_for("node", st["node"])
            self._station_writer.add({
                "id": st["actor_id"], "typeActor": "hybrid.actor.SubwayStation",
                "data": {"dataType": "model.hybrid.entity.state.SubwayStationState", "content": {
                    "startTick": 0, "name": st["name"], "nodeId": node,
                    "terminal": st["terminal"], "garage": st["garage"], "lines": st["lines"],
                    "subways": st["subways"], "linesRoute": st["linesRoute"],
                    "people": {}, "status": "Start"}},
                "dependencies": {"node": {"id": node, "classType": "hybrid.actor.Node"}},
            })
        for key, line in self._lines.items():
            rail_type = self._RAIL_TYPE[line["mode"]]
            speed = self._SPEED_KMH[line["mode"]]
            for lnk in line["fwd_links"] + line["ret_links"]:
                a, b = self._stations[lnk["from_key"]], self._stations[lnk["to_key"]]
                na, nb = self.ids.id_for("node", a["node"]), self.ids.id_for("node", b["node"])
                self._rail_link_writer.add({
                    "id": lnk["link_id"], "typeActor": "hybrid.actor.RailLink",
                    "data": {"dataType": "model.hybrid.entity.state.RailLinkState", "content": {
                        "from": na, "to": nb, "length": _sanitize(lnk["distance"], 100.0),
                        "lanes": 2, "speedLimit": speed, "capacity": 10.0, "freeSpeed": speed,
                        "railType": rail_type, "subwayLine": key,
                        "fromStation": a["actor_id"], "toStation": b["actor_id"],
                        "gradient": 0.0, "curvature": 0.0, "simulationMode": "MESO",
                        "laneConfigurations": [], "vehiclesByLane": {},
                        "microTimeStep": self.cfg.micro_time_step,
                        "microTicksPerGlobalTick": self.cfg.micro_ticks_per_tick,
                        "registered": [], "scheduleOnTimeManager": False}},
                    "dependencies": {
                        "from_node": {"id": na, "classType": "hybrid.actor.Node"},
                        "to_node": {"id": nb, "classType": "hybrid.actor.Node"},
                        "from_station": {"id": a["actor_id"], "classType": "hybrid.actor.SubwayStation"},
                        "to_station": {"id": b["actor_id"], "classType": "hybrid.actor.SubwayStation"}},
                })

    # ------------------------------------------------------------------
    def transit_map_entries(self) -> List[Dict]:
        """TransitStop-compatible dicts for every station, same shape as sao-paulo's."""
        lines_at: Dict[str, List[str]] = defaultdict(list)
        for key, line in self._lines.items():
            for st in line["stations"]:
                lines_at[st["key"]] += [key, key + "_ret"]
        entries = []
        for skey, st in self._stations.items():
            lat, lon = _projected_to_wgs84(st["x"], st["y"])
            entries.append({
                "id": self.ids.id_for("stop", f"subway_{skey}"),
                "actorId": st["actor_id"], "actorClassType": "hybrid.actor.SubwayStation",
                "nodeId": self.ids.id_for("node", st["node"]),
                "latitude": lat, "longitude": lon, "stopType": "subway",
                "lines": sorted(set(lines_at[skey])),
            })
        return entries

    def transit_route_entries(self) -> List[Dict]:
        """TransitRoute-compatible dicts for RAPTOR, same shape/formula as sao-paulo's."""
        entries = []
        for key, line in self._lines.items():
            for suffix, links in (("", line["fwd_links"]), ("_ret", line["ret_links"])):
                stops = [{"stopId": self.ids.id_for("stop", f"subway_{links[0]['from_key']}"),
                          "travelTimeFromPrevSeconds": 0}]
                for l in links:
                    stops.append({"stopId": self.ids.id_for("stop", f"subway_{l['to_key']}"),
                                  "travelTimeFromPrevSeconds":
                                      round(l["distance"] / line["velocity"] + self.cfg.train_stop_time)})
                entries.append({"lineId": key + suffix, "stopType": "subway",
                                "headwaySeconds": line["interval"], "stops": stops})
        return entries


# ============================================================================
# MAIN GENERATOR
# ============================================================================


class TrafficSignalGenerator:
    """Match OSM signal locations to MATSim road nodes, then emit HTC actors.

    Multiple OSM stop lines can map to one road node; one controller is emitted
    for that node. Matching never uses the population's much wider snap radius.
    """

    def __init__(self, cfg: Config, net: MatsimNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg, self.net, self.data_dir, self.ids = cfg, net, data_dir, ids
        self.stats: Dict[str, int] = defaultdict(int)

    def generate(self) -> None:
        node_ids = sorted(self.net._node_incoming_links)
        tree = cKDTree([self.net.node_xy[n] for n in node_ids]) if node_ids else None
        project = Transformer.from_crs("EPSG:4326", self.cfg.population_crs, always_xy=True)
        matches: Dict[str, List[str]] = defaultdict(list)
        for osm_id, lat, lon in iter_osm_signals(Path(self.cfg.signal_osm_file)):
            self.stats["osm_signal_nodes"] += 1
            x, y = project.transform(lon, lat)
            if tree is None or not (math.isfinite(x) and math.isfinite(y)):
                self.stats["signals_unmatched"] += 1
                continue
            distance, index = tree.query([x, y], distance_upper_bound=self.cfg.signal_max_snap_distance)
            if not math.isfinite(distance):
                self.stats["signals_unmatched"] += 1
                continue
            node = node_ids[int(index)]
            matches[node].append(osm_id)
            self.stats["osm_signal_nodes_matched"] += 1
        writer = StreamingWriter(self.data_dir, "traffic_signals", self.cfg.items_per_file)
        rng = random.Random(self.cfg.random_seed + 777)
        for node in sorted(matches):
            writer.add(build_signal_actor(self.cfg, self.ids, rng, node, self.net._node_incoming_links[node]))
        result = writer.close()
        self.stats["traffic_signals"], self.stats["files_traffic_signals"] = result["total"], result["files"]
        self.stats["osm_signal_nodes_merged"] = self.stats["osm_signal_nodes_matched"] - result["total"]
        (self.data_dir / "osm_signal_matches.json").write_text(json.dumps(matches, indent=2))
        print(f"  [Signals] {result['total']:,} controllers from {self.stats['osm_signal_nodes']:,} OSM nodes; "
              f"{self.stats['signals_unmatched']:,} outside the matching radius")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NetworkMapCache:
    """Cache the pristine road outputs and the state needed by downstream converters.

    Save before signals/transit/population mutate actors or allocate more IDs. JSON state
    avoids executable serialization, and copies keep every scenario independent.
    """

    def __init__(self, cfg: Config):
        settings = {name: getattr(cfg, name) for name in (
            "population_crs", "random_seed", "micro_ratio", "micro_strategy",
            "micro_time_step", "micro_ticks_per_tick", "items_per_file", "include_signals",
        )}
        self.identity = {
            "version": 1, "network": _file_sha256(cfg.network_file),
            "generator": _file_sha256(Path(__file__)), "settings": settings,
        }
        key = hashlib.sha256(json.dumps(self.identity, sort_keys=True).encode()).hexdigest()
        self.path = (cfg.map_cache_dir or cfg.output_dir.parent / ".toulouse-map-cache") / key

    def restore(self, net: MatsimNetworkParser) -> bool:
        try:
            manifest = json.loads((self.path / "manifest.json").read_text())
            if manifest["identity"] != self.identity:
                return False
            files = manifest["files"]
            if not {"network_state.json", "network_ids.json", "city_map.json",
                    "bus_routing_links.json"}.issubset(files):
                return False
            for name, digest in files.items():
                if Path(name).name != name or _file_sha256(self.path / name) != digest:
                    return False
            state = json.loads((self.path / "network_state.json").read_text())
            for prefix in ("nodes", "links"):
                if any(f"{prefix}_{i}.json" not in files for i in range(state["stats"][f"files_{prefix}"])):
                    return False
            # Validate/decode everything before touching the new scenario.
            ids = ScenarioIdRegistry()
            ids.load(self.path / "network_ids.json")
            node_xy = {k: tuple(v) for k, v in state["node_xy"].items()}
            node_latlon = {k: tuple(v) for k, v in state["node_latlon"].items()}
            kd_ids = state["kd_ids"]
            tree = cKDTree([node_xy[nid] for nid in kd_ids]) if kd_ids else None
            component = set(state["main_component"])
            bus_ids = set(state["bus_link_ids"])
            incoming = defaultdict(list, state["node_incoming_links"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if self.path.exists():
                print(f"  [MAP] cache unavailable or damaged; rebuilding ({exc})")
            return False

        for name in files:
            if name not in ("network_state.json", "network_ids.json"):
                shutil.copyfile(self.path / name, net.data_dir / name)
        net.ids._ids = ids._ids
        net.ids._next = ids._next
        net.node_xy, net.node_latlon = node_xy, node_latlon
        net._kd_ids, net.kdtree = kd_ids, tree
        net.main_component = component
        net._bus_link_ids = bus_ids
        net._node_incoming_links = incoming
        net.stats = defaultdict(int, state["stats"])
        print(f"  [MAP] reused network and city map: {self.path}")
        return True

    def save(self, net: MatsimNetworkParser) -> None:
        state = {
            "node_xy": net.node_xy, "node_latlon": net.node_latlon,
            "kd_ids": net._kd_ids, "main_component": sorted(net.main_component),
            "bus_link_ids": sorted(net._bus_link_ids),
            "node_incoming_links": net._node_incoming_links, "stats": net.stats,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".building-", dir=self.path.parent) as tmp:
                staging = Path(tmp) / "entry"
                staging.mkdir()
                names = ["city_map.json", "bus_routing_links.json"]
                for prefix in ("nodes", "links"):
                    names += [f"{prefix}_{i}.json" for i in range(net.stats[f"files_{prefix}"])]
                for name in names:
                    shutil.copyfile(net.data_dir / name, staging / name)
                (staging / "network_state.json").write_text(json.dumps(state, separators=(",", ":")))
                net.ids.save(staging / "network_ids.json")
                manifest = {"identity": self.identity, "files": {
                    p.name: _file_sha256(p) for p in staging.iterdir()
                }}
                (staging / "manifest.json").write_text(json.dumps(manifest))
                # A previous invalid entry may need replacing. Published entries are
                # complete; an interrupted build remains only in a staging directory.
                if self.path.exists():
                    shutil.rmtree(self.path)
                staging.rename(self.path)
            print(f"  [MAP] cached network and city map: {self.path}")
        except OSError as exc:
            print(f"  [MAP] could not save cache; generation continues ({exc})")


class HybridInputGenerator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stats: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        cfg.validate()
        # Refuse to mix a new run into an old one: stale persons_N.json / buses_N.json from a
        # larger previous run would otherwise be loaded by the engine next to the new files.
        if cfg.output_dir.exists() and any(cfg.output_dir.iterdir()):
            raise FileExistsError(f"Output is not empty: {cfg.output_dir}; use a fresh scenario name")
        for label, fp in (("network", cfg.network_file), ("population", cfg.population_file)):
            if fp is None or not Path(fp).is_file():
                raise FileNotFoundError(f"MATSim {label} file missing: {fp}")
        uses_schedule = cfg.include_transit or cfg.include_rail
        if uses_schedule and (cfg.transit_schedule_file is None
                              or not Path(cfg.transit_schedule_file).is_file()):
            raise FileNotFoundError(f"--transit/--rail need a transit schedule: {cfg.transit_schedule_file}")

        if cfg.include_signals and (cfg.signal_osm_file is None or not Path(cfg.signal_osm_file).is_file()):
            raise FileNotFoundError("--signals requires --signal-osm-file with an OSM XML/XML.gz extract")

        n_steps = 3 + (1 if uses_schedule else 0) + int(cfg.include_signals)
        print("=" * 70)
        print("  MATSim Toulouse -> HTC Hybrid Simulator Input Generator")
        print(f"  Scenario     : {cfg.scenario_name}")
        print(f"  Sample       : {cfg.sample_rate * 100:.3f}%")
        print(f"  Source CRS   : {cfg.population_crs}")
        print(f"  Bus Transit  : {'yes' if cfg.include_transit else 'no'}")
        print(f"  Rail Transit : {'yes' if cfg.include_rail else 'no'}")
        print(f"  OSM Signals  : {'yes' if cfg.include_signals else 'no'}")
        print(f"  Unresolved   : {cfg.unsupported_mode}")
        print(f"  Output       : {cfg.output_dir}")
        print("=" * 70)

        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        data_dir = cfg.output_dir / "data"
        data_dir.mkdir(exist_ok=True)
        ids = ScenarioIdRegistry()

        print(f"\n[1/{n_steps}] Parsing MATSim network ...")
        net = MatsimNetworkParser(cfg, data_dir, ids)
        cache = NetworkMapCache(cfg) if cfg.reuse_map else None
        reused = cache.restore(net) if cache else False
        if not reused:
            net.parse(cfg.network_file)
        ids.save(data_dir / "id_registry.json")
        self.stats.update(net.stats)
        self.stats["network_map_cache_hit"] = int(reused)

        print(f"\n[2/{n_steps}] Building city map ...")
        cm_path = data_dir / "city_map.json"
        if not reused:
            cm = CityMapWriter(cm_path, ids)
            for nid, (lat, lon) in net.node_latlon.items():
                cm.add_node(nid, lat, lon)
            self._add_edges_to_city_map(cm, data_dir, net.stats.get("files_links", 0))
            cm.close()
            if cache:
                cache.save(net)
        print(f"  city_map.json written ({cm_path.stat().st_size // 1024:,} KB)")

        print(f"\n[3/{n_steps}] Converting population ...")
        pop = PopulationConverter(cfg, net, data_dir, ids)
        pop.convert(cfg.population_file)
        self.stats.update(pop.stats)
        self.stats["persons"] = pop.stats.get("persons_emitted", 0)

        if uses_schedule:
            print(f"\n[4/{n_steps}] Converting transit schedule ...")
            transit = TransitConverter(cfg, net, data_dir, ids)
            transit.convert(cfg.transit_schedule_file, cfg.transit_vehicles_file)
            self.stats.update({f"transit_{k}": v for k, v in transit.stats.items()})
            entries: List[Dict] = transit.transit_map_entries() if cfg.include_transit else []
            routes: List[Dict] = transit.transit_route_entries() if cfg.include_transit else []
            if cfg.include_rail:
                rail = RailScheduleConverter(cfg, net, data_dir, ids, transit.rail_routes, transit.stop_xy,
                                             transit.stop_name, transit.vehicle_capacity)
                rail.convert()
                self.stats.update({f"rail_{k}": v for k, v in rail.stats.items()})
                entries += rail.transit_map_entries()
                routes += rail.transit_route_entries()
            with open(data_dir / "transit_map.json", "w") as f:
                json.dump(entries, f, separators=(",", ":"))
            with open(data_dir / "transit_routes.json", "w") as f:
                json.dump(routes, f, separators=(",", ":"))
            self.stats["transit_map_entries"] = len(entries)
            self.stats["transit_route_entries"] = len(routes)
            print(f"  transit_map.json ({len(entries):,} stops), transit_routes.json ({len(routes):,} routes)")

        if cfg.include_signals:
            print(f"\n[{n_steps}/{n_steps}] Extracting OSM traffic signals ...")
            signals = TrafficSignalGenerator(cfg, net, data_dir, ids)
            signals.generate()
            wire_signal_nodes(data_dir)
            self.stats.update({f"signal_{k}": v for k, v in signals.stats.items()})

        ids.save(data_dir / "id_registry.json")
        (cfg.output_dir / "effective_config.json").write_text(json.dumps(asdict(cfg), default=str, indent=2))
        print("  Writing scenario_metadata.json ...")
        self._write_metadata()
        print("  Writing GENERATION_REPORT.md ...")
        self._write_report()

        if cfg.output_format == "sqlite":
            print("\n  Converting data/ actor + city-map JSON to SQLite ...")
            self._convert_data_dir_to_sqlite(data_dir)

        print("\n  Writing simulation.json ...")
        self._write_simulation_json(data_dir)

        print("\n" + "=" * 70)
        print("  Done!")
        self._print_summary()
        print("=" * 70)

    # ------------------------------------------------------------------
    @staticmethod
    def _add_edges_to_city_map(cm: CityMapWriter, data_dir: Path, num_link_files: int) -> None:
        rev_node = {v: k[1] for (k, v) in cm.ids._ids.items() if k[0] == "node"}
        rev_link = {v: k[1] for (k, v) in cm.ids._ids.items() if k[0] == "link"}
        for i in range(num_link_files):
            fp = data_dir / f"links_{i}.json"
            if not fp.exists():
                continue
            with open(fp) as f:
                links = json.load(f)
            for lnk in links:
                c = lnk["data"]["content"]
                lid = rev_link[lnk["id"]]
                from_id = rev_node[c["from"]]
                to_id = rev_node[c["to"]]
                cm.add_edge(lid, from_id, to_id, c["length"], c["speedLimit"])
            del links
            gc.collect()

    # ------------------------------------------------------------------
    _ACTOR_JSON_GLOBS = [
        "nodes_*.json", "links_*.json", "persons_*.json", "cars_*.json",
        "bikes_*.json", "motorcycles_*.json", "bus_stops_*.json", "buses_*.json",
        "subway_stations_*.json", "rail_links_*.json", "traffic_signals_*.json",
    ]

    @classmethod
    def _convert_data_dir_to_sqlite(cls, data_dir: Path) -> None:
        """Convert every data/*.json actor-simulation file (and city_map.json) to the equivalent
        SQLite .db, deleting the source JSON once the conversion succeeds. Reuses
        tools/scenario-db-converter/convert.py -- the same producer SqliteLoadData.scala /
        CityMapUtil.scala read -- instead of re-implementing the JSON->SQLite schema here.
        On failure the .json is left in place (and any partial .db removed)."""
        converter_dir = Path(__file__).resolve().parent.parent / "scenario-db-converter"
        if str(converter_dir) not in sys.path:
            sys.path.insert(0, str(converter_dir))
        from convert import actor_simulation_json_to_db, city_map_json_to_db

        converted = 0
        for pattern in cls._ACTOR_JSON_GLOBS:
            for json_fp in sorted(data_dir.glob(pattern)):
                db_fp = json_fp.with_suffix(".db")
                try:
                    actor_simulation_json_to_db(str(json_fp), str(db_fp))
                except Exception:
                    db_fp.unlink(missing_ok=True)
                    raise
                json_fp.unlink()
                converted += 1

        city_map_json = data_dir / "city_map.json"
        if city_map_json.exists():
            city_map_db = data_dir / "city_map.db"
            try:
                city_map_json_to_db(str(city_map_json), str(city_map_db))
            except Exception:
                city_map_db.unlink(missing_ok=True)
                raise
            city_map_json.unlink()
            converted += 1
        print(f"  Converted {converted} data file(s) to SQLite (.db)")

    # ------------------------------------------------------------------
    def _build_actor_sources(self, data_dir: Path) -> List[Dict]:
        cfg = self.cfg
        base = f"{cfg.docker_base}/{cfg.scenario_name}/data"
        sources: List[Dict] = []
        # DataSourceTypeEnum case names are literally "json"/"sqlite", so output_format doubles
        # as the sourceType value.
        ext = "db" if cfg.output_format == "sqlite" else "json"

        def _static(fid: str, cls: str, base_name: str) -> Dict:
            return {
                "id": fid, "classType": cls,
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": "EAGER",
                "entityLifecycle": "STATIC",
                "dataSource": {"sourceType": cfg.output_format, "info": {"path": f"{base}/{base_name}.{ext}"}},
            }

        def _dynamic(fid: str, cls: str, base_name: str) -> Dict:
            return {
                "id": fid, "classType": cls,
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": "PROGRESSIVE",
                "entityLifecycle": "DYNAMIC",
                "dataSource": {"sourceType": cfg.output_format, "info": {"path": f"{base}/{base_name}.{ext}"}},
            }

        for i in range(self.stats.get("files_nodes", 0)):
            sources.append(_static(f"nodes_{i}", "hybrid.actor.Node", f"nodes_{i}"))
        for i in range(self.stats.get("files_links", 0)):
            sources.append(_static(f"links_{i}", "hybrid.actor.Link", f"links_{i}"))
        for i in range(self.stats.get("files_persons", 0)):
            sources.append(_dynamic(f"persons_{i}", "hybrid.actor.Person", f"persons_{i}"))
        for i in range(self.stats.get("files_cars", 0)):
            sources.append(_dynamic(f"cars_{i}", "hybrid.actor.Car", f"cars_{i}"))
        for i in range(self.stats.get("files_bikes", 0)):
            sources.append(_dynamic(f"bikes_{i}", "hybrid.actor.Bicycle", f"bikes_{i}"))
        for i in range(self.stats.get("files_motorcycles", 0)):
            sources.append(_dynamic(f"motorcycles_{i}", "hybrid.actor.Motorcycle", f"motorcycles_{i}"))
        if cfg.include_transit:
            for i in range(self.stats.get("transit_files_bus_stops", 0)):
                sources.append(_static(f"bus_stops_{i}", "hybrid.actor.BusStop", f"bus_stops_{i}"))
            for i in range(self.stats.get("transit_files_buses", 0)):
                sources.append(_dynamic(f"buses_{i}", "hybrid.actor.Bus", f"buses_{i}"))
        if cfg.include_rail:
            for i in range(self.stats.get("rail_files_subway_stations", 0)):
                sources.append(_static(f"subway_stations_{i}", "hybrid.actor.SubwayStation",
                                       f"subway_stations_{i}"))
            for i in range(self.stats.get("rail_files_rail_links", 0)):
                sources.append(_static(f"rail_links_{i}", "hybrid.actor.RailLink", f"rail_links_{i}"))
        for i in range(self.stats.get("signal_files_traffic_signals", 0)):
            sources.append(_static(f"traffic_signals_{i}", "hybrid.actor.TrafficSignal", f"traffic_signals_{i}"))
        return sources

    # ------------------------------------------------------------------
    @staticmethod
    def _post_load_registration_classes() -> List[str]:
        """Actor classes that must register with other actors after loading (same list as the
        sao-paulo generator, so both scenarios boot identically). BusStop registers with its
        Node; the others are no-ops when a scenario has no such actors."""
        return [
            "org.interscity.htc.model.hybrid.actor.BusStop",
            "org.interscity.htc.model.hybrid.actor.BusStation",
            "org.interscity.htc.model.hybrid.actor.SubwayStation",
        ]

    # ------------------------------------------------------------------
    def _write_simulation_json(self, data_dir: Path) -> None:
        cfg = self.cfg
        base = f"{cfg.docker_base}/{cfg.scenario_name}/data"
        config = {
            "id": cfg.scenario_name,
            "name": f"Toulouse MATSim - {cfg.scenario_name}",
            "description": (
                f"Generated from MATSim Toulouse scenario. Sample: {cfg.sample_rate * 100:.3f}%. "
                f"Bus Transit: {'yes' if cfg.include_transit else 'no'}. "
                f"Rail Transit: {'yes' if cfg.include_rail else 'no'}. "
                f"Unresolved-mode policy: {cfg.unsupported_mode}."
            ),
            "startTick": 0,
            "endTick": cfg.duration,
            "startRealTime": cfg.start_real_time,
            "timeUnit": "seconds",
            "timeStep": 1,
            "duration": cfg.duration,
            "tickDuration": 1.0,
            "randomSeed": cfg.random_seed,
            "enableDynamicModeChoice": cfg.enable_dynamic_mode_choice,
            "dynamicActorTypes": ["hybrid.actor.Subway"] if cfg.include_rail else [],
            "actorsDataSources": self._build_actor_sources(data_dir),
            "cityMapFile": f"{base}/city_map.{'db' if cfg.output_format == 'sqlite' else 'json'}",
            "warmupTargets": ["org.interscity.htc.model.hybrid.util.CityMapUtil#warmUp"],
            "postLoadRegistrationClasses": self._post_load_registration_classes(),
        }
        if (data_dir / "transit_map.json").exists():
            config["transitMapFile"] = f"{base}/transit_map.json"
        if (data_dir / "transit_routes.json").exists():
            config["transitRoutesFile"] = f"{base}/transit_routes.json"
        with open(cfg.output_dir / "simulation.json", "w") as f:
            json.dump(config, f, separators=(",", ":"))

    # ------------------------------------------------------------------
    def _write_metadata(self) -> None:
        cfg = self.cfg
        toulouse_meta = {}
        meta_file = cfg.input_dir / "toulouse_meta.json"
        if meta_file.exists():
            with open(meta_file) as f:
                toulouse_meta = json.load(f)

        used = [cfg.network_file, cfg.population_file]
        if cfg.include_transit or cfg.include_rail:
            used += [cfg.transit_schedule_file, cfg.transit_vehicles_file]
        if cfg.include_signals:
            used.append(cfg.signal_osm_file)
        snapshots = []
        for path in used:
            if path is not None and Path(path).is_file():
                st = Path(path).stat()
                snapshots.append({"path": str(path), "bytes": st.st_size, "mtime_ns": st.st_mtime_ns})

        meta = {
            "inputSnapshots": snapshots,
            "name": cfg.scenario_name,
            "description": "Generated from MATSim Toulouse scenario",
            "generated": datetime.now().isoformat(),
            "version": "2.0.0",
            "source": {
                "directory": str(cfg.input_dir),
                "format": "MATSim native XML (network_v2 + population_v6 + transitSchedule_v2)",
                "populationCrs": cfg.population_crs,
                "toulouseScenarioMeta": toulouse_meta,
            },
            "target": {"directory": str(cfg.output_dir), "model": "Hybrid (MICRO/MESO)"},
            "statistics": {
                k: int(v) if isinstance(v, (int, np.integer)) else v for k, v in self.stats.items()
            },
            "configuration": {
                "sampleRate": cfg.sample_rate,
                "samplingStrategy": cfg.sampling_strategy,
                "populationSize": cfg.population_size,
                "microLinkRatio": cfg.micro_ratio,
                "microStrategy": cfg.micro_strategy,
                "includeTransit": cfg.include_transit,
                "includeRail": cfg.include_rail,
                "includeSignals": cfg.include_signals,
                "signalOsmFile": str(cfg.signal_osm_file) if cfg.include_signals else None,
                "signalMaxSnapDistanceM": cfg.signal_max_snap_distance,
                "signalCycleDuration": cfg.signal_cycle_duration,
                "maxTrainsPerLine": cfg.max_trains_per_line,
                "maxTransitRoutes": cfg.max_transit_routes,
                "maxBusesPerRoute": cfg.max_buses_per_route,
                "itemsPerFile": cfg.items_per_file,
                "randomSeed": cfg.random_seed,
                "unsupportedMode": cfg.unsupported_mode,
                "maxSnapDistanceM": cfg.max_snap_distance,
            },
        }
        with open(cfg.output_dir / "scenario_metadata.json", "w") as f:
            json.dump(meta, f, indent=2)

    # ------------------------------------------------------------------
    def _write_report(self) -> None:
        cfg = self.cfg
        lines = [f"# Generation report: {cfg.scenario_name}", "",
                 f"Source: MATSim native XML (network_v2 + population_v6 + transitSchedule_v2), CRS `{cfg.population_crs}`. "
                 f"Seed: `{cfg.random_seed}`. Sample: `{cfg.sample_rate}`.",
                 f"Clock: one second per tick; inclusive horizon: `{cfg.duration}`.",
                 f"Unresolved mode policy: `{cfg.unsupported_mode}`.",
                 f"Population sampling: `{cfg.sampling_strategy}`. Prefix sampling takes valid persons "
                 "in file order and may bias the sample; random sampling scans the entire source.", "",
                 "## Modeling assumptions", "",
                 "- Native Person plans use Activity, WalkLeg, PrivateVehicleLeg and explicitly requested PendingDecision elements.",
                 "- Only car, bike and walk trips map to native legs. `pt` and `car_passenger` trips follow the unresolved-mode policy above; `walk` turns them into WalkLegs, `replan` into runtime PendingDecisions.",
                 "- Replanning changes the source trip mode/itinerary; it is not MATSim transit-route preservation.",
                 "- Bus (`--transit`) and rail/subway/tram (`--rail`) come from the same MATSim schedule, split by each route's own `<transportMode>`. Rail-only network links are excluded from the road graph.",
                 "- Rail: per transitLine only the stop pattern with the most departures is kept (`rail_patterns_unused` counts the rest); the return direction is its reversed station sequence. Segments are straight-line station separations (floor 100 m), not track geometry; line speed is the schedule run time over that length. Trains are not scaled with the sample.",
                 "- Each scheduled departure becomes one Bus actor (earliest `max_buses_per_route` per route). HTC buses circulate on a closed road path until the horizon; stop-time arrival adherence and trip-end retirement are not implemented by this converter.",
                 "- Bus/train counts are not scaled with the sampled population. Review fleet caps before scientific use.",
                 "- Traffic signal locations come from OSM highway=traffic_signals nodes matched to road nodes within signal_max_snap_distance. Multiple stop lines on one node share a controller; phase timing is synthetic, not an OSM observation.",
                 "- MICRO signal notifications use each incoming-link phase. MESO outgoing gates use the straightest incoming approach; this is an approximation without turn-specific movements.",
                 "- Subway, tram and regional train services use SubwayStation/RailLink actors with SUBWAY/LIGHT_RAIL/HEAVY_RAIL types. Train definitions are embedded in station garages and become Subway actors at runtime.",
                 "- Activities and stops snap to the nearest main-component node within `max_snap_distance`; persons with an unsnappable activity are skipped (`persons_skipped_snap_distance`).",
                 "- The road graph does not model turn restrictions or mode-specific access: snapping uses one directed connected cohort.",
                 "- A successful conversion is not evidence of calibrated traffic behavior or a successful simulator run.", "",
                 "## Counters", "", "| Counter | Value |", "|---|---|"]
        lines.extend(f"| {key} | {value} |" for key, value in sorted(self.stats.items()))
        lines.extend(["", "See `effective_config.json` for resolved options and `scenario_metadata.json` for source information.", ""])
        (cfg.output_dir / "GENERATION_REPORT.md").write_text("\n".join(lines))

    # ------------------------------------------------------------------
    def _print_summary(self) -> None:
        s = self.stats
        print(f"  Network : {s.get('nodes', 0):,} nodes, "
              f"{s.get('micro_links', 0) + s.get('meso_links', 0):,} links")
        print(f"  People  : {s.get('persons_emitted', 0):,} persons, "
              f"{s.get('cars', 0):,} cars, {s.get('bikes', 0):,} bikes")
        if self.cfg.include_transit:
            print(f"  Transit : {s.get('transit_bus_stops', 0):,} stops, {s.get('transit_buses', 0):,} buses")
        if self.cfg.include_rail:
            print(f"  Rail    : {s.get('rail_subway_stations', 0):,} stations, "
                  f"{s.get('rail_rail_links', 0):,} rail links, {s.get('rail_subway_trains', 0):,} trains")
            for mode in ("subway", "tram", "rail"):
                print(f"    {mode:7} : {s.get(f'rail_lines_{mode}', 0):,} lines, "
                      f"{s.get(f'rail_trains_{mode}', 0):,} trains")
        if self.cfg.include_signals:
            print(f"  Signals : {s.get('signal_traffic_signals', 0):,} traffic signals")
        if s.get("transit_map_entries"):
            print(f"  Transit map: {s.get('transit_map_entries', 0):,} stops indexed for mode choice")
        print(f"  Output  : {self.cfg.output_dir}")


# ============================================================================
# CLI
# ============================================================================


def _find_input_file(input_dir: Path, name: str) -> Path:
    gz = input_dir / f"{name}.xml.gz"
    plain = input_dir / f"{name}.xml"
    if gz.exists():
        return gz
    if plain.exists():
        return plain
    raise FileNotFoundError(f"Neither {gz} nor {plain} found")


def _set_source_crs(crs: str) -> None:
    """Rebuild the projected->WGS-84 transformer for a non-default --population-crs."""
    global _T_PROJECTED_TO_WGS84
    _T_PROJECTED_TO_WGS84 = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate HTC hybrid simulator input from the MATSim Toulouse scenario",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test -- 0.1%% sample, network + population only:
  python3 generate_hybrid_input.py --scenario tl_test --sample 0.001 --no-transit --no-rail --unsupported-mode walk

  # 1%% with routed bus transit, unresolved modes replanned at runtime
  python3 generate_hybrid_input.py --scenario tl_1pct --sample 0.01 --transit \\
      --unsupported-mode replan --mc-modes bus walk

  # Every trip decided at runtime by the travel-time engine
  python3 generate_hybrid_input.py --scenario tl_mc --sample 0.01 --transit \\
      --dynamic-mode-choice --mc-strategy travel-time --mc-modes car bicycle bus walk
""",
    )
    p.add_argument("--scenario", required=True, help="Scenario name (output subdirectory)")
    p.add_argument("--input-dir", default=".", help="Directory with toulouse_*.xml.gz [default: .]")
    p.add_argument("--output", default="output", help="Parent output directory [default: ./output]")
    p.add_argument("--output-format", choices=["json", "sqlite"], default="json",
                   help="json: split *_N.json actor files (default). sqlite: convert them to *_N.db "
                        "via tools/scenario-db-converter (requires 'ijson') and delete the JSON.")
    p.add_argument("--sample", type=float, default=0.01, help="Fraction of persons to simulate")
    p.add_argument("--sampling-strategy", choices=["prefix", "random"], default="prefix",
                   help="prefix: stop at generated-person target (file-order bias); "
                        "random: seeded Bernoulli sample over the entire file [default: prefix]")
    p.add_argument("--population-size", type=int,
                   help="Known source person count; avoids the first counting pass for prefix sampling")
    p.add_argument("--reuse-map", action=argparse.BooleanOptionalAction, default=True,
                   help="Reuse unchanged road network and city-map cache [default: on]")
    p.add_argument("--map-cache-dir", type=Path,
                   help="Shared map cache directory [default: OUTPUT/.toulouse-map-cache]")
    p.add_argument("--declare-ownership", action=argparse.BooleanOptionalAction, default=True,
                   help="Write owner/part_of on each private vehicle (partial-simulation filter) [default: on]")
    p.add_argument("--population-crs", default=_SRC_CRS,
                   help="Projected CRS shared by network, plans and transit schedule "
                        f"[default: {_SRC_CRS}; the scenario's own metadata says 'Atlantis']")
    p.add_argument("--unsupported-mode", choices=["error", "walk", "skip-person", "replan"], default="replan",
                   help="Trips whose mode has no native HTC leg (pt, car_passenger, ...): abort, "
                        "walk them, skip the person, or replan at runtime [default: replan]")
    p.add_argument("--max-snap-distance", type=float, default=1000.0,
                   help="Maximum activity/stop snap distance in metres [default: 1000]")
    p.add_argument("--duration", type=int, default=86400, help="Inclusive horizon in seconds [default: 86400]")
    p.add_argument("--start-real-time", default="2025-01-01T00:00:00.000",
                   help="Local simulation origin ISO datetime [default: 2025-01-01T00:00:00.000]")
    p.add_argument("--seed", type=int, default=42, help="Random seed")
    p.add_argument("--items-per-file", type=int, default=50_000)
    p.add_argument("--docker-base", default="/app/hyperbolic-time-chamber/simulations/input")

    p.add_argument("--transit", action=argparse.BooleanOptionalAction, default=True,
                   help="Include bus, subway, tram and regional train transit; use --no-rail for buses only")
    p.add_argument("--max-transit-routes", type=int, default=0,
                   help="Cap on bus transitRoutes converted, file order; 0=all [default: 0]")
    p.add_argument("--max-buses-per-route", type=int, default=0,
                   help="Bus actors per route (earliest departures); 0=all departures [default: 0]")
    p.add_argument("--transit-headway", type=int, default=900,
                   help="Fallback headway (s) when a route has <2 departures [default: 900]")

    p.add_argument("--rail", action=argparse.BooleanOptionalAction, default=None,
                   help="Include rail/subway/tram from the schedule: SubwayStation + RailLink actors, "
                        "one dominant stop pattern per transitLine, served both ways [default: follows --transit]")
    p.add_argument("--max-trains-per-line", type=int, default=0,
                   help="Trains dispatched per line and direction; 0=all within horizon [default: 0]")
    p.add_argument("--train-capacity", type=int, default=1200,
                   help="Fallback passenger capacity when the vehicle type is unknown [default: 1200]")
    p.add_argument("--train-interval", type=int, default=300,
                   help="Fallback headway (s) for lines with < 2 departures [default: 300]")
    p.add_argument("--train-velocity", type=float, default=22.22,
                   help="Fallback train speed in m/s when the schedule gives no run time [default: 22.22]")
    p.add_argument("--train-stop-time", type=int, default=30,
                   help="Dwell time at each station in seconds [default: 30]")

    p.add_argument("--signals", action=argparse.BooleanOptionalAction, default=None,
                   help="Extract traffic lights; enabled automatically when --signal-osm-file is provided")
    p.add_argument("--signal-osm-file", help="OSM XML/XML.gz extract; relative to input-dir")
    p.add_argument("--signal-max-snap-distance", type=float, default=50.0,
                   help="Maximum OSM signal to road-node distance in metres [default: 50]")
    p.add_argument("--signal-cycle", type=int, default=120,
                   help="Synthetic signal cycle in seconds [default: 120]")

    p.add_argument("--micro-ratio", type=float, default=0.0,
                   help="Fraction of links as MICRO [default: 0.0 = full MESO]")
    p.add_argument("--micro-strategy", choices=["random", "highway", "arterial"], default="highway")

    p.add_argument("--driver-aggressiveness-min", type=float, default=0.3)
    p.add_argument("--driver-aggressiveness-max", type=float, default=0.9)
    p.add_argument("--driver-reaction-time-min", type=float, default=0.8)
    p.add_argument("--driver-reaction-time-max", type=float, default=1.2)
    p.add_argument("--driver-speed-factor-min", type=float, default=0.9)
    p.add_argument("--driver-speed-factor-max", type=float, default=1.1)
    p.add_argument("--driver-gap-factor-min", type=float, default=0.8)
    p.add_argument("--driver-gap-factor-max", type=float, default=1.2)

    # -- Dynamic mode choice (flag names identical to the sao-paulo generator) --
    p.add_argument("--dynamic-mode-choice", action=argparse.BooleanOptionalAction, default=False,
                   help="Emit a PendingDecision for every trip, resolved at runtime by --mc-strategy")
    p.add_argument("--mc-strategy", choices=["raptor", "nearest-stop-utility", "travel-time"],
                   default="nearest-stop-utility",
                   help="ModeDecisionEngine id [default: nearest-stop-utility]; only 'travel-time' "
                        "considers car/bicycle/motorcycle")
    p.add_argument("--mc-modes", nargs="+", default=None, metavar="MODE",
                   help="Modes evaluated by the strategy [default: enabled bus/subway modes plus walk]")
    p.add_argument("--mc-beta-mode", type=float, default=1.0)
    p.add_argument("--mc-beta-access", type=float, default=0.001)
    p.add_argument("--mc-beta-egress", type=float, default=0.001)
    p.add_argument("--mc-beta-private", type=float, default=0.00005)
    p.add_argument("--mc-pref-subway", type=float, default=2.0)
    p.add_argument("--mc-pref-bus", type=float, default=1.0)
    p.add_argument("--mc-pref-walk", type=float, default=0.0)
    p.add_argument("--mc-pref-car", type=float, default=3.0)
    p.add_argument("--mc-pref-bicycle", type=float, default=1.5)
    p.add_argument("--mc-pref-motorcycle", type=float, default=2.0)
    p.add_argument("--mc-max-access-dist", type=float, default=1500.0)
    p.add_argument("--mc-max-walk-dist", type=float, default=2000.0)
    p.add_argument("--mc-beta-travel-time", type=float, default=0.01)
    p.add_argument("--mc-walking-speed", type=float, default=1.4)
    p.add_argument("--mc-avg-bus-speed", type=float, default=8.33)
    p.add_argument("--mc-avg-subway-speed", type=float, default=12.5)
    p.add_argument("--mc-avg-bicycle-speed", type=float, default=5.56)
    p.add_argument("--mc-avg-motorcycle-speed", type=float, default=11.11)
    p.add_argument("--mc-avg-car-speed", type=float, default=13.89)

    p.add_argument("--network-file", default=None, help="Override path to network XML(.gz)")
    p.add_argument("--population-file", default=None, help="Override path to population XML(.gz)")
    p.add_argument("--transit-schedule-file", default=None)
    p.add_argument("--transit-vehicles-file", default=None)

    args = p.parse_args()
    if args.signals is None:
        args.signals = bool(args.signal_osm_file)
    if args.rail is None:
        args.rail = args.transit
    if args.mc_modes is None:
        args.mc_modes = (["bus"] if args.transit else []) + (["subway"] if args.rail else []) + ["walk"]

    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output).resolve() / args.scenario

    if not input_dir.exists():
        print(f"ERROR: input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)
    if not 0 < args.sample <= 1.0:
        print("ERROR: --sample must be in (0, 1]", file=sys.stderr)
        sys.exit(1)
    try:
        CRS.from_user_input(args.population_crs)
    except Exception as exc:
        print(f"ERROR: invalid --population-crs: {exc}", file=sys.stderr)
        sys.exit(1)
    _set_source_crs(args.population_crs)

    try:
        network_file = Path(args.network_file) if args.network_file else _find_input_file(
            input_dir, "toulouse_network")
        population_file = Path(args.population_file) if args.population_file else _find_input_file(
            input_dir, "toulouse_population")
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    transit_schedule_file = None
    transit_vehicles_file = None
    if args.transit or args.rail:
        try:
            transit_schedule_file = (Path(args.transit_schedule_file) if args.transit_schedule_file
                                     else _find_input_file(input_dir, "toulouse_transit_schedule"))
            transit_vehicles_file = (Path(args.transit_vehicles_file) if args.transit_vehicles_file
                                     else _find_input_file(input_dir, "toulouse_transit_vehicles"))
        except FileNotFoundError as exc:
            print(f"ERROR: --transit/--rail require the transit schedule/vehicles files: {exc}", file=sys.stderr)
            sys.exit(1)

    cfg = Config(
        input_dir=input_dir,
        output_dir=output_dir,
        scenario_name=args.scenario,
        sample_rate=args.sample,
        sampling_strategy=args.sampling_strategy,
        population_size=args.population_size,
        reuse_map=args.reuse_map,
        map_cache_dir=args.map_cache_dir.resolve() if args.map_cache_dir else None,
        random_seed=args.seed,
        micro_ratio=args.micro_ratio,
        micro_strategy=args.micro_strategy,
        include_transit=args.transit,
        include_rail=args.rail,
        include_signals=args.signals,
        signal_osm_file=((input_dir / args.signal_osm_file).resolve() if args.signal_osm_file else None),
        signal_max_snap_distance=args.signal_max_snap_distance,
        signal_cycle_duration=args.signal_cycle,
        max_trains_per_line=args.max_trains_per_line,
        train_capacity=args.train_capacity,
        train_interval=args.train_interval,
        train_velocity=args.train_velocity,
        train_stop_time=args.train_stop_time,
        max_transit_routes=args.max_transit_routes,
        max_buses_per_route=args.max_buses_per_route,
        transit_default_headway=args.transit_headway,
        items_per_file=args.items_per_file,
        docker_base=args.docker_base,
        output_format=args.output_format,
        duration=args.duration,
        start_real_time=args.start_real_time,
        population_crs=args.population_crs,
        unsupported_mode=args.unsupported_mode,
        max_snap_distance=args.max_snap_distance,
        driver_aggressiveness_min=args.driver_aggressiveness_min,
        driver_aggressiveness_max=args.driver_aggressiveness_max,
        driver_reaction_time_min=args.driver_reaction_time_min,
        driver_reaction_time_max=args.driver_reaction_time_max,
        driver_max_speed_factor_min=args.driver_speed_factor_min,
        driver_max_speed_factor_max=args.driver_speed_factor_max,
        driver_min_gap_factor_min=args.driver_gap_factor_min,
        driver_min_gap_factor_max=args.driver_gap_factor_max,
        enable_dynamic_mode_choice=args.dynamic_mode_choice,
        mode_choice_strategy=args.mc_strategy,
        mc_included_modes=args.mc_modes,
        mc_beta_mode=args.mc_beta_mode,
        mc_beta_access=args.mc_beta_access,
        mc_beta_egress=args.mc_beta_egress,
        mc_beta_private_vehicle=args.mc_beta_private,
        mc_mode_pref_subway=args.mc_pref_subway,
        mc_mode_pref_bus=args.mc_pref_bus,
        mc_mode_pref_walk=args.mc_pref_walk,
        mc_mode_pref_car=args.mc_pref_car,
        mc_mode_pref_bicycle=args.mc_pref_bicycle,
        mc_mode_pref_motorcycle=args.mc_pref_motorcycle,
        mc_max_access_distance_m=args.mc_max_access_dist,
        mc_max_walk_distance_m=args.mc_max_walk_dist,
        mc_beta_travel_time=args.mc_beta_travel_time,
        mc_walking_speed_ms=args.mc_walking_speed,
        mc_avg_bus_speed_ms=args.mc_avg_bus_speed,
        mc_avg_subway_speed_ms=args.mc_avg_subway_speed,
        mc_avg_bicycle_speed_ms=args.mc_avg_bicycle_speed,
        mc_avg_motorcycle_speed_ms=args.mc_avg_motorcycle_speed,
        mc_avg_car_speed_ms=args.mc_avg_car_speed,
        network_file=network_file,
        population_file=population_file,
        transit_schedule_file=transit_schedule_file,
        transit_vehicles_file=transit_vehicles_file,
        declare_ownership=args.declare_ownership,
    )

    gen = HybridInputGenerator(cfg)
    try:
        gen.run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        import traceback

        print(f"\nERROR: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
