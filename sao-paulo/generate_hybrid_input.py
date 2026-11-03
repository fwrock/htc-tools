#!/usr/bin/env python3
"""
EQAsim São Paulo → Hybrid Simulator Input Generator

Converts EQAsim São Paulo scenario data to Hyperbolic Time Chamber hybrid
simulator input format.

Data sources (inside --input-dir):
  - sao_paulo.osm.gz (or sao_paulo.osm)   : OSM road network
  - persons.csv                             : synthetic population attributes
  - activities.gpkg                         : activity locations (SQLite/GeoPackage)
  - trips.csv                               : trip modes and times
  - sptrans/  and  emtu/                    : GTFS transit feeds (optional)

Output directory layout:
  <scenario_name>/
  ├── data/
  │   ├── nodes_*.json
  │   ├── links_*.json
  │   ├── persons_*.json
  │   ├── cars_*.json
  │   ├── bikes_*.json          (if bicycle trips exist)
  │   ├── bus_stops_*.json      (if --transit)
  │   ├── buses_*.json          (if --transit)
  │   └── city_map.json
  ├── simulation.json
  ├── scenario_metadata.json
  └── GENERATION_REPORT.md

Usage:
  # 1 % sample, no transit, output inside ./output/
  python generate_hybrid_input.py --scenario sao_paulo_1pct --sample 0.01

  # 5 % sample with transit (SPTrans + EMTU)
  python generate_hybrid_input.py --scenario sao_paulo_5pct --sample 0.05 --transit

  # Custom output location
  python generate_hybrid_input.py --scenario test --sample 0.001 \\
      --output /home/dean/PhD/hyperbolic-time-chamber/simulations/input

  # Full micro-meso split with custom ratio
  python generate_hybrid_input.py --scenario sp_hybrid --sample 0.02 \\
      --micro-ratio 0.3 --micro-strategy highway

Dependencies: Python >= 3.10, pyproj, scipy, numpy (see requirements.txt).
"""

import argparse
import csv
import gc
import gzip
import hashlib
import heapq
import json
import math
import os
import random
import sqlite3
import struct
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import numpy as np
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scenario_common.traffic_signals import build_signal_actor, validate_signal_config

# ============================================================================
# CONSTANTS
# ============================================================================

# WGS84 lat/lon → WGS84 UTM zone 23S (used to store node coords in metres,
# matching the coordinate system used by MATSim-derived scenarios)
_T_WGS84_TO_UTM23S = Transformer.from_crs("EPSG:4326", "EPSG:32723", always_xy=True)

# Speed defaults per OSM highway tag (km/h)
_HIGHWAY_SPEED: Dict[str, float] = {
    "motorway": 110.0, "motorway_link": 80.0,
    "trunk": 90.0,     "trunk_link": 70.0,
    "primary": 60.0,   "primary_link": 50.0,
    "secondary": 50.0, "secondary_link": 40.0,
    "tertiary": 40.0,  "tertiary_link": 30.0,
    "unclassified": 30.0,
    "residential": 30.0,
    "living_street": 10.0,
    "service": 20.0,
    "track": 20.0,
    "path": 10.0,
    "cycleway": 15.0,
    "footway": 5.0,
    "pedestrian": 5.0,
    "steps": 2.0,
}

# Capacity defaults per highway type (veh/h per lane)
_HIGHWAY_CAPACITY: Dict[str, float] = {
    "motorway": 2200.0, "motorway_link": 1500.0,
    "trunk": 1800.0,    "trunk_link": 1500.0,
    "primary": 1500.0,  "primary_link": 1200.0,
    "secondary": 1200.0,"secondary_link": 900.0,
    "tertiary": 900.0,  "tertiary_link": 700.0,
    "unclassified": 600.0,
    "residential": 600.0,
    "living_street": 300.0,
    "service": 400.0,
}

# Highway types that allow car traffic
_CAR_HIGHWAY_TYPES: Set[str] = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "living_street", "service", "road",
}

# Modes allowed per highway type
_HIGHWAY_MODES: Dict[str, List[str]] = {
    "motorway": ["car", "car_passenger"],
    "motorway_link": ["car", "car_passenger"],
    "trunk": ["car", "car_passenger"],
    "trunk_link": ["car", "car_passenger"],
    "cycleway": ["bike"],
    "footway": ["walk"],
    "pedestrian": ["walk"],
    "steps": ["walk"],
}


def _sanitize(v: float, default: float = 0.0, max_v: float = 1e9) -> float:
    if math.isnan(v) or math.isinf(v):
        return default
    return max(-max_v, min(max_v, v))


def _time_str_to_secs(s: str) -> int:
    """Parse HH:MM:SS (possibly > 24h) to total seconds."""
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


def _wgs84_to_utm23s(lat: float, lon: float) -> Tuple[float, float]:
    """Return (easting, northing) in WGS84 UTM zone 23S metres."""
    e, n = _T_WGS84_TO_UTM23S.transform(lon, lat)
    return _sanitize(e, 0.0, 1e7), _sanitize(n, 0.0, 1e7)


# ============================================================================
# GLOBAL ENTITY ID REGISTRY
# ============================================================================

class ScenarioIdRegistry:
    """Assigns a globally-unique `long` id to every scenario entity.

    Replaces the old `htcaid:<type>;<identifier>` string ids (which embedded the
    actor type and were only unique *per type*) with plain, opaque, monotonically
    increasing `long` ids that are unique **across all actor types** (node, link,
    person, car, bike, bus, subway, signal, …). The actor type is no longer encoded
    in the id — it is carried by `typeActor` / `classType` on the envelope.

    `id_for(kind, source_id)` is deterministic: the first request for a given
    `(kind, source_id)` claims the next id, and every later request for that same
    key returns the *same* id. Because all generator stages iterate their inputs in
    a deterministic order, a rerun with the same seed + inputs produces the same ids.
    """

    def __init__(self) -> None:
        self._next: int = 1
        self._ids: Dict[Tuple[str, str], int] = {}

    def id_for(self, kind: str, source_id) -> int:
        """Returns the unique long id for ``(kind, source_id)``, allocating on first use."""
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
        """Persists the mapping so `--skip-network` reruns can rebuild identical ids."""
        path.write_text(json.dumps({
            "next": self._next,
            "ids": {f"{k[0]}:{k[1]}": v for k, v in self._ids.items()},
        }))

    def load(self, path: Path) -> None:
        """Restores the mapping persisted by [[save]]."""
        if not path.exists():
            return
        data = json.loads(path.read_text())
        self._next = data.get("next", 1)
        for key, v in data.get("ids", {}).items():
            kind, source = key.split(":", 1)
            self._ids[(kind, source)] = v


# ============================================================================
# STREAMING FILE WRITER
# ============================================================================

class StreamingWriter:
    """Incrementally writes items into split JSON array files."""

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


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class Config:
    input_dir: Path
    output_dir: Path
    scenario_name: str

    # Population
    sample_rate: float = 0.01        # fraction of persons to simulate
    random_seed: int = 42

    # Hybrid network
    micro_ratio: float = 0.0         # fraction of links as MICRO (0 = full MESO)
    micro_strategy: str = "highway"  # highway | arterial | random
    micro_time_step: float = 0.1     # seconds
    micro_ticks_per_tick: int = 10

    # Transit (bus)
    include_transit: bool = False
    transit_providers: List[str] = field(
        default_factory=lambda: ["sptrans", "emtu"]
    )
    max_buses_per_route: int = 10    # 0 = all trips; >0 = hard cap per GTFS route

    # Traffic signals
    include_signals: bool = False
    signal_cycle_duration: int = 120     # default cycle length in seconds
    signal_green_ratio: float = 0.45     # green fraction per phase (rest = transition)
    signal_yellow_duration: int = 3      # yellow phase in seconds
    signal_all_red_duration: int = 2     # all-red clearance in seconds

    # Rail transit (subway / train / monorail / light_rail from OSM)
    include_rail: bool = False
    rail_types: List[str] = field(
        default_factory=lambda: ["subway", "rail", "light_rail", "monorail"]
    )
    max_trains_per_line: int = 6          # trains dispatched per line per direction (global default)
    train_capacity: int = 1200            # passengers per train
    # Per-line parameter overrides (JSON file).  Keys must match line_key_pattern
    # (i.e. "{route_type}_{ref}", e.g. "monorail_15").  Supported keys:
    #   headway_seconds, capacity, velocity_ms, stop_time_seconds,
    #   max_trains, number_of_ports.
    rail_lines_config_file: Optional[str] = None
    train_velocity: float = 22.22         # m/s (~80 km/h)
    train_stop_time: int = 30             # seconds at each station
    train_interval: int = 300             # seconds between dispatches (headway)
    train_number_of_ports: int = 8        # doors per side
    train_boarding_time: float = 1.5      # seconds per passenger boarding

    # Driver attributes — randomised per vehicle/trip (uniform distribution)
    driver_aggressiveness_min: float = 0.3
    driver_aggressiveness_max: float = 0.9
    driver_reaction_time_min: float = 0.8
    driver_reaction_time_max: float = 1.2
    driver_max_speed_factor_min: float = 0.9
    driver_max_speed_factor_max: float = 1.1
    driver_min_gap_factor_min: float = 0.8
    driver_min_gap_factor_max: float = 1.2

    # Dynamic mode choice
    # When True, every trip becomes a PendingDecision plan element instead of a
    # fixed leg, resolved at runtime by the ModeDecisionEngine named by
    # mode_choice_strategy — instead of the static EQAsim/MATSim mode.
    enable_dynamic_mode_choice: bool = False
    # ModeDecisionEngine id — must match ModeDecisionEngineRegistry exactly:
    # "raptor" | "nearest-stop-utility" | "travel-time".
    mode_choice_strategy: str = "nearest-stop-utility"

    # ModeChoiceWeights — all fields map 1-to-1 to the Scala case class.
    # Ignored when enable_dynamic_mode_choice=False.
    mc_included_modes: List[str] = field(
        default_factory=lambda: ["bus", "subway", "walk"]
    )
    mc_beta_mode: float = 1.0             # scale applied to mode-preference score
    mc_beta_access: float = 0.001         # per-metre penalty for access leg (walk to PT stop)
    mc_beta_egress: float = 0.001         # per-metre penalty for egress leg (PT stop to dest)
    mc_beta_private_vehicle: float = 0.00005  # per-metre penalty for car/bike/moto O→D distance
    mc_mode_pref_subway: float = 2.0      # ASC for subway
    mc_mode_pref_bus: float = 1.0         # ASC for bus
    mc_mode_pref_walk: float = 0.0        # ASC for walking (base reference)
    mc_mode_pref_car: float = 3.0         # ASC for car
    mc_mode_pref_bicycle: float = 1.5     # ASC for bicycle
    mc_mode_pref_motorcycle: float = 2.0  # ASC for motorcycle
    mc_max_access_distance_m: float = 1500.0  # max walk distance to PT stop (m)
    mc_max_walk_distance_m: float = 2000.0    # max O→D distance to offer walk as mode (m)
    # travel-time strategy extras (only used when mode_choice_strategy="travel-time")
    mc_beta_travel_time: float = 0.01         # per-second penalty for travel time
    mc_walking_speed_ms: float = 1.4          # walking speed to convert access dist → time (m/s)
    mc_avg_bus_speed_ms: float = 8.33         # avg in-vehicle bus speed (~30 km/h)
    mc_avg_subway_speed_ms: float = 12.5      # avg subway speed (~45 km/h)
    mc_avg_bicycle_speed_ms: float = 5.56     # avg bicycle speed (~20 km/h)
    mc_avg_motorcycle_speed_ms: float = 11.11 # avg motorcycle speed (~40 km/h)
    mc_avg_car_speed_ms: float = 13.89        # avg car speed (~50 km/h)

    # Spatial bounding-box filter — limits the OSM network and population to a
    # rectangular area.  Format: (lat_min, lon_min, lat_max, lon_max) in WGS-84
    # decimal degrees.  None = no filter (full network).
    # Example for São Paulo city centre: (-23.57, -46.66, -23.53, -46.62)
    bbox: Optional[Tuple[float, float, float, float]] = None

    # File splitting
    items_per_file: int = 50_000

    # Input/output format
    input_format: str = "eqasim"    # eqasim | matsim
    output_format: str = "json"     # json | sqlite — sqlite reuses tools/scenario-db-converter
    matsim_plans_file: Optional[str] = None  # overrides <input_dir>/output_plans.xml(.gz)

    # Simulation time (seconds, default = 24 h)
    duration: int = 86_400
    start_real_time: str = "2025-01-01T00:00:00.000"

    # Docker base path (used in simulation.json path references)
    docker_base: str = "/app/hyperbolic-time-chamber/simulations/input"

    # Skip OSM network re-generation (reuse existing nodes_*.json / links_*.json /
    # city_map.json).  Only population, transit and signal actors are regenerated.
    # Requires a previous full run to have produced the network files.
    skip_network: bool = False

    population_crs: Optional[str] = None  # EQASim: GeoPackage metadata; MATSim: required
    service_date: Optional[str] = None
    unsupported_mode: str = "error"  # error | walk | skip-person | replan
    # Write `"owner": {..., "relation": "part_of"}` on each private vehicle so the partial-simulation filter can
    # keep/remove a person together with their vehicles (see simedape/docs/PARTIAL_SIMULATION.md). Off by default
    # On by default; --no-declare-ownership restores the old output.
    declare_ownership: bool = True
    max_snap_distance: float = 1000.0
    osm_file: Optional[str] = None
    rail_osm_file: Optional[str] = None

    def validate(self) -> None:
        validate_signal_config(self)
        if not self.scenario_name or Path(self.scenario_name).name != self.scenario_name or self.scenario_name in (".", ".."):
            raise ValueError("scenario must be a single directory name")
        if not 0 < self.sample_rate <= 1 or not 0 <= self.micro_ratio <= 1:
            raise ValueError("sample must be in (0,1] and micro-ratio in [0,1]")
        for name in ("items_per_file", "duration", "train_interval", "train_capacity", "signal_cycle_duration"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not math.isfinite(self.max_snap_distance) or self.max_snap_distance <= 0:
            raise ValueError("max-snap-distance must be finite and positive")
        if self.unsupported_mode not in ("error", "walk", "skip-person", "replan"):
            raise ValueError("Invalid unresolved-mode policy")
        if self.input_format not in ("eqasim", "matsim") or self.output_format not in ("json", "sqlite"):
            raise ValueError("Invalid input/output format")
        if self.max_buses_per_route < 0 or self.max_trains_per_line < 0:
            raise ValueError("bus/train fleet caps must be >= 0 (0 means all)")
        datetime.fromisoformat(self.start_real_time)
        if self.bbox and not (-90 <= self.bbox[0] < self.bbox[2] <= 90 and -180 <= self.bbox[1] < self.bbox[3] <= 180):
            raise ValueError("Invalid WGS84 bbox")
        if self.input_format == "matsim" and not self.population_crs:
            raise ValueError("MATSim requires --population-crs from global.coordinateSystem")
        if self.population_crs:
            CRS.from_user_input(self.population_crs)
        if self.include_transit and not self.service_date:
            raise ValueError("--transit requires --service-date YYYY-MM-DD")
        if self.service_date:
            datetime.strptime(self.service_date, "%Y-%m-%d")
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
        for prefix in ("driver_aggressiveness", "driver_reaction_time", "driver_max_speed_factor", "driver_min_gap_factor"):
            lo, hi = getattr(self, prefix + "_min"), getattr(self, prefix + "_max")
            if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi):
                raise ValueError(f"invalid {prefix} range")


# ============================================================================
# OSM NETWORK PARSER  (two-pass streaming)
# ============================================================================

class OsmNetworkParser:
    """
    Two-pass over the OSM XML (or .gz) file:
      Pass 1 – collect highway way metadata and the set of needed node IDs.
      Pass 2 – emit node actors for needed nodes; collect UTM coords for KD-tree.
    Then writes link actors from the buffered way data.
    """

    ROUTABLE_TYPES: Set[str] = _CAR_HIGHWAY_TYPES | {
        "cycleway", "footway", "pedestrian", "path", "track",
        "steps", "living_street",
    }

    def __init__(self, cfg: Config, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.data_dir = data_dir
        self.ids = ids
        self._rng = random.Random(cfg.random_seed)

        # Populated during pass 1
        self._needed_ids: Set[str] = set()   # string node IDs
        self._bus_link_ids: Set[int] = set()
        self._ways: List[Dict] = []          # highway way descriptors

        # Populated during pass 2
        self._node_utm: Dict[str, Tuple[float, float]] = {}  # id → (e, n)
        self._node_latlon: Dict[str, Tuple[float, float]] = {}  # id → (lat, lon)
        self._signal_node_ids: Set[str] = set()  # OSM nodes with highway=traffic_signals

        # KD-tree (built after pass 2)
        self.kdtree: Optional[cKDTree] = None
        self._kd_ids: List[str] = []         # node IDs in KD-tree order

        # Stats
        self.stats: Dict[str, int] = defaultdict(int)

        # Incoming links per node (populated during _write_links for signal nodes)
        self._node_incoming_links: Dict[str, List[str]] = defaultdict(list)

        # Adjacency (directed) for strongly-connected-component analysis
        self._adj_fwd: Dict[str, List[str]] = defaultdict(list)
        self._adj_rev: Dict[str, List[str]] = defaultdict(list)
        self._main_component: Optional[Set[str]] = None  # filled after _write_links

        # Bounding-box filter in UTM-23S coordinates (e_min, n_min, e_max, n_max).
        # Derived from cfg.bbox once so we don't repeat the conversion per node.
        if cfg.bbox is not None:
            lat_min, lon_min, lat_max, lon_max = cfg.bbox
            e_min, n_min = _wgs84_to_utm23s(lat_min, lon_min)
            e_max, n_max = _wgs84_to_utm23s(lat_max, lon_max)
            self._bbox_utm: Optional[Tuple[float, float, float, float]] = (
                min(e_min, e_max), min(n_min, n_max),
                max(e_min, e_max), max(n_min, n_max),
            )
        else:
            self._bbox_utm = None

        # Writers (created inside parse())
        self._node_writer: Optional[StreamingWriter] = None
        self._link_writer: Optional[StreamingWriter] = None

    # ------------------------------------------------------------------
    def parse(self, osm_file: Path) -> None:
        print(f"  [OSM] pass 1/2 – scanning highway ways: {osm_file.name}")
        self._pass1_ways(osm_file)
        print(f"        found {len(self._ways):,} highway ways, "
              f"{len(self._needed_ids):,} unique node refs")

        self._node_writer = StreamingWriter(self.data_dir, "nodes", self.cfg.items_per_file)
        self._link_writer = StreamingWriter(self.data_dir, "links", self.cfg.items_per_file)

        print(f"  [OSM] pass 2/2 – extracting {len(self._needed_ids):,} nodes …")
        self._pass2_nodes(osm_file)

        print(f"  [OSM] writing {len(self._ways):,} links …")
        self._write_links()

        node_r = self._node_writer.close()
        link_r = self._link_writer.close()
        self.stats["nodes"] = node_r["total"]
        self.stats["files_nodes"] = node_r["files"]
        self.stats["links"] = link_r["total"]
        self.stats["files_links"] = link_r["files"]

        # Keep the OSM extraction even when signals are disabled for this run.
        (self.data_dir / "osm_signal_nodes.json").write_text(json.dumps(sorted(self._signal_node_ids)))

        (self.data_dir / "bus_routing_links.json").write_text(json.dumps(sorted(self._bus_link_ids)))
        # Find largest connected component (undirected BFS)
        self._find_main_component()

        # Build KD-tree over main-component nodes only, so nearest_node
        # never returns an isolated node unreachable in the graph.
        mc_ids = [nid for nid in self._kd_ids if nid in self._main_component]
        print(f"  [OSM] building KD-tree over {len(mc_ids):,} main-component nodes "
              f"({len(self._kd_ids) - len(mc_ids):,} isolated nodes excluded) …")
        if mc_ids:
            self._kd_ids = mc_ids
            utm_arr = np.array([self._node_utm[nid] for nid in self._kd_ids])
            self.kdtree = cKDTree(utm_arr)

        # nodes_*.json still contains every parsed node (isolated ones included —
        # they stay useful e.g. as link endpoints for dead-end streets). Persist
        # main-component membership separately so a later --skip-network run can
        # rebuild the exact same filtered KD-tree without re-parsing the OSM file
        # or re-running the BFS.
        with open(self.data_dir / "main_component.json", "w") as f:
            json.dump(sorted(self._main_component), f)

        print(f"  [OSM] done – {self.stats['nodes']:,} nodes, "
              f"{self.stats['links']:,} links "
              f"({self.stats['micro_links']:,} MICRO / {self.stats['meso_links']:,} MESO)")

    # ------------------------------------------------------------------
    def _open_osm(self, osm_file: Path):
        """Return an opened file handle (handles .gz transparently)."""
        if osm_file.suffix == ".gz":
            return gzip.open(osm_file, "rb")
        return open(osm_file, "rb")

    # ------------------------------------------------------------------
    def _pass1_ways(self, osm_file: Path) -> None:
        """
        First pass: collect highway way descriptors and required node IDs.
        Uses start+end events so we can clear() processed children from root
        and avoid unbounded memory growth across the huge nodes section.
        """
        root = None
        count = 0
        last_report = 0
        with self._open_osm(osm_file) as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem  # first start event = the <osm> root
                    continue
                if event != "end":
                    continue

                count += 1
                if count - last_report >= 500_000:
                    print(f"        … {count:,} elements scanned "
                          f"({len(self._ways):,} ways)", end="\r")
                    last_report = count

                if elem.tag == "way":
                    tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                    hw_type = tags.get("highway", "")
                    if hw_type in self.ROUTABLE_TYPES:
                        node_refs = [nd.get("ref") for nd in elem.findall("nd")]
                        if len(node_refs) >= 2:
                            self._ways.append({
                                "id": elem.get("id"),
                                "nodes": node_refs,
                                "highway": hw_type,
                                "oneway": tags.get("oneway", "no"),
                                "maxspeed": tags.get("maxspeed"),
                                "lanes": tags.get("lanes"),
                                "name": tags.get("name", ""),
                                "access": tags.get("access", ""),
                                "surface": tags.get("surface", ""),
                                "tags": tags,
                            })
                            self._needed_ids.update(node_refs)

                # Clear root's accumulated children when we finish a top-level
                # element.  Only do it at "safe" tag boundaries (node / way /
                # relation / bounds) so we never clear an element mid-parse.
                # Batching the clear (len > 10 000) avoids O(n^2) list scans.
                if (root is not None
                        and elem.tag in ("node", "way", "relation", "bounds")
                        and len(root) > 10_000):
                    root.clear()

            del ctx
        gc.collect()
        print(f"        {count:,} elements scanned                    ")

    # ------------------------------------------------------------------
    def _pass2_nodes(self, osm_file: Path) -> None:
        """
        Second pass: extract coordinates for needed highway nodes.
        Stops as soon as the first <way> is encountered (nodes come first
        in OSM XML), using the same root-clearing trick for memory.
        """
        root = None
        count = 0
        processed = 0
        with self._open_osm(osm_file) as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem
                    continue
                if event != "end":
                    continue

                count += 1

                if elem.tag == "node":
                    nid = elem.get("id")
                    if nid in self._needed_ids:
                        lat = float(elem.get("lat", 0))
                        lon = float(elem.get("lon", 0))
                        # Apply bounding-box filter when configured.
                        if self.cfg.bbox is not None:
                            lat_min, lon_min, lat_max, lon_max = self.cfg.bbox
                            if not (lat_min <= lat <= lat_max
                                    and lon_min <= lon <= lon_max):
                                continue
                        e, n = _wgs84_to_utm23s(lat, lon)
                        self._node_utm[nid] = (e, n)
                        self._node_latlon[nid] = (lat, lon)
                        self._kd_ids.append(nid)
                        self._node_writer.add(self._build_node_actor(nid, lat, lon))
                        # Check for traffic signal tag
                        tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                        if tags.get("highway") == "traffic_signals":
                            self._signal_node_ids.add(nid)
                        processed += 1
                        if processed % 100_000 == 0:
                            print(f"        … {processed:,} nodes written", end="\r")
                elif elem.tag == "way":
                    # Ways come after all nodes in OSM XML; once we hit the
                    # first <way>, all needed node coords have been collected.
                    break

                # Same safe clear: only at node/bounds boundaries.
                if (root is not None
                        and elem.tag in ("node", "bounds")
                        and len(root) > 10_000):
                    root.clear()

            del ctx
        gc.collect()
        print(f"        {processed:,} highway nodes extracted          ")

    # ------------------------------------------------------------------
    def _build_node_actor(self, nid: str, lat: float, lon: float) -> Dict:
        return {
            "id": self.ids.id_for("node", nid),
            "typeActor": "hybrid.actor.Node",
            "data": {
                "dataType": "model.hybrid.entity.state.NodeState",
                "content": {
                    "startTick": 0,
                    "latitude": lat,    # WGS-84 decimal degrees
                    "longitude": lon,   # WGS-84 decimal degrees
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
    def _write_links(self) -> None:
        link_serial = 0
        for way in self._ways:
            hw = way["highway"]
            node_refs = way["nodes"]

            speed = self._parse_speed(way)
            capacity = _HIGHWAY_CAPACITY.get(hw, 600.0)
            lanes = self._parse_lanes(way)
            modes = self._infer_modes(hw, way["tags"])
            oneway = way["oneway"] in ("yes", "1", "true", "-1")
            reversed_dir = way["oneway"] == "-1"

            for i in range(len(node_refs) - 1):
                a_id = node_refs[i]
                b_id = node_refs[i + 1]
                if a_id not in self._node_utm or b_id not in self._node_utm:
                    continue

                length = self._calc_length(a_id, b_id)

                # Record directed adjacency for SCC analysis (forward + reverse)
                # (filled per actual directed link below)

                # Forward link
                link_serial += 1
                sim_mode = self._select_mode(hw, speed, capacity)
                fwd_actual_from = a_id if not reversed_dir else b_id
                fwd_actual_to   = b_id if not reversed_dir else a_id
                self._adj_fwd[fwd_actual_from].append(fwd_actual_to)
                self._adj_rev[fwd_actual_to].append(fwd_actual_from)
                fwd = self._build_link_actor(
                    str(link_serial), fwd_actual_from, fwd_actual_to,
                    length, speed, capacity, lanes, modes,
                    hw, sim_mode, way.get("name", "")
                )
                self._link_writer.add(fwd)
                if hw in _CAR_HIGHWAY_TYPES:
                    self._bus_link_ids.add(fwd["id"])
                # Track incoming links for traffic signal nodes
                if fwd_actual_to in self._signal_node_ids:
                    self._node_incoming_links[fwd_actual_to].append(str(link_serial))
                if sim_mode == "MICRO":
                    self.stats["micro_links"] += 1
                else:
                    self.stats["meso_links"] += 1

                # Reverse link (bidirectional roads)
                if not oneway:
                    rev_actual_from = b_id if not reversed_dir else a_id
                    rev_actual_to   = a_id if not reversed_dir else b_id
                    self._adj_fwd[rev_actual_from].append(rev_actual_to)
                    self._adj_rev[rev_actual_to].append(rev_actual_from)
                    link_serial += 1
                    rev_mode = self._select_mode(hw, speed, capacity)
                    rev = self._build_link_actor(
                        str(link_serial), b_id, a_id,
                        length, speed, capacity, lanes, modes,
                        hw, rev_mode, way.get("name", "")
                    )
                    self._link_writer.add(rev)
                    if hw in _CAR_HIGHWAY_TYPES:
                        self._bus_link_ids.add(rev["id"])
                    # Track incoming links for traffic signal nodes
                    if a_id in self._signal_node_ids:
                        self._node_incoming_links[a_id].append(str(link_serial))
                    if rev_mode == "MICRO":
                        self.stats["micro_links"] += 1
                    else:
                        self.stats["meso_links"] += 1

        # self._ways no longer needed – free memory
        self._ways.clear()
        gc.collect()

    # ------------------------------------------------------------------
    def _find_main_component(self) -> None:
        """Approximate the largest strongly connected component (SCC) using
        bidirectional BFS: forward reachability ∩ reverse reachability from a
        high-degree seed node.  This ensures every GTFS stop snapped to a
        main-component node can both *reach* and *be reached from* any other
        such node, preventing dead-end one-way streets from causing routing
        failures."""

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

        # Pick seed: node with highest out-degree (most likely in main SCC)
        if not self._adj_fwd:
            raise ValueError("OSM selection produced no directed road links; check bbox/input")
        seed = max(self._adj_fwd, key=lambda n: len(self._adj_fwd[n]))

        # Forward reachability from seed
        fwd_set = bfs(self._adj_fwd, seed)
        # Reverse reachability from seed (nodes that CAN reach seed)
        rev_set = bfs(self._adj_rev, seed)

        # Nodes in both sets form the main SCC
        self._main_component = fwd_set & rev_set

        all_nodes = set(self._adj_fwd.keys()) | set(self._adj_rev.keys())
        excluded = len(all_nodes) - len(self._main_component)
        self.stats["main_component_nodes"] = len(self._main_component)
        self.stats["isolated_nodes"] = excluded
        # Free adjacency data
        self._adj_fwd.clear()
        self._adj_rev.clear()

    # ------------------------------------------------------------------
    def _build_link_actor(
        self, lid: str, from_id: str, to_id: str,
        length: float, speed: float, capacity: float,
        lanes: int, modes: List[str], hw_type: str,
        sim_mode: str, name: str,
    ) -> Dict:
        lane_cfgs = []
        for lane_i in range(lanes):
            ltype = "normal"
            if lanes >= 3 and lane_i == 0 and "pt" in modes:
                ltype = "bus_lane"
            elif lanes >= 2 and lane_i == lanes - 1 and "bike" in modes:
                ltype = "bike_lane"
            lane_cfgs.append({"laneId": lane_i, "type": ltype,
                               "width": 3.5, "speedLimit": None})
        return {
            "id": self.ids.id_for("link", lid),
            "typeActor": "hybrid.actor.Link",
            "data": {
                "dataType": "model.hybrid.entity.state.LinkState",
                "content": {
                    "startTick": 0,
                    "from": self.ids.id_for("node", from_id),
                    "to": self.ids.id_for("node", to_id),
                    "length": _sanitize(length, 1.0),
                    "lanes": lanes,
                    "speedLimit": speed,
                    "freeSpeed": speed,
                    "capacity": capacity,
                    "simulationMode": sim_mode,
                    "microTimeStep": self.cfg.micro_time_step,
                    "microTicksPerGlobalTick": self.cfg.micro_ticks_per_tick,
                    "laneConfigurations": lane_cfgs,
                    "congestionFactor": 1.0,
                    "currentSpeed": speed,
                    "registered": [],
                    "vehiclesByLane": {},
                },
            },
            "dependencies": {
                "from_node": {"id": self.ids.id_for("node", from_id), "classType": "hybrid.actor.Node"},
                "to_node":   {"id": self.ids.id_for("node", to_id),   "classType": "hybrid.actor.Node"},
            },
        }

    # ------------------------------------------------------------------
    def _calc_length(self, a: str, b: str) -> float:
        """Euclidean distance between two nodes in UTM metres."""
        ae, an = self._node_utm[a]
        be, bn = self._node_utm[b]
        return math.hypot(be - ae, bn - an)

    def _parse_speed(self, way: Dict) -> float:
        raw = way.get("maxspeed") or ""
        try:
            val = float(raw.split()[0])
            if "mph" in raw.lower():
                val *= 1.60934
            return _sanitize(val, 30.0, 200.0)
        except (ValueError, IndexError):
            return _HIGHWAY_SPEED.get(way["highway"], 30.0)

    def _parse_lanes(self, way: Dict) -> int:
        try:
            return max(1, int(str(way.get("lanes") or "1").split(";")[0]))
        except ValueError:
            return 1

    def _infer_modes(self, hw_type: str, tags: Dict) -> List[str]:
        if hw_type in _HIGHWAY_MODES:
            return list(_HIGHWAY_MODES[hw_type])
        modes: List[str] = []
        if hw_type in _CAR_HIGHWAY_TYPES:
            modes += ["car", "car_passenger"]
            if tags.get("bus") in ("yes", "designated") or tags.get("psv") == "yes":
                modes.append("pt")
        if tags.get("bicycle") in ("yes", "designated") or hw_type == "cycleway":
            modes.append("bike")
        if not modes:
            modes = ["walk"]
        return modes

    def _select_mode(self, hw_type: str, speed: float, capacity: float) -> str:
        if self.cfg.micro_ratio <= 0:
            return "MESO"
        if self.cfg.micro_ratio >= 1.0:
            return "MICRO"
        strat = self.cfg.micro_strategy
        if strat == "highway" and hw_type in ("motorway", "trunk"):
            return "MICRO"
        if strat == "arterial" and hw_type in ("primary", "secondary"):
            return "MICRO"
        if self._rng.random() < self.cfg.micro_ratio:
            return "MICRO"
        return "MESO"

    # ------------------------------------------------------------------
    def nearest_node(self, easting: float, northing: float) -> Optional[str]:
        """Return the OSM node ID nearest to the given UTM coords."""
        if self.kdtree is None:
            return None
        if not math.isfinite(easting) or not math.isfinite(northing) or not self.is_in_bbox(easting, northing):
            return None
        distance, idx = self.kdtree.query([easting, northing])
        if distance > self.cfg.max_snap_distance:
            return None
        return self._kd_ids[idx]

    def is_in_bbox(self, easting: float, northing: float) -> bool:
        """True when no bbox is configured or when the UTM coords fall inside it."""
        if self._bbox_utm is None:
            return True
        e_min, n_min, e_max, n_max = self._bbox_utm
        return e_min <= easting <= e_max and n_min <= northing <= n_max

    def free_heavy_data(self) -> None:
        """Release memory-heavy data no longer needed after city_map is built.

        Keeps only _node_utm, kdtree, and _kd_ids (needed for nearest-node lookups).
        """
        self._ways.clear()
        self._needed_ids.clear()
        self._node_latlon.clear()
        if self._node_writer is not None:
            self._node_writer = None
        if self._link_writer is not None:
            self._link_writer = None
        gc.collect()

    # ------------------------------------------------------------------
    @classmethod
    def rebuild_from_output(cls, cfg: "Config", data_dir: Path, ids: ScenarioIdRegistry) -> "OsmNetworkParser":
        """Reconstruct a minimal OsmNetworkParser from already-generated output files.

        Reads nodes_*.json, links_*.json and traffic_signals_*.json to rebuild
        the KD-tree and the data structures needed by population/transit converters
        and (optionally) the traffic signal generator.  No OSM file is read.
        """
        parser = cls.__new__(cls)
        parser.cfg = cfg
        parser.data_dir = data_dir
        parser.ids = ids
        # Reverse maps: long id → source id, rebuilt from the persisted registry.
        rev_node = {v: k[1] for (k, v) in ids._ids.items() if k[0] == "node"}
        rev_link = {v: k[1] for (k, v) in ids._ids.items() if k[0] == "link"}
        parser._needed_ids = set()
        parser._ways = []
        parser._node_utm = {}
        parser._node_latlon = {}
        parser._signal_node_ids = set()
        parser._node_incoming_links = defaultdict(list)
        parser._kd_ids = []
        parser.kdtree = None
        parser.stats = defaultdict(int)
        parser._adj_fwd = defaultdict(list)
        parser._adj_rev = defaultdict(list)
        parser._main_component = None
        # Restore bbox_utm from config (same logic as __init__)
        if cfg.bbox is not None:
            lat_min, lon_min, lat_max, lon_max = cfg.bbox
            e_min, n_min = _wgs84_to_utm23s(lat_min, lon_min)
            e_max, n_max = _wgs84_to_utm23s(lat_max, lon_max)
            parser._bbox_utm = (
                min(e_min, e_max), min(n_min, n_max),
                max(e_min, e_max), max(n_min, n_max),
            )
        else:
            parser._bbox_utm = None
        parser._node_writer = None
        parser._link_writer = None

        # ── Nodes ──
        node_files = sorted(data_dir.glob("nodes_*.json"))
        if not node_files:
            raise FileNotFoundError(
                f"No nodes_*.json found in {data_dir}. "
                "Run without --skip-network first to generate the network."
            )
        for fp in node_files:
            with open(fp) as f:
                for node in json.load(f):
                    nid = rev_node[node["id"]]
                    lat = node["data"]["content"]["latitude"]
                    lon = node["data"]["content"]["longitude"]
                    e, n = _wgs84_to_utm23s(lat, lon)
                    parser._node_utm[nid] = (e, n)
                    parser._node_latlon[nid] = (lat, lon)
                    parser._kd_ids.append(nid)
        parser.stats["files_nodes"] = len(node_files)

        # ── Links — rebuild incoming-link index (needed for signal generator)
        # and directed adjacency (needed for the main-component BFS fallback,
        # see below) ──
        link_files = sorted(data_dir.glob("links_*.json"))
        for fp in link_files:
            with open(fp) as f:
                for lnk in json.load(f):
                    c   = lnk["data"]["content"]
                    lid = rev_link[lnk["id"]]
                    from_nid = rev_node[c["from"]]
                    to_nid = rev_node[c["to"]]
                    parser._node_incoming_links[to_nid].append(lid)
                    parser._adj_fwd[from_nid].append(to_nid)
                    parser._adj_rev[to_nid].append(from_nid)
        parser.stats["files_links"] = len(link_files)

        # ── Main-component filter for the KD-tree — mirrors parse(), so
        # --skip-network never hands nearest_node() an isolated/unreachable
        # node.  Prefer the file persisted by parse() (O(1) load); fall back
        # to re-running the BFS over the adjacency just built from links_*.json
        # for output directories generated before this fix existed.
        main_component_file = data_dir / "main_component.json"
        if main_component_file.exists():
            with open(main_component_file) as f:
                parser._main_component = set(json.load(f))
        elif parser._adj_fwd:
            print("  [OSM] main_component.json not found — recomputing BFS "
                  "from links_*.json …")
            parser._find_main_component()
        else:
            parser._main_component = set(parser._kd_ids)

        mc_ids = [nid for nid in parser._kd_ids if nid in parser._main_component]
        excluded = len(parser._kd_ids) - len(mc_ids)
        if excluded:
            print(f"  [OSM] filtering KD-tree to {len(mc_ids):,} main-component "
                  f"nodes ({excluded:,} isolated nodes excluded) …")
        if mc_ids:
            parser._kd_ids = mc_ids

        if parser._kd_ids:
            coords = np.array([parser._node_utm[nid] for nid in parser._kd_ids])
            parser.kdtree = cKDTree(coords)

        # Prefer the source extraction: actors may never have been generated.
        signal_nodes_file = data_dir / "osm_signal_nodes.json"
        if signal_nodes_file.exists():
            parser._signal_node_ids = set(json.loads(signal_nodes_file.read_text()))
        else:
            # Backwards compatibility for older outputs that already have actors.
            for fp in sorted(data_dir.glob("traffic_signals_*.json")):
                for sig in json.loads(fp.read_text()):
                    content = sig["data"]["content"]
                    for node in content.get("nodes", [content.get("nodeId")]):
                        if node in rev_node:
                            parser._signal_node_ids.add(rev_node[node])
            if cfg.include_signals and not parser._signal_node_ids:
                raise ValueError("--skip-network cannot recover OSM signal locations from this older output; regenerate the network")

        print(
            f"  Rebuilt from output: {len(parser._node_utm):,} nodes, "
            f"{len(link_files)} link files"
        )
        return parser


# ============================================================================
# POPULATION CONVERTER  (eqasim CSVs + GeoPackage, or raw MATSim plans XML)
# ============================================================================

class PopulationConverter:
    """
    Two input paths, selected by cfg.input_format:
      - "eqasim" (default): sampled persons from persons.csv, activity
        locations from activities.gpkg (SQLite), trip modes from trips.csv.
      - "matsim": streams output_plans.xml(.gz) directly (see _convert_matsim).
    Both converge on the same _emit_person() to produce Person and Vehicle
    actors.
    """

    VEHICLE_MODES: Dict[str, Tuple[str, str, str]] = {
        # mode → (actor_class, state_type, vehicle_class_key)
        "car":          ("hybrid.actor.Car",      "model.hybrid.entity.state.CarState",      "car"),
        "bike":         ("hybrid.actor.Bicycle",  "model.hybrid.entity.state.BicycleState",  "bike"),
        "motorcycle":   ("hybrid.actor.Motorcycle","model.hybrid.entity.state.MotorcycleState","motorcycle"),
    }
    VEHICLE_SIZE: Dict[str, float] = {"car": 4.5, "bike": 2.0, "motorcycle": 2.5}

    # EQAsim/MATSim mode string → PersonState.originalPlan's ConcreteMode name
    # (model.hybrid.entity.state.plan.ConcreteMode: Walk|Car|Bicycle|Motorcycle|Bus|Subway).
    # Exact transit conversion needs MATSim facility/line correspondence. Unresolved
    # modes are handled by the explicit policy before any actor is emitted.
    MODE_TO_CONCRETE: Dict[str, str] = {
        "car": "Car", "bike": "Bicycle", "motorcycle": "Motorcycle", "walk": "Walk",
    }
    CONCRETE_TO_VEHICLE_MODE: Dict[str, str] = {
        "Car": "car", "Bicycle": "bike", "Motorcycle": "motorcycle",
    }
    # cfg.mc_included_modes entries (lowercase, EQAsim-style) → ConcreteMode name, for
    # PendingDecision.decision.allowedModes.
    MC_MODE_TO_CONCRETE: Dict[str, str] = {
        "car": "Car", "bicycle": "Bicycle", "motorcycle": "Motorcycle",
        "bus": "Bus", "subway": "Subway", "walk": "Walk",
    }
    # VEHICLE_MODES key → ActorTypeEnum name for that vehicle's own actorType field.
    # NOT mode.capitalize() — "bike".capitalize() == "Bike", but ActorTypeEnum has no
    # "Bike" case (only "Bicycle"), which would fail enum deserialization outright.
    VEHICLE_MODE_TO_ACTOR_TYPE: Dict[str, str] = {
        "car": "Car", "bike": "Bicycle", "motorcycle": "Motorcycle",
    }

    def __init__(self, cfg: Config, osm: OsmNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.osm = osm
        self.data_dir = data_dir
        self.ids = ids
        self._rng = random.Random(cfg.random_seed + 1)

        self._writers: Dict[str, StreamingWriter] = {
            "persons": StreamingWriter(data_dir, "persons", cfg.items_per_file),
            "cars":    StreamingWriter(data_dir, "cars",    cfg.items_per_file),
            "bikes":   StreamingWriter(data_dir, "bikes",   cfg.items_per_file),
            "motorcycles": StreamingWriter(data_dir, "motorcycles", cfg.items_per_file),
        }
        self.stats: Dict[str, int] = defaultdict(int)
        self._population_transform = None

    # ------------------------------------------------------------------
    def convert(self) -> None:
        """Memory-efficient chunked processing.

        Instead of loading ALL activities and trips into memory at once
        (which at 100% sample would require 30+ GB), we:
          1. Sample person IDs (set of ints, ~1 GB at 100%)
          2. Stream trips.csv into a temporary SQLite DB on disk
          3. Process persons in chunks of CHUNK_SIZE:
             - query activities from GeoPackage for chunk
             - query trips from temp SQLite for chunk
             - emit person + vehicle actors
             - free chunk data + gc.collect()
        Peak memory for the population step stays under ~2 GB.
        """
        if self.cfg.input_format == "matsim":
            self._convert_matsim()
            return

        CHUNK_SIZE = 200_000

        inp = self.cfg.input_dir
        persons_csv     = inp / "persons.csv"
        activities_gpkg = inp / "activities.gpkg"
        trips_csv       = inp / "trips.csv"

        for fp in (persons_csv, activities_gpkg, trips_csv):
            if not fp.exists():
                raise FileNotFoundError(f"Required file not found: {fp}")

        # ── 1. Sample person IDs ──
        print("  [POP] sampling persons …")
        sampled_ids = self._sample_persons(persons_csv)
        self.stats["persons_total_in_file"] = self.stats.get("persons_scanned", 0)
        total_sampled = len(sampled_ids)
        print(f"        sampled {total_sampled:,} persons "
              f"({self.cfg.sample_rate * 100:.1f}% of file)")

        # ── 2. Index trips into temp SQLite (streaming, low mem) ──
        print("  [POP] indexing trips into temp DB …")
        trips_db_path = self.data_dir / "_trips_index.sqlite"
        self._index_trips(trips_csv, sampled_ids, trips_db_path)

        # Convert set → sorted list, then free the set
        id_list = sorted(sampled_ids)
        del sampled_ids
        gc.collect()

        # ── 3. Process persons in chunks ──
        print("  [POP] generating person and vehicle actors (chunked) …")
        trips_conn = sqlite3.connect(str(trips_db_path))
        acts_conn  = sqlite3.connect(str(activities_gpkg))
        acts_conn.row_factory = sqlite3.Row
        source_crs = self.cfg.population_crs
        if not source_crs:
            row = acts_conn.execute(
                "SELECT s.definition, s.organization, s.organization_coordsys_id "
                "FROM gpkg_geometry_columns g JOIN gpkg_spatial_ref_sys s ON g.srs_id=s.srs_id "
                "WHERE g.table_name='activities'"
            ).fetchone()
            if row is None:
                raise ValueError("GeoPackage has no activities CRS; supply --population-crs")
            source_crs = f"EPSG:{row[2]}" if row[1].upper() == "EPSG" else row[0]
        self._population_transform = Transformer.from_crs(source_crs, "EPSG:32723", always_xy=True)
        self.stats["population_crs"] = CRS.from_user_input(source_crs).to_string()

        for chunk_start in range(0, len(id_list), CHUNK_SIZE):
            chunk_end = min(chunk_start + CHUNK_SIZE, len(id_list))
            chunk = id_list[chunk_start:chunk_end]
            chunk_set = set(chunk)

            activities = self._load_activities_chunk(acts_conn, chunk_set)
            trips      = self._load_trips_chunk(trips_conn, chunk_set)

            for pid in chunk:
                acts = activities.get(pid, [])
                trps = trips.get(pid, [])
                if acts:
                    self._emit_person(pid, acts, trps)
                else:
                    self.stats["persons_skipped_no_activities"] += 1

            del activities, trips, chunk_set, chunk
            gc.collect()

            print(f"        … {chunk_end:,}/{len(id_list):,} persons "
                  f"({chunk_end * 100 / len(id_list):.0f}%)", end="\r")

        print(f"        {len(id_list):,} persons processed                         ")

        acts_conn.close()
        trips_conn.close()
        trips_db_path.unlink(missing_ok=True)
        del id_list
        gc.collect()

        for w in self._writers.values():
            r = w.close()
            self.stats[f"files_{w.base}"] = r["files"]
            self.stats[w.base] = r["total"]

        print(f"  [POP] done – {self.stats['persons']:,} persons, "
              f"{self.stats['cars']:,} cars, {self.stats['bikes']:,} bikes")

        self._validate_reachability()

    # ------------------------------------------------------------------
    def _resolve_matsim_plans_file(self) -> Path:
        if self.cfg.matsim_plans_file:
            p = Path(self.cfg.matsim_plans_file)
            if not p.is_absolute():
                p = self.cfg.input_dir / p
            candidates = [p]
        else:
            candidates = [
                self.cfg.input_dir / "output_plans.xml.gz",
                self.cfg.input_dir / "output_plans.xml",
            ]
        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(
            f"MATSim plans file not found (tried {[str(c) for c in candidates]}). "
            "Expected output_plans.xml.gz or output_plans.xml in --input-dir, "
            "or pass --matsim-plans explicitly."
        )

    # ------------------------------------------------------------------
    def _convert_matsim(self) -> None:
        plans_path = self._resolve_matsim_plans_file()
        transform = Transformer.from_crs(self.cfg.population_crs, "EPSG:32723", always_xy=True)
        print(f"  [POP] streaming MATSim plans: {plans_path.name}")
        opener = gzip.open if plans_path.suffix == ".gz" else open
        with opener(plans_path, "rb") as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            _, root = next(ctx)
            for event, elem in ctx:
                if event != "end" or elem.tag != "person":
                    continue
                self.stats["persons_scanned"] += 1
                if self.cfg.sample_rate >= 1 or self._rng.random() < self.cfg.sample_rate:
                    plans = elem.findall("plan")
                    selected = [p for p in plans if p.get("selected") == "yes"]
                    if not selected and len(plans) == 1 and plans[0].get("selected") != "no":
                        selected = plans
                    if len(selected) != 1:
                        raise ValueError(f"Person {elem.get('id')}: expected one selected plan")
                    acts, trips = self._matsim_plan(selected[0], transform)
                    self._emit_person(elem.get("id"), acts, trips)
                root.remove(elem)
        self.stats["persons_total_in_file"] = self.stats["persons_scanned"]
        for w in self._writers.values():
            result = w.close()
            self.stats[f"files_{w.base}"] = result["files"]
            self.stats[w.base] = result["total"]
        self._validate_reachability()

    def _matsim_plan(self, plan: ET.Element, transform: Transformer) -> Tuple[List[Dict], List[Dict]]:
        acts, trips, legs = [], [], []
        aliases = {"bicycle": "bike", "transit_walk": "walk", "access_walk": "walk", "egress_walk": "walk"}
        for elem in plan:
            if elem.tag == "leg":
                legs.append(elem)
                continue
            if elem.tag not in ("activity", "act"):
                continue
            if elem.get("type", "").lower().endswith(" interaction"):
                self.stats["matsim_stage_activities_collapsed"] += 1
                continue
            x, y = elem.get("x"), elem.get("y")
            if x is None or y is None:
                raise ValueError("MATSim activity requires x/y; link/facility-only locations need an external lookup")
            e, n = transform.transform(float(x), float(y), errcheck=True)
            end, dur = elem.get("end_time"), elem.get("dur") or elem.get("max_dur")
            if end and dur:
                self.stats["matsim_duration_overridden_by_end_time"] += 1
            if end and end != "undefined":
                spec = {"kind": "AtTick", "tick": self._to_tick(end)}
            elif dur and dur != "undefined":
                spec = {"kind": "Duration", "ticks": self._to_tick(dur)}
            else:
                spec = {"kind": "AtTick", "tick": self.cfg.duration}
                self.stats["matsim_activity_end_fallback_horizon"] += 1
            if acts:
                if not legs:
                    raise ValueError("MATSim plan has consecutive main activities without a leg")
                modes = [aliases.get(l.get("mode", "").lower(), l.get("mode", "").lower()) for l in legs]
                main_modes = set(modes) - {"walk"}
                if len(main_modes) > 1:
                    raise ValueError(f"Mixed-mode MATSim trip needs an explicit mapping: {modes}")
                mode = next(iter(main_modes), "walk")
                departure = legs[0].get("dep_time")
                dep_tick = self._to_tick(departure) if departure and departure != "undefined" else acts[-1]["end_spec"].get("tick", 0)
                if departure and departure != "undefined":
                    acts[-1]["end_spec"] = {"kind": "AtTick", "tick": dep_tick}
                trips.append({"trip_id": len(trips), "mode": mode, "dep_time": dep_tick,
                              "arr_time": 0, "prec_idx": len(acts)-1, "foll_idx": len(acts)})
                legs = []
            acts.append({"activity_id": len(acts), "purpose": elem.get("type", ""),
                         "start_time": None, "end_time": end, "end_spec": spec,
                         "is_last": False, "easting": e, "northing": n})
        if legs:
            raise ValueError("MATSim plan ends with a leg without a destination activity")
        if acts:
            acts[-1]["is_last"] = True
        return acts, trips

    # ------------------------------------------------------------------
    def _validate_reachability(self) -> None:
        """Abort if any activity was snapped to a node outside the road
        network's main component — such trips would have no real route.
        A regression tripwire for the KD-tree filtering in OsmNetworkParser
        (parse() and rebuild_from_output()), not an expected outcome."""
        snapped = self.stats.get("activity_nodes_snapped", 0)
        off_mc = self.stats.get("activity_nodes_off_main_component", 0)
        if snapped == 0 or off_mc == 0:
            return
        frac = off_mc / snapped
        raise RuntimeError(
            f"[POP] reachability check failed: {off_mc:,}/{snapped:,} "
            f"({frac:.4%}) activity nodes fall outside the road network's "
            f"main connected component — trips to/from them have no real "
            f"route. This indicates the KD-tree was not filtered to "
            f"main-component nodes (see OsmNetworkParser.parse() / "
            f"rebuild_from_output())."
        )

    # ------------------------------------------------------------------
    def _sample_persons(self, persons_csv: Path) -> Set[int]:
        sampled: Set[int] = set()
        rng = self._rng
        rate = self.cfg.sample_rate
        scanned = 0
        with open(persons_csv, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                scanned += 1
                if rate >= 1.0 or rng.random() < rate:
                    sampled.add(int(row["person_id"]))
        self.stats["persons_scanned"] = scanned
        return sampled

    # ------------------------------------------------------------------
    def _load_activities_chunk(
        self, conn: sqlite3.Connection, chunk_ids: Set[int]
    ) -> Dict[int, List[Dict]]:
        """Load activities for a chunk of person IDs from the GeoPackage."""
        result: Dict[int, List[Dict]] = defaultdict(list)
        batch_size = 5_000
        id_list = sorted(chunk_ids)

        for start in range(0, len(id_list), batch_size):
            batch = id_list[start: start + batch_size]
            placeholders = ",".join("?" * len(batch))
            sql = (
                f"SELECT person_id, activity_id, purpose, start_time, end_time, "
                f"is_last, geom FROM activities "
                f"WHERE person_id IN ({placeholders}) "
                f"ORDER BY person_id, activity_id"
            )
            cur = conn.execute(sql, batch)
            for row in cur:
                e_str, n_str = self._decode_gpkg_point(row["geom"])
                if e_str is not None:
                    e_str, n_str = self._population_transform.transform(e_str, n_str, errcheck=True)
                result[row["person_id"]].append({
                    "activity_id": row["activity_id"],
                    "purpose":     row["purpose"],
                    "start_time":  row["start_time"],
                    "end_time":    row["end_time"],
                    "is_last":     str(row["is_last"]).lower() in ("1", "true"),
                    "easting":     e_str,
                    "northing":    n_str,
                })

        return dict(result)

    # ------------------------------------------------------------------
    @staticmethod
    def _decode_gpkg_point(blob: bytes) -> Tuple[Optional[float], Optional[float]]:
        """Decode a GeoPackage WKB point geometry blob → (x, y)."""
        if not blob:
            return None, None
        try:
            if blob[:2] != b"GP":
                return None, None
            flags = blob[3]
            if flags & 0x10:
                return None, None
            env_type = (flags >> 1) & 7
            env_sizes = [0, 32, 48, 48, 64]
            env_size = env_sizes[env_type] if env_type < len(env_sizes) else 0
            wkb_off = 8 + env_size
            wkb = blob[wkb_off:]
            byte_order = wkb[0]
            fmt = "<" if byte_order == 1 else ">"
            geometry_type = struct.unpack_from(fmt + "I", wkb, 1)[0]
            if geometry_type not in (1, 1001, 2001, 3001):
                return None, None
            x, y = struct.unpack_from(fmt + "dd", wkb, 5)
            return (x, y) if math.isfinite(x) and math.isfinite(y) else (None, None)
        except Exception:
            return None, None

    # ------------------------------------------------------------------
    def _index_trips(
        self, trips_csv: Path, sampled_ids: Set[int], db_path: Path
    ) -> None:
        """Stream trips.csv into a temporary SQLite DB for chunked access.

        This avoids holding all ~35 M trip dicts in memory.  The SQLite
        file lives on disk and is deleted after processing.
        """
        if db_path.exists():
            db_path.unlink()
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -64000")  # 64 MB page cache
        conn.execute(
            "CREATE TABLE trips ("
            "  person_id INTEGER,"
            "  trip_id   INTEGER,"
            "  mode      TEXT,"
            "  dep_time  INTEGER,"
            "  arr_time  INTEGER,"
            "  prec_idx  INTEGER,"
            "  foll_idx  INTEGER"
            ")"
        )

        sample_all = self.cfg.sample_rate >= 1.0
        inserted = 0
        buf: List[tuple] = []
        with open(trips_csv, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                pid = int(row["person_id"])
                if not sample_all and pid not in sampled_ids:
                    continue
                buf.append((
                    pid,
                    int(row["trip_id"]),
                    row["mode"].strip().lower(),
                    self._to_tick(row.get("departure_time", "")),
                    self._to_tick(row.get("arrival_time", "")),
                    int(row.get("preceeding_activity_index", 0)),
                    int(row.get("following_activity_index", 1)),
                ))
                if len(buf) >= 200_000:
                    conn.executemany(
                        "INSERT INTO trips VALUES (?,?,?,?,?,?,?)", buf
                    )
                    buf.clear()
                    inserted += len(buf) or 200_000  # approximate
                    if inserted % 5_000_000 < 200_000:
                        print(f"        … {inserted:,} trips indexed", end="\r")
        if buf:
            conn.executemany(
                "INSERT INTO trips VALUES (?,?,?,?,?,?,?)", buf
            )
            inserted += len(buf)
            buf.clear()

        conn.execute("CREATE INDEX idx_trips_pid ON trips(person_id)")
        conn.commit()
        total = conn.execute("SELECT count(*) FROM trips").fetchone()[0]
        conn.close()
        gc.collect()
        print(f"        {total:,} trips indexed into temp DB                    ")

    # ------------------------------------------------------------------
    def _load_trips_chunk(
        self, conn: sqlite3.Connection, chunk_ids: Set[int]
    ) -> Dict[int, List[Dict]]:
        """Load trips for a chunk of person IDs from the indexed SQLite DB."""
        result: Dict[int, List[Dict]] = defaultdict(list)
        batch_size = 5_000
        id_list = sorted(chunk_ids)

        for start in range(0, len(id_list), batch_size):
            batch = id_list[start: start + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT person_id, trip_id, mode, dep_time, arr_time, "
                f"prec_idx, foll_idx FROM trips "
                f"WHERE person_id IN ({placeholders}) "
                f"ORDER BY person_id, trip_id",
                batch,
            )
            for row in cur:
                result[row[0]].append({
                    "trip_id":  row[1],
                    "mode":     row[2],
                    "dep_time": row[3],
                    "arr_time": row[4],
                    "prec_idx": row[5],
                    "foll_idx": row[6],
                })

        return dict(result)

    # ------------------------------------------------------------------
    @staticmethod
    def _to_tick(val: str) -> int:
        """Convert float-seconds string or HH:MM:SS to integer tick."""
        if val is None or str(val).strip() in ("", "nan", "None", "undefined"):
            raise ValueError(f"Missing simulation time: {val!r}")
        text = str(val).strip()
        seconds = float(text) if ":" not in text else _time_str_to_secs(text)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"Invalid simulation time: {val!r}")
        return round(seconds)

    # ------------------------------------------------------------------
    def _emit_person(
        self, pid: int, acts: List[Dict], trips: List[Dict]
    ) -> None:
        """Build and write a Person actor + any owned vehicle actors."""
        # Persons with no trips are irrelevant to a mobility simulation.
        if not trips:
            self.stats["persons_skipped_no_trips"] = (
                self.stats.get("persons_skipped_no_trips", 0) + 1
            )
            return

        trips = [dict(t, mode={"bicycle": "bike"}.get(t["mode"], t["mode"])) for t in trips]
        unresolved = [t for t in trips if t["mode"] not in self.MODE_TO_CONCRETE]
        if unresolved and not self.cfg.enable_dynamic_mode_choice:
            self.stats["trips_unresolved_mode"] += len(unresolved)
            if self.cfg.unsupported_mode == "error":
                raise ValueError(f"Person {pid}: unresolved modes {sorted({t['mode'] for t in unresolved})}; "
                                 "choose --unsupported-mode replan, skip-person, or walk explicitly")
            if self.cfg.unsupported_mode == "skip-person":
                self.stats["persons_skipped_unsupported_mode"] += 1
                return
        if not acts or any(a.get("easting") is None or a.get("northing") is None for a in acts):
            self.stats["persons_skipped_invalid_activity"] += 1
            return
        if any(not (0 <= t["prec_idx"] < len(acts) and 0 <= t["foll_idx"] < len(acts)) for t in trips):
            raise ValueError(f"Person {pid}: trip references a missing activity")
        rng = self._rng

        # ── Map each activity to its nearest OSM node ──
        node_ids: List[Optional[str]] = []
        for act in acts:
            e, n = act["easting"], act["northing"]
            if e is None:
                node_ids.append(None)
            else:
                ef, nf = float(e), float(n)
                if not self.osm.is_in_bbox(ef, nf):
                    # Person has at least one activity outside the bbox; skip.
                    self.stats["persons_skipped_outside_bbox"] = (
                        self.stats.get("persons_skipped_outside_bbox", 0) + 1
                    )
                    return
                snapped = self.osm.nearest_node(ef, nf)
                if snapped is None:
                    self.stats["persons_skipped_snap_distance"] += 1
                    return
                node_ids.append(snapped)
                if snapped is not None:
                    self.stats["activity_nodes_snapped"] += 1
                    if (self.osm._main_component is not None
                            and snapped not in self.osm._main_component):
                        # Should never happen — nearest_node()'s KD-tree is
                        # filtered to main-component nodes only (see parse()
                        # and rebuild_from_output()). Counted here as a
                        # regression tripwire, not expected steady-state.
                        self.stats["activity_nodes_off_main_component"] += 1

        # ── Determine required vehicle types ──
        # One vehicle actor per vehicle type used (not per trip)
        vehicle_actors: Dict[str, int] = {}  # mode → long entity id
        first_dep: Dict[str, int] = {}
        first_node: Dict[str, Optional[str]] = {}

        for t in trips:
            mode = t["mode"]
            if mode not in self.VEHICLE_MODES or mode in vehicle_actors:
                continue
            # origin = first activity's node
            orig_idx = t["prec_idx"]
            orig_node = (node_ids[orig_idx]
                         if orig_idx < len(node_ids) else node_ids[0])
            if orig_node is None:
                continue
            v_id = self.ids.id_for(mode.lower(), f"{pid}_v_{mode}")
            vehicle_actors[mode] = v_id
            first_dep[mode] = t["dep_time"]
            first_node[mode] = orig_node

        if self.cfg.enable_dynamic_mode_choice or (unresolved and self.cfg.unsupported_mode == "replan"):
            for name in self.cfg.mc_included_modes:
                mode = "bike" if name == "bicycle" else name
                if mode in self.VEHICLE_MODES and mode not in vehicle_actors:
                    vehicle_actors[mode] = self.ids.id_for(mode, f"{pid}_v_{mode}")
                    first_dep[mode], first_node[mode] = trips[0]["dep_time"], node_ids[0]

        # ── Write vehicle actors ──
        for mode, v_id in vehicle_actors.items():
            v_actor = self._build_vehicle(v_id, mode, first_node[mode],
                                          first_dep[mode], rng,
                                          owner_id=self.ids.id_for("person", pid))
            wkey = {"car": "cars", "bike": "bikes", "motorcycle": "motorcycles"}[mode]
            self._writers[wkey].add(v_actor)

        # ── Build the native plan: a flat List[PlanElement] (Activity/WalkLeg/
        # PrivateVehicleLeg/TransitLeg/PendingDecision), per PersonState.originalPlan
        # (docs/SCENARIO_MODELING.md §4.11). This replaced the old dailySchedule +
        # currentActivityIndex + ArrivalLogistics model with no backward compatibility —
        # see docs/SCENARIO_MODELING.md's note on the same section.
        plan_elements: List[Dict] = []
        trip_by_prec: Dict[int, Dict] = {t["prec_idx"]: t for t in trips}

        for i, act in enumerate(acts):
            node_id = node_ids[i]
            if node_id is None:
                continue

            dep_trip = trip_by_prec.get(i)
            if dep_trip is not None:
                end_time = dep_trip["dep_time"]
            elif act.get("end_time") not in (None, ""):
                try:
                    end_time = int(float(act["end_time"]))
                except (ValueError, TypeError):
                    end_time = self.cfg.duration
            else:
                end_time = self.cfg.duration

            plan_elements.append({
                "kind":         "Activity",
                "activityType": act["purpose"],
                "nodeId":       self.ids.id_for("node", node_id),
                "endTime":      act.get("end_spec", {"kind": "AtTick", "tick": end_time}),
            })

            if dep_trip is None:
                continue
            dest_idx = dep_trip["foll_idx"]
            dest_node = node_ids[dest_idx] if dest_idx < len(node_ids) else None
            if dest_node is None:
                # Trip has no resolvable destination node — drop the leg (and the
                # trip); the activity itself stays, so the person just stays put.
                self.stats["trips_skipped_no_dest_node"] += 1
                continue

            if self.cfg.enable_dynamic_mode_choice or (
                    self.cfg.unsupported_mode == "replan" and dep_trip["mode"] not in self.MODE_TO_CONCRETE):
                self.stats["trips_replanned"] += 1
                plan_elements.append({
                    "kind": "PendingDecision",
                    "decision": {
                        "allowedModes": [
                            self.MC_MODE_TO_CONCRETE.get(m, m.capitalize())
                            for m in self.cfg.mc_included_modes
                        ],
                        "strategyId":      self.cfg.mode_choice_strategy,
                        "weightsOverride": None,
                    },
                })
            else:
                plan_elements.append(self._build_leg_element(
                    dep_trip["mode"], node_id, dest_node, vehicle_actors, rng
                ))

        if not plan_elements:
            self.stats["persons_skipped_empty_schedule"] += 1
            return

        # Build owned-vehicles reference map
        owned: Dict[str, Dict] = {}
        for mode, v_id in vehicle_actors.items():
            owned["bicycle" if mode == "bike" else mode] = {"id": v_id, "classType": self.VEHICLE_MODES[mode][0]}

        # plan_elements[0] is always the first Activity (built above, before any
        # leg) — its endTime.tick is when the person leaves home for the first
        # time, which distributes actor wake-up ticks throughout the day.
        first_act_start_tick = plan_elements[0]["endTime"].get("tick", 0)

        person = {
            "id": self.ids.id_for("person", pid),
            "typeActor": "hybrid.actor.Person",
            "data": {
                "dataType": "model.hybrid.entity.state.PersonState",
                "content": {
                    "startTick":             first_act_start_tick,
                    "scheduleOnTimeManager": True,
                    "originalPlan":          plan_elements,
                    "ownedVehicles":        owned,
                    "totalDistanceTraveled": 0.0,
                    "completedTrips":        0,
                    "modeChoiceWeights": {
                        "includedModes":         self.cfg.mc_included_modes,
                        "betaMode":              self.cfg.mc_beta_mode,
                        "betaAccess":            self.cfg.mc_beta_access,
                        "betaEgress":            self.cfg.mc_beta_egress,
                        "betaPrivateVehicle":    self.cfg.mc_beta_private_vehicle,
                        "modePrefSubway":        self.cfg.mc_mode_pref_subway,
                        "modePrefBus":           self.cfg.mc_mode_pref_bus,
                        "modePrefWalk":          self.cfg.mc_mode_pref_walk,
                        "modePrefCar":           self.cfg.mc_mode_pref_car,
                        "modePrefBicycle":       self.cfg.mc_mode_pref_bicycle,
                        "modePrefMotorcycle":    self.cfg.mc_mode_pref_motorcycle,
                        "maxAccessDistanceM":    self.cfg.mc_max_access_distance_m,
                        "maxWalkDistanceM":      self.cfg.mc_max_walk_distance_m,
                        "betaTravelTime":        self.cfg.mc_beta_travel_time,
                        "walkingSpeedMs":        self.cfg.mc_walking_speed_ms,
                        "avgBusSpeedMs":         self.cfg.mc_avg_bus_speed_ms,
                        "avgSubwaySpeedMs":      self.cfg.mc_avg_subway_speed_ms,
                        "avgBicycleSpeedMs":     self.cfg.mc_avg_bicycle_speed_ms,
                        "avgMotorcycleSpeedMs":  self.cfg.mc_avg_motorcycle_speed_ms,
                        "avgCarSpeedMs":         self.cfg.mc_avg_car_speed_ms,
                    },
                },
            },
            "dependencies": {},
        }
        self._writers["persons"].add(person)
        self.stats["persons_emitted"] += 1

    # ------------------------------------------------------------------
    def _build_leg_element(
        self, csv_mode: str, origin_node: str, dest_node: str,
        vehicle_actors: Dict[str, int], rng: random.Random,
    ) -> Dict:
        """Emit a native private/walk leg after the caller applies its mode policy."""
        concrete = self.MODE_TO_CONCRETE.get(csv_mode)
        vh_mode = self.CONCRETE_TO_VEHICLE_MODE.get(concrete)
        if vh_mode is not None and vh_mode in vehicle_actors:
            return {
                "kind":   "PrivateVehicleLeg",
                "mode":   concrete,
                "vehicle": {"id": vehicle_actors[vh_mode],
                            "classType": self.VEHICLE_MODES[vh_mode][0]},
                "driverAttributes": {
                    "aggressiveness":  rng.uniform(self.cfg.driver_aggressiveness_min,
                                                   self.cfg.driver_aggressiveness_max),
                    "maxSpeedFactor":  rng.uniform(self.cfg.driver_max_speed_factor_min,
                                                   self.cfg.driver_max_speed_factor_max),
                    "reactionTime":    rng.uniform(self.cfg.driver_reaction_time_min,
                                                   self.cfg.driver_reaction_time_max),
                    "minGapFactor":    rng.uniform(self.cfg.driver_min_gap_factor_min,
                                                   self.cfg.driver_min_gap_factor_max),
                },
            }
        if csv_mode != "walk":
            self.stats["trips_mode_fallback_walk"] += 1
        return {
            "kind":               "WalkLeg",
            "originNodeId":       self.ids.id_for("node", origin_node),
            "destinationNodeId":  self.ids.id_for("node", dest_node),
            "precomputedRoute":   None,
        }

    # ------------------------------------------------------------------
    def _build_vehicle(
        self, v_id: str, mode: str, origin_node: str,
        start_tick: int, rng: random.Random, owner_id=None
    ) -> Dict:
        actor_class, state_type, _ = self.VEHICLE_MODES[mode]
        size = self.VEHICLE_SIZE.get(mode, 4.5)
        content = {
            "startTick":           start_tick,
            "scheduleOnTimeManager": False,
            "movableStatus": "Parked",
            "movableCurrentNode": self.ids.id_for("node", origin_node),
            "origin":              self.ids.id_for("node", origin_node),
            "destination":         self.ids.id_for("node", origin_node),
            "actorType":           self.VEHICLE_MODE_TO_ACTOR_TYPE[mode],
            "size":                size,
            "currentSimulationMode": "MESO",
            "microState":          None,
            "distance":            0.0,
        }
        # CarState has precomputedRoute; BicycleState/MotorcycleState do not.
        actor_type = self.VEHICLE_MODE_TO_ACTOR_TYPE[mode]
        if actor_type == "Car":
            content["precomputedRoute"] = None
        dependencies = {
            "from_node": {"id": self.ids.id_for("node", origin_node), "classType": "hybrid.actor.Node"},
            "to_node":   {"id": self.ids.id_for("node", origin_node), "classType": "hybrid.actor.Node"},
        }
        if owner_id is not None and self.cfg.declare_ownership:
            dependencies["owner"] = {"id": owner_id, "classType": "hybrid.actor.Person", "relation": "part_of"}
        return {
            "id": v_id,
            "typeActor": actor_class,
            "data": {"dataType": state_type, "content": content},
            "dependencies": dependencies,
        }


# ============================================================================
# GTFS TRANSIT CONVERTER
# ============================================================================

class BusRoadRouter:
    """Offline routing through directed motor-road links, returning HTC compact routes."""

    def __init__(self, data_dir: Path):
        path = data_dir / "bus_routing_links.json"
        if not path.exists():
            raise ValueError("GTFS routing needs bus_routing_links.json; regenerate the network")
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


class GtfsTransitConverter:
    """
    Reads GTFS directories (sptrans/ and/or emtu/) and produces:
      - BusStop actors (one per unique stop)
      - BusStation actors (one per route, collecting its buses)
      - Bus actors (one per trip, capped at max_buses_per_route)
    Each stop is snapped to the nearest OSM highway node.
    """

    def __init__(self, cfg: Config, osm: OsmNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.osm = osm
        self.data_dir = data_dir
        self.ids = ids

        self._stop_writer    = StreamingWriter(data_dir, "bus_stops",    cfg.items_per_file)
        self._station_writer = StreamingWriter(data_dir, "bus_stations", cfg.items_per_file)
        self._bus_writer     = StreamingWriter(data_dir, "buses",        cfg.items_per_file)

        self.stats: Dict[str, int] = defaultdict(int)
        # stop_id (global) → OSM node_id
        self._stop_node: Dict[str, str] = {}
        # stop_id (global) → actor id
        self._stop_actor_id: Dict[str, int] = {}
        # stop_id (global) → (lat, lon)
        self._stop_latlon: Dict[str, Tuple[float, float]] = {}
        # stop_id (global) → sorted list of line labels that serve this stop
        self._stop_lines: Dict[str, List[str]] = defaultdict(list)
        self._router: Optional[BusRoadRouter] = None
        self._route_emitted = defaultdict(int)
        self._trip_direction = {}
        self._frequency_rows = defaultdict(list)
        self._stop_counter = 0
        self._bus_counter  = 0
        # accumulated route data for transit_routes.json (RAPTOR)
        # each entry: (provider, route_id, pat_dict, headway_secs)
        self._route_data: List[Tuple[str, str, Dict, int]] = []

    # ------------------------------------------------------------------
    def convert(self) -> None:
        for provider in self.cfg.transit_providers:
            gtfs_dir = self.cfg.input_dir / provider
            if not gtfs_dir.is_dir():
                raise FileNotFoundError(f"Requested GTFS provider not found: {gtfs_dir}")
            print(f"  [GTFS] processing {provider} …")
            self._process_provider(provider, gtfs_dir)

        for w, key in [
            (self._stop_writer,    "bus_stops"),
            (self._station_writer, "bus_stations"),
            (self._bus_writer,     "buses"),
        ]:
            r = w.close()
            self.stats[f"files_{key}"] = r["files"]
            self.stats[key] = r["total"]

        print(f"  [GTFS] done – {self.stats['bus_stops']:,} stops, "
              f"{self.stats['bus_stations']:,} stations, "
              f"{self.stats['buses']:,} buses")

    # ------------------------------------------------------------------
    def _process_provider(self, provider: str, gtfs_dir: Path) -> None:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            if not (gtfs_dir / name).is_file():
                raise FileNotFoundError(gtfs_dir / name)
        self._frequency_rows = defaultdict(list)
        freq = self._load_frequencies(gtfs_dir)
        # 1. Stops → snap to OSM, create BusStop actors
        stops_info = self._load_and_snap_stops(gtfs_dir, provider)

        # 2. Routes → name map
        routes_info = self._load_routes(gtfs_dir)

        # 3. Trips → route mapping
        trips_to_route = self._load_trips_map(gtfs_dir)

        # 4. Stop times → build route→stop_sequence and trips per route
        route_patterns: Dict[str, Dict] = {}  # route_id → {stops, trips, route_info}
        self._build_route_patterns(
            gtfs_dir, stops_info, trips_to_route, routes_info, route_patterns
        )

        if self.cfg.max_buses_per_route:
            by_source = defaultdict(list)
            for pattern_id, pattern in route_patterns.items():
                for trip in pattern["trips"]:
                    by_source[pattern["source_route_id"]].append(
                        (trip["start_tick"], trip["trip_id"], pattern_id))
            selected = {entry for entries in by_source.values()
                        for entry in sorted(entries)[:self.cfg.max_buses_per_route]}
            for pattern_id, pattern in route_patterns.items():
                before = len(pattern["trips"])
                pattern["trips"] = [trip for trip in pattern["trips"]
                                    if (trip["start_tick"], trip["trip_id"], pattern_id) in selected]
                self.stats["departures_removed_by_fleet_cap"] += before - len(pattern["trips"])


        # 6. Emit BusStation + Bus actors for each route pattern
        for route_id, pat in route_patterns.items():
            self._emit_route(provider, route_id, pat, freq)

    # ------------------------------------------------------------------
    def _load_and_snap_stops(
        self, gtfs_dir: Path, provider: str
    ) -> Dict[str, Dict]:
        stops: Dict[str, Dict] = {}
        sf = gtfs_dir / "stops.txt"
        if not sf.exists():
            return stops
        with open(sf, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid    = row["stop_id"].strip()
                lat    = float(row.get("stop_lat", 0) or 0)
                lon    = float(row.get("stop_lon", 0) or 0)
                name   = row.get("stop_name", f"Stop {sid}").strip()
                e, n   = _wgs84_to_utm23s(lat, lon)
                osm_nid = self.osm.nearest_node(e, n)
                if osm_nid is None:
                    continue

                # Global stop key avoids ID collisions between providers
                global_sid = f"{provider}_{sid}"
                actor_id   = self.ids.id_for("busstop", global_sid)
                self._stop_actor_id[global_sid] = actor_id
                self._stop_node[global_sid] = osm_nid
                self._stop_latlon[global_sid] = (lat, lon)
                stops[sid] = {
                    "global_id":   global_sid,
                    "actor_id":    actor_id,
                    "node_id":     osm_nid,
                    "name":        name,
                    "lat": lat, "lon": lon,
                }

                actor = {
                    "id": actor_id,
                    "typeActor": "hybrid.actor.BusStop",
                    "data": {
                        "dataType": "model.hybrid.entity.state.BusStopState",
                        "content": {
                            "nodeId": self.ids.id_for("node", osm_nid),
                            "label": name,
                            "people": {},
                            "scheduleOnTimeManager": False,
                        },
                    },
                    "dependencies": {
                        "node": {
                            "id":        self.ids.id_for("node", osm_nid),
                            "classType": "hybrid.actor.Node",
                        },
                    },
                }
                self._stop_writer.add(actor)
                self._stop_counter += 1

        return stops

    # ------------------------------------------------------------------
    def _load_routes(self, gtfs_dir: Path) -> Dict[str, str]:
        """Return {route_id: route_short_name}."""
        routes = {}
        rf = gtfs_dir / "routes.txt"
        if not rf.exists():
            return routes
        with open(rf, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                kind = row.get("route_type", "").strip()
                if kind == "3" or (kind.isdigit() and 700 <= int(kind) <= 899):
                    routes[row["route_id"].strip()] = row.get("route_short_name", "").strip() or row["route_id"].strip()
        return routes

    # ------------------------------------------------------------------
    def _active_services(self, gtfs_dir: Path) -> Set[str]:
        day = datetime.strptime(self.cfg.service_date, "%Y-%m-%d")
        date_key = day.strftime("%Y%m%d")
        weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]
        active = set()
        calendar = gtfs_dir / "calendar.txt"
        exceptions = gtfs_dir / "calendar_dates.txt"
        if not calendar.exists() and not exceptions.exists():
            raise ValueError(f"No GTFS service calendar in {gtfs_dir}")
        if calendar.exists():
            with calendar.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if row["start_date"] <= date_key <= row["end_date"] and row[weekday] == "1":
                        active.add(row["service_id"].strip())
        if exceptions.exists():
            with exceptions.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if row["date"] == date_key:
                        if row["exception_type"] == "1":
                            active.add(row["service_id"].strip())
                        elif row["exception_type"] == "2":
                            active.discard(row["service_id"].strip())
        if not active:
            raise ValueError(f"GTFS {gtfs_dir.name} has no service on {self.cfg.service_date}; check feed validity")
        return active

    def _load_trips_map(self, gtfs_dir: Path) -> Dict[str, str]:
        tm = {}
        services = self._active_services(gtfs_dir)
        routes = self._load_routes(gtfs_dir)
        with (gtfs_dir / "trips.txt").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                tid, rid = row["trip_id"].strip(), row["route_id"].strip()
                if row["service_id"].strip() in services and rid in routes:
                    tm[tid] = rid
                    self._trip_direction[tid] = row.get("direction_id", "")
                else:
                    self.stats["trips_filtered_service_or_route_type"] += 1
        return tm

    # ------------------------------------------------------------------
    def _build_route_patterns(
        self,
        gtfs_dir: Path,
        stops_info: Dict[str, Dict],
        trips_to_route: Dict[str, str],
        routes_info: Dict[str, str],
        out: Dict[str, Dict],
    ) -> None:
        """Fills *out* with route-pattern data from stop_times.txt."""
        stf = gtfs_dir / "stop_times.txt"
        if not stf.exists():
            return

        # Group stop_times by trip_id
        trip_stops: Dict[str, List[Tuple[int, str, int, int]]] = defaultdict(list)
        # (seq, stop_id, arr_secs, dep_secs)
        with open(stf, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                tid = row["trip_id"].strip()
                if tid not in trips_to_route:
                    continue
                sid = row["stop_id"].strip()
                # Keep unsnapped stops so the entire pattern is rejected, rather than shortcutting it.
                seq = int(row.get("stop_sequence", 0))
                arr = _time_str_to_secs(row.get("arrival_time", "00:00:00"))
                dep = _time_str_to_secs(row.get("departure_time", "00:00:00"))
                trip_stops[tid].append((seq, sid, arr, dep))

        patterns = {}
        for trip_id, stop_rows in sorted(trip_stops.items()):
            route_id = trips_to_route[trip_id]
            stop_rows.sort(key=lambda r: r[0])
            if len(stop_rows) < 2 or any(r[1] not in stops_info for r in stop_rows):
                self.stats["trips_skipped_unsnapped_stops"] += 1
                continue
            key = (route_id, self._trip_direction[trip_id], tuple(r[1] for r in stop_rows))
            if key not in patterns:
                pattern_id = f"{route_id}_{hashlib.sha256(repr(key).encode()).hexdigest()[:12]}"
                patterns[key] = pattern_id
                out[pattern_id] = {"name": pattern_id, "source_route_id": route_id,
                                   "stops": [r[1] for r in stop_rows], "trips": []}
            pat = out[patterns[key]]
            windows = self._frequency_rows.get(trip_id)
            starts = sorted({tick for start, end, step in windows for tick in range(start, min(end, self.cfg.duration + 1), step)}) if windows else [stop_rows[0][3]]
            for start in starts:
                if start < 0 or start > self.cfg.duration:
                    continue
                shift = start - stop_rows[0][3]
                pat["trips"].append({"trip_id": trip_id, "start_tick": start,
                                     "stop_rows": [(seq, sid, arr+shift, dep+shift) for seq, sid, arr, dep in stop_rows]})

    # ------------------------------------------------------------------
    def _load_frequencies(self, gtfs_dir: Path) -> Dict[str, int]:
        """Return {trip_id: headway_secs} from frequencies.txt."""
        freq: Dict[str, int] = {}
        ff = gtfs_dir / "frequencies.txt"
        if not ff.exists():
            return freq
        with open(ff, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                tid = row["trip_id"].strip()
                headway = int(row["headway_secs"])
                start, end = _time_str_to_secs(row["start_time"]), _time_str_to_secs(row["end_time"])
                if headway <= 0 or end <= start:
                    raise ValueError(f"Invalid GTFS frequency for trip {tid}")
                freq[tid] = headway
                self._frequency_rows[tid].append((start, end, headway))
                if row.get("exact_times", "0") != "1":
                    self.stats["frequency_windows_regularized"] += 1
        return freq

    # ------------------------------------------------------------------
    def _emit_route(self, provider: str, route_id: str, pat: Dict, freq: Dict[str, int]) -> None:
        source_route = (provider, pat["source_route_id"])
        cap = self.cfg.max_buses_per_route
        remaining = cap - self._route_emitted[source_route] if cap else None
        trips = sorted(pat["trips"], key=lambda t: (t["start_tick"], t["trip_id"]))
        if remaining is not None:
            trips = trips[:max(0, remaining)]
        if not trips:
            return
        # BusState uses a stop map, so repeated stop identities cannot represent loops.
        if len(set(pat["stops"])) != len(pat["stops"]):
            self.stats["patterns_skipped_repeated_stop"] += 1
            return
        gids = [f"{provider}_{sid}" for sid in pat["stops"]]
        stops = {}
        previous = None
        for gid in gids:
            node = self.ids.id_for("node", self._stop_node[gid])
            if node != previous:
                stops[self._stop_actor_id[gid]] = node
                previous = node
        if len(stops) < 2:
            self.stats["patterns_skipped_collapsed_stops"] += 1
            return
        if self._router is None:
            self._router = BusRoadRouter(self.data_dir)
        nodes = list(stops.values())
        route = []
        self.stats["patterns_routed"] += 1
        if self.stats["patterns_routed"] % 100 == 0:
            print(f"        … {self.stats['patterns_routed']:,} bus patterns routed", flush=True)
        try:
            for source, target in zip(nodes, nodes[1:]):
                route.extend(self._router.route(source, target))
            route.extend(self._router.route(nodes[-1], nodes[0]))
        except ValueError:
            self.stats["patterns_skipped_no_motor_route"] += 1
            return
        label = f"{provider}_{pat['name']}"
        pat = dict(pat, name=label, trips=trips)
        for gid in gids:
            self._stop_lines[gid].append(label)
        for trip in trips:
            bus_id = self.ids.id_for("bus", f"{provider}_{trip['trip_id']}_{trip['start_tick']}")
            self._bus_writer.add({
                "id": bus_id, "typeActor": "hybrid.actor.Bus",
                "data": {"dataType": "model.hybrid.entity.state.BusState", "content": {
                    "startTick": trip["start_tick"], "label": label, "capacity": 80,
                    "size": 12.0, "numberOfPorts": 2, "origin": nodes[0], "destination": nodes[-1],
                    "busStops": stops, "people": {}, "storedBestRoute": route,
                    "currentSimulationMode": "MESO", "microState": None,
                }},
                "dependencies": {"from_node": {"id": nodes[0], "classType": "hybrid.actor.Node"},
                                 "to_node": {"id": nodes[-1], "classType": "hybrid.actor.Node"}},
            })
        self.stats["circulating_bus_services"] += len(trips)
        self._route_emitted[source_route] += len(trips)
        headway = freq.get(trips[0]["trip_id"], 600)
        gaps = [b["start_tick"] - a["start_tick"] for a, b in zip(trips, trips[1:])]
        positive = [g for g in gaps if g > 0]
        if positive:
            headway = round(sum(positive) / len(positive))
        self._route_data.append((provider, route_id, pat, headway))

    # ------------------------------------------------------------------
    def transit_route_entries(self) -> List[Dict]:
        """Return TransitRoute-compatible dicts for RAPTOR routing.

        Each entry corresponds to one GTFS route-pattern, containing an ordered
        stop sequence with cumulative travel times derived from stop_times.txt
        departure-time deltas and the representative headway from frequencies.txt.

        The stop IDs in ``RouteStop.stopId`` match the ``id`` field in the
        transit_map entries produced by :meth:`transit_map_entries`.
        """
        entries = []
        for (provider, route_id, pat, headway) in self._route_data:
            stop_ids = pat["stops"]
            trips = pat["trips"]
            if not stop_ids or not trips:
                continue

            # Build ordered stop sequence with per-hop travel times.
            # Use the first trip's stop_rows which are already sorted by sequence.
            ref_trip = sorted(trips, key=lambda t: t["start_tick"])[0]
            stop_rows = sorted(ref_trip["stop_rows"], key=lambda r: r[0])  # sort by seq

            route_stops = []
            prev_dep_secs = None
            for seq, raw_sid, arr_secs, dep_secs in stop_rows:
                gid = f"{provider}_{raw_sid}"
                if gid not in self._stop_actor_id:
                    prev_dep_secs = dep_secs
                    continue
                transit_map_id = self.ids.id_for("stop", f"bus_{gid}")
                if prev_dep_secs is None:
                    travel_time = 0
                else:
                    travel_time = max(0, dep_secs - prev_dep_secs)
                route_stops.append({
                    "stopId": transit_map_id,
                    "travelTimeFromPrevSeconds": travel_time,
                })
                prev_dep_secs = dep_secs

            if len(route_stops) < 2:
                continue

            entries.append({
                "lineId":          pat["name"],
                "stopType":        "bus",
                "headwaySeconds":  headway,
                "stops":           route_stops,
            })
        return entries

    # ------------------------------------------------------------------
    def transit_map_entries(self) -> List[Dict]:
        """Return TransitStop-compatible dicts for all bus stops.

        Each entry represents one BusStop actor, listing all line labels that
        use it.  Stops not served by any routed line are omitted.
        """
        entries = []
        for global_sid, actor_id in self._stop_actor_id.items():
            lines = self._stop_lines.get(global_sid)
            if not lines:
                continue  # stop exists in GTFS but no route uses it
            node_id = self._stop_node.get(global_sid, "")
            lat, lon = self._stop_latlon.get(global_sid, (0.0, 0.0))
            entries.append({
                "id":              self.ids.id_for("stop", f"bus_{global_sid}"),
                "actorId":         actor_id,
                "actorClassType":  "hybrid.actor.BusStop",
                "nodeId":          self.ids.id_for("node", node_id),
                "latitude":        lat,
                "longitude":       lon,
                "stopType":        "bus",
                "lines":           sorted(lines),
            })
        return entries


# ============================================================================
# OSM RAIL EXTRACTOR  (subway / train / light_rail / monorail from OSM)
# ============================================================================

class OsmRailExtractor:
    """
    Extracts railway infrastructure from OSM:
      - Station nodes (railway=station|stop|halt)
      - Railway ways (railway=subway|rail|light_rail|monorail)
      - Route relations (route=subway|train|light_rail|monorail)

    Produces ordered station sequences per line by walking the route relations.
    """

    # Rail types we consider active infrastructure
    ACTIVE_RAIL: Set[str] = {"subway", "rail", "light_rail", "monorail"}

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Populated during extraction
        self.station_nodes: Dict[str, Dict] = {}   # osm_node_id → info
        self.railway_ways: Dict[str, Dict] = {}    # osm_way_id → info
        self.route_relations: List[Dict] = []       # ordered list
        self._all_rail_node_ids: Set[str] = set()   # all node refs from railway ways
        self._rail_node_coords: Dict[str, Tuple[float, float]] = {}  # id → (lat, lon)
        self._rail_node_utm: Dict[str, Tuple[float, float]] = {}     # id → (e, n)

    # ------------------------------------------------------------------
    def extract(self, osm_file: Path) -> None:
        """Three-pass extraction of railway data from OSM XML/.gz."""
        print(f"  [RAIL] pass 1/3 – scanning railway ways & relations …")
        self._pass1_ways_and_relations(osm_file)
        print(f"        {len(self.railway_ways):,} railway ways, "
              f"{len(self.route_relations)} route relations, "
              f"{len(self._all_rail_node_ids):,} node refs")

        print(f"  [RAIL] pass 2/3 – extracting station nodes & rail node coords …")
        self._pass2_nodes(osm_file)
        print(f"        {len(self.station_nodes):,} station/stop/halt nodes extracted")

        print(f"  [RAIL] pass 3/3 – building ordered station sequences per line …")
        self._build_line_sequences()

        route_types = defaultdict(int)
        for r in self.route_relations:
            route_types[r["route"]] += 1
        for rt, cnt in sorted(route_types.items()):
            print(f"        {rt}: {cnt} route variants")
        print(f"  [RAIL] done – {len(self.route_relations)} routes with "
              f"{sum(len(r.get('ordered_stations', [])) for r in self.route_relations)} "
              f"total station references")

    # ------------------------------------------------------------------
    def _open_osm(self, osm_file: Path):
        if osm_file.suffix == ".gz":
            return gzip.open(osm_file, "rb")
        return open(osm_file, "rb")

    # ------------------------------------------------------------------
    def _pass1_ways_and_relations(self, osm_file: Path) -> None:
        root = None
        count = 0
        with self._open_osm(osm_file) as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem
                    continue
                if event != "end":
                    continue
                count += 1
                if count % 2_000_000 == 0:
                    print(f"        … {count:,} elements", end="\r")

                if elem.tag == "node":
                    # Quick check for station nodes (we gather coords in pass 2)
                    tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                    rw = tags.get("railway", "")
                    if rw in ("station", "stop", "halt") or tags.get("public_transport") == "stop_position":
                        self.station_nodes[elem.get("id")] = {
                            "name": tags.get("name", ""),
                            "railway": rw,
                            "station_type": tags.get("station", ""),
                            "operator": tags.get("operator", ""),
                            "line": tags.get("line", ""),
                            "network": tags.get("network", ""),
                        }

                elif elem.tag == "way":
                    tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                    rw = tags.get("railway", "")
                    if rw in self.ACTIVE_RAIL:
                        node_refs = [nd.get("ref") for nd in elem.findall("nd")]
                        self.railway_ways[elem.get("id")] = {
                            "railway": rw,
                            "name": tags.get("name", ""),
                            "node_refs": node_refs,
                            "ref": tags.get("ref", ""),
                            "service": tags.get("service", ""),
                            "usage": tags.get("usage", ""),
                            "operator": tags.get("operator", ""),
                        }
                        self._all_rail_node_ids.update(node_refs)

                elif elem.tag == "relation":
                    tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                    route = tags.get("route", "")
                    if route in ("subway", "train", "light_rail", "monorail") and ("rail" if route == "train" else route) in self.cfg.rail_types:
                        stop_nodes = []
                        way_refs = []
                        for m in elem.findall("member"):
                            mtype = m.get("type")
                            mref = m.get("ref")
                            mrole = m.get("role", "")
                            if "stop" in mrole and mtype == "node":
                                stop_nodes.append(mref)
                            elif mtype == "way" and mrole not in ("platform",):
                                way_refs.append(mref)
                        self.route_relations.append({
                            "osm_id": elem.get("id"),
                            "route": route,
                            "name": tags.get("name", "unknown"),
                            "ref": tags.get("ref", ""),
                            "colour": tags.get("colour", ""),
                            "operator": tags.get("operator", ""),
                            "from": tags.get("from", ""),
                            "to": tags.get("to", ""),
                            "interval": tags.get("interval", ""),
                            "stop_node_ids": stop_nodes,
                            "way_refs": way_refs,
                        })
                        self._all_rail_node_ids.update(stop_nodes)

                if (root is not None
                        and elem.tag in ("node", "way", "relation", "bounds")
                        and len(root) > 10_000):
                    root.clear()
            del ctx
        gc.collect()
        print(f"        {count:,} elements scanned                    ")

    # ------------------------------------------------------------------
    def _pass2_nodes(self, osm_file: Path) -> None:
        """Extract coordinates for all rail-related nodes."""
        root = None
        count = 0
        found = 0
        with self._open_osm(osm_file) as fh:
            ctx = ET.iterparse(fh, events=("start", "end"))
            for event, elem in ctx:
                if event == "start" and root is None:
                    root = elem
                    continue
                if event != "end":
                    continue
                count += 1

                if elem.tag == "node":
                    nid = elem.get("id")
                    if nid in self._all_rail_node_ids or nid in self.station_nodes:
                        lat = float(elem.get("lat", 0))
                        lon = float(elem.get("lon", 0))
                        self._rail_node_coords[nid] = (lat, lon)
                        e, n = _wgs84_to_utm23s(lat, lon)
                        self._rail_node_utm[nid] = (e, n)
                        found += 1
                        # Update station nodes with coords
                        if nid in self.station_nodes:
                            self.station_nodes[nid]["lat"] = lat
                            self.station_nodes[nid]["lon"] = lon
                            self.station_nodes[nid]["utm_e"] = e
                            self.station_nodes[nid]["utm_n"] = n
                elif elem.tag == "way":
                    # All nodes come before ways in OSM
                    break

                if (root is not None
                        and elem.tag in ("node", "bounds")
                        and len(root) > 10_000):
                    root.clear()
            del ctx
        gc.collect()
        print(f"        {found:,} rail node coordinates extracted")

    # ------------------------------------------------------------------
    def _build_line_sequences(self) -> None:
        """
        For each route relation, build an ordered station list using
        the stop_node_ids from the relation members (which are already ordered
        in OSM route relations). Also compute inter-station distances.
        """
        for rel in self.route_relations:
            missing = [nid for nid in rel["stop_node_ids"] if nid not in self._rail_node_utm]
            if missing:
                rel["missing_station_nodes"] = missing
                rel["ordered_stations"] = []
                continue
            ordered = []
            for snid in rel["stop_node_ids"]:
                info = self.station_nodes.get(snid, {})
                name = info.get("name", f"Stop {snid}")
                if snid not in self._rail_node_utm:
                    continue
                e, n = self._rail_node_utm[snid]
                ordered.append({
                    "osm_node_id": snid,
                    "name": name,
                    "utm_e": e,
                    "utm_n": n,
                })

            # Compute inter-station distances
            for i in range(1, len(ordered)):
                prev = ordered[i - 1]
                curr = ordered[i]
                dist = math.hypot(curr["utm_e"] - prev["utm_e"],
                                  curr["utm_n"] - prev["utm_n"])
                curr["distance_from_prev"] = max(dist, 100.0)  # min 100m
            if ordered:
                ordered[0]["distance_from_prev"] = 0.0

            rel["ordered_stations"] = ordered

    # ------------------------------------------------------------------
    def calc_distance(self, node_a: str, node_b: str) -> float:
        """Euclidean distance between two OSM nodes in UTM metres."""
        if node_a in self._rail_node_utm and node_b in self._rail_node_utm:
            ae, an = self._rail_node_utm[node_a]
            be, bn = self._rail_node_utm[node_b]
            return max(math.hypot(be - ae, bn - an), 100.0)
        return 1000.0  # fallback

    def get_nearest_highway_node(
        self, osm_parser: "OsmNetworkParser", osm_node_id: str
    ) -> Optional[str]:
        """Find the nearest highway node to a rail station node."""
        if osm_node_id in self._rail_node_utm:
            e, n = self._rail_node_utm[osm_node_id]
            return osm_parser.nearest_node(e, n)
        return None


# ============================================================================
# RAIL TRANSIT CONVERTER  (OSM rail → SubwayStation + RailLink + Subway actors)
# ============================================================================

class RailTransitConverter:
    """
    Converts extracted OSM rail data into simulator actors:
      - SubwayStation actors (one per unique station)
      - RailLink actors (one per inter-station segment, forward + return)
      - Subway train definitions embedded in garage station state

    Lines are deduplicated: if a line has two route relations (one per direction),
    only one set of stations and rail links is created, with bidirectional links.
    The garage station at each end dispatches trains in its direction.
    """

    # Map OSM route type → simulator railType
    _RAIL_TYPE_MAP = {
        "subway":     "SUBWAY",
        "train":      "HEAVY_RAIL",
        "light_rail": "LIGHT_RAIL",
        "monorail":   "SUBWAY",  # simulator treats monorail as subway
    }

    # Default speeds by rail type (km/h)
    _RAIL_SPEEDS = {
        "subway":     80.0,
        "train":      60.0,
        "light_rail": 50.0,
        "monorail":   60.0,
    }

    def __init__(
        self,
        cfg: Config,
        rail: OsmRailExtractor,
        osm: "OsmNetworkParser",
        data_dir: Path,
        ids: ScenarioIdRegistry,
    ):
        self.cfg = cfg
        self.rail = rail
        self.osm = osm
        self.data_dir = data_dir
        self.ids = ids

        self._station_writer = StreamingWriter(data_dir, "subway_stations", cfg.items_per_file)
        self._rail_link_writer = StreamingWriter(data_dir, "rail_links", cfg.items_per_file)

        self.stats: Dict[str, int] = defaultdict(int)

        # Load per-line parameter overrides (keyed by line_key_pattern)
        self._line_overrides: Dict[str, Dict] = {}
        if cfg.rail_lines_config_file:
            cfg_path = Path(cfg.rail_lines_config_file)
            if not cfg_path.is_absolute():
                cfg_path = cfg.input_dir / cfg_path
            if not cfg_path.is_file():
                raise FileNotFoundError(cfg_path)
            raw = json.loads(cfg_path.read_text())
            if isinstance(raw, list):
                raw = {entry["line_key_pattern"]: entry for entry in raw}
            if not isinstance(raw, dict):
                raise ValueError("Rail line configuration must be an object or legacy list")
            integers = {"capacity", "max_trains", "number_of_ports", "headway_seconds", "stop_time_seconds"}
            numeric = integers | {"velocity_ms", "max_terminal_gap_m"}
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    raise ValueError(f"Invalid rail override {key}")
                for name, value in entry.items():
                    if name in numeric:
                        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (name != "max_trains" and value == 0):
                            raise ValueError(f"Invalid rail override {key}.{name}")
                        if name in integers and not isinstance(value, int):
                            raise ValueError(f"Rail override {key}.{name} requires an integer")
                    elif name == "relation_ids":
                        if not isinstance(value, list) or not value or not all(isinstance(v, (int, str)) for v in value):
                            raise ValueError(f"Invalid relation_ids for {key}")
                    elif not name.startswith("_") and name not in ("line_key_pattern", "description", "name", "notes"):
                        raise ValueError(f"Unknown rail override {key}.{name}")
                self._line_overrides[key] = entry
            print(f"  [RAIL] loaded {len(self._line_overrides)} per-line overrides from {cfg_path.name}")

        # station_key → {actor_id, osm_node_id, highway_node_id, name, ...}
        self._stations: Dict[str, Dict] = {}
        # line_id → {name, ref, route_type, colour, operator,
        #            forward_stations: [...], return_stations: [...],
        #            rail_links_fwd: [...], rail_links_ret: [...]}
        self._lines: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    def convert(self) -> None:
        """Main conversion pipeline."""
        print(f"  [RAIL→SIM] deduplicating lines …")
        self._deduplicate_lines()
        print(f"        {len(self._lines)} unique lines identified")

        print(f"  [RAIL→SIM] creating station actors …")
        self._create_stations()
        print(f"        {len(self._stations)} unique stations")

        print(f"  [RAIL→SIM] creating rail link actors …")
        self._lines = {
            key: line for key, line in self._lines.items()
            if all(st["osm_node_id"] in self._stations
                   for seq in (line["forward_stations"], line["return_stations"]) for st in seq)
        }
        self._create_rail_links()

        print(f"  [RAIL→SIM] configuring garage stations with trains …")
        self._configure_garages()

        print(f"  [RAIL→SIM] writing actors …")
        self._write_all()

        st_r = self._station_writer.close()
        rl_r = self._rail_link_writer.close()
        self.stats["subway_stations"] = st_r["total"]
        self.stats["files_subway_stations"] = st_r["files"]
        self.stats["rail_links"] = rl_r["total"]
        self.stats["files_rail_links"] = rl_r["files"]

        print(f"  [RAIL→SIM] done – {self.stats['subway_stations']:,} stations, "
              f"{self.stats['rail_links']:,} rail links, "
              f"{self.stats.get('subway_trains', 0):,} train definitions")

    # ------------------------------------------------------------------
    def transit_map_entries(self) -> List[Dict]:
        """Return TransitStop-compatible dicts for all subway/rail stations.

        Each entry lists the line keys that stop at this station.
        """
        station_lines: Dict[str, List[str]] = defaultdict(list)
        for line_key, line in self._lines.items():
            for suffix, seq in (("", line["forward_stations"]),
                                ("_ret", line["return_stations"] or list(reversed(line["forward_stations"])))):
                for st in seq:
                    if st["osm_node_id"] in self._stations:
                        station_lines[st["osm_node_id"]].append(line_key + suffix)

        entries = []
        for osm_nid, station in self._stations.items():
            coords = self.rail._rail_node_coords.get(osm_nid)
            if not coords:
                continue
            lat, lon = coords
            lines = sorted(station_lines.get(osm_nid, []))
            entries.append({
                "id":              self.ids.id_for("stop", f"subway_{osm_nid}"),
                "actorId":         station["actor_id"],
                "actorClassType":  "hybrid.actor.SubwayStation",
                "nodeId":          self.ids.id_for("node", station['highway_node_id']),
                "latitude":        lat,
                "longitude":       lon,
                "stopType":        "subway",
                "lines":           lines,
            })
        return entries

    def transit_route_entries(self) -> List[Dict]:
        entries = []
        for key, line in self._lines.items():
            ov = self._line_overrides.get(key, {})
            velocity = ov.get("velocity_ms", self.cfg.train_velocity)
            dwell = ov.get("stop_time_seconds", self.cfg.train_stop_time)
            headway = ov.get("headway_seconds", line["interval"])
            for suffix, links in (("", line["fwd_rail_links"]), ("_ret", line["ret_rail_links"])):
                if not links:
                    continue
                first = links[0]["from_station_key"]
                stops = [{"stopId": self.ids.id_for("stop", f"subway_{first}"), "travelTimeFromPrevSeconds": 0}]
                for link in links:
                    node = link["to_station_key"]
                    stops.append({"stopId": self.ids.id_for("stop", f"subway_{node}"),
                                  "travelTimeFromPrevSeconds": round(link["distance"] / velocity + dwell)})
                entries.append({"lineId": key + suffix, "stopType": "subway", "headwaySeconds": headway, "stops": stops})
        return entries

    # ------------------------------------------------------------------
    def _deduplicate_lines(self) -> None:
        """
        Group route relations by line reference (e.g., ref="1" for Linha 1).
        Each group becomes one line with forward and return station sequences.
        """
        line_groups: Dict[str, List[Dict]] = defaultdict(list)

        for rel in self.rail.route_relations:
            # Build a line key from ref + route type
            ref = rel.get("ref", "").strip()
            route = rel["route"]
            if not ref:
                # Use relation name as fallback
                ref = rel["name"].replace(" ", "_")[:30]
            line_key = f"{route}_{ref}"
            line_groups[line_key].append(rel)

        for line_key, rels in line_groups.items():
            selected_ids = self._line_overrides.get(line_key, {}).get("relation_ids")
            if selected_ids is not None:
                rels = [rel for rel in rels if str(rel["osm_id"]) in {str(v) for v in selected_ids}]
                if not rels:
                    raise ValueError(f"No OSM relations selected for {line_key}")
            # Pick the first relation for metadata
            first = rels[0]
            route_type = first["route"]

            # Determine forward and return: use the one with more stations
            # or the first one as forward
            rels_with_stations = [r for r in rels if len(r.get("ordered_stations", [])) >= 2]
            if not rels_with_stations:
                continue

            # Sort by number of stations descending; first = forward
            rels_with_stations.sort(key=lambda r: len(r["ordered_stations"]), reverse=True)
            fwd_rel = rels_with_stations[0]
            distinct = {tuple(st["osm_node_id"] for st in r["ordered_stations"]) for r in rels_with_stations}
            if len(distinct) > 2:
                raise ValueError(f"OSM line {line_key} has multiple branches; split/select relations in a reviewed rail extract")
            ret_rel = next((r for r in rels_with_stations[1:]
                            if r["ordered_stations"] != fwd_rel["ordered_stations"]), None)

            self._lines[line_key] = {
                "name": first.get("name", line_key),
                "ref": first.get("ref", ""),
                "route_type": route_type,
                "colour": first.get("colour", ""),
                "operator": first.get("operator", ""),
                "interval": self._parse_interval(first.get("interval", "")),
                "forward_stations": fwd_rel["ordered_stations"],
                "return_stations": ret_rel["ordered_stations"] if ret_rel else [],
                "fwd_rail_links": [],
                "ret_rail_links": [],
            }

    # ------------------------------------------------------------------
    def _parse_interval(self, raw: str) -> int:
        """Parse interval from OSM tag (minutes) to seconds."""
        try:
            minutes = int(raw)
            return max(60, minutes * 60)  # at least 1 minute
        except (ValueError, TypeError):
            return self.cfg.train_interval  # default from config

    # ------------------------------------------------------------------
    def _create_stations(self) -> None:
        """Create unique station actors from all lines."""
        for line_key, line in self._lines.items():
            for station_list in (line["forward_stations"], line["return_stations"]):
                for st in station_list:
                    osm_nid = st["osm_node_id"]
                    if osm_nid in self._stations:
                        continue  # already created

                    # Find nearest highway node for this station
                    hw_node = self.rail.get_nearest_highway_node(self.osm, osm_nid)
                    if not hw_node:
                        continue

                    station_key = osm_nid
                    actor_id = self.ids.id_for("subwaystation", f"station_{osm_nid}")

                    self._stations[station_key] = {
                        "actor_id": actor_id,
                        "osm_node_id": osm_nid,
                        "highway_node_id": hw_node,
                        "name": st["name"],
                        "utm_e": st["utm_e"],
                        "utm_n": st["utm_n"],
                        "terminal": False,
                        "garage": False,
                        "lines": {},          # populated later
                        "subways": {},         # populated later
                        "linesRoute": {},      # populated later
                    }

    # ------------------------------------------------------------------
    def _create_rail_links(self) -> None:
        """Create RailLink actors for each inter-station segment."""
        for line_key, line in self._lines.items():
            rail_type = self._RAIL_TYPE_MAP.get(line["route_type"], "SUBWAY")
            speed = self._RAIL_SPEEDS.get(line["route_type"], 80.0)

            # Forward direction
            fwd_stations = line["forward_stations"]
            fwd_links = []
            for i in range(len(fwd_stations) - 1):
                from_st = fwd_stations[i]
                to_st = fwd_stations[i + 1]
                from_key = from_st["osm_node_id"]
                to_key = to_st["osm_node_id"]

                if from_key not in self._stations or to_key not in self._stations:
                    continue

                distance = to_st.get("distance_from_prev", 1000.0)
                link_id = self.ids.id_for("rail_link", f"line_{line_key}_segment_{i}")

                from_hw = self._stations[from_key]["highway_node_id"]
                to_hw = self._stations[to_key]["highway_node_id"]

                fwd_links.append({
                    "link_id": link_id,
                    "from_station_key": from_key,
                    "to_station_key": to_key,
                    "from_hw_node": from_hw,
                    "to_hw_node": to_hw,
                    "distance": distance,
                    "rail_type": rail_type,
                    "speed": speed,
                    "line_key": line_key,
                })

            line["fwd_rail_links"] = fwd_links

            # Return direction: if we have explicit return stations, use them;
            # otherwise reverse the forward direction
            if line["return_stations"]:
                ret_stations = line["return_stations"]
            else:
                ret_stations = list(reversed(fwd_stations))
                # Recompute distances for reverse
                for j in range(1, len(ret_stations)):
                    prev = ret_stations[j - 1]
                    curr = ret_stations[j]
                    dist = math.hypot(curr["utm_e"] - prev["utm_e"],
                                      curr["utm_n"] - prev["utm_n"])
                    curr = dict(curr)  # don't mutate original
                    curr["distance_from_prev"] = max(dist, 100.0)
                    ret_stations[j] = curr

            ret_links = []
            for i in range(len(ret_stations) - 1):
                from_st = ret_stations[i]
                to_st = ret_stations[i + 1]
                from_key = from_st["osm_node_id"]
                to_key = to_st["osm_node_id"]

                if from_key not in self._stations or to_key not in self._stations:
                    continue

                distance = to_st.get("distance_from_prev", 1000.0)
                link_id = self.ids.id_for("rail_link", f"line_{line_key}_return_{i}")

                from_hw = self._stations[from_key]["highway_node_id"]
                to_hw = self._stations[to_key]["highway_node_id"]

                ret_links.append({
                    "link_id": link_id,
                    "from_station_key": from_key,
                    "to_station_key": to_key,
                    "from_hw_node": from_hw,
                    "to_hw_node": to_hw,
                    "distance": distance,
                    "rail_type": rail_type,
                    "speed": speed,
                    "line_key": line_key,
                })

            line["ret_rail_links"] = ret_links

            terminal_links = []
            def join_terminal(before: Dict, after: Dict, suffix: str) -> List[Dict]:
                source, target = before["to_station_key"], after["from_station_key"]
                if source == target:
                    return []
                a, b = self._stations[source], self._stations[target]
                gap = math.hypot(a["utm_e"] - b["utm_e"], a["utm_n"] - b["utm_n"])
                limit = self._line_overrides.get(line_key, {}).get("max_terminal_gap_m", 250.0)
                if a["name"].casefold().strip() != b["name"].casefold().strip() or gap > limit:
                    raise ValueError(f"Rail {line_key}: directional terminals do not match; review relation_ids/station correspondence")
                link = {
                    "link_id": self.ids.id_for("rail_link", f"{line_key}_terminal_{suffix}"),
                    "from_station_key": source, "to_station_key": target,
                    "from_hw_node": a["highway_node_id"], "to_hw_node": b["highway_node_id"],
                    "distance": max(1.0, gap), "rail_type": rail_type, "speed": speed, "line_key": line_key,
                }
                terminal_links.append(link)
                self.stats["synthetic_terminal_links"] += 1
                return [link]

            if fwd_links and ret_links:
                far_join = join_terminal(fwd_links[-1], ret_links[0], "far")
                near_join = join_terminal(ret_links[-1], fwd_links[0], "near")
                line["fwd_cycle"] = fwd_links + far_join + ret_links + near_join
                line["ret_cycle"] = ret_links + near_join + fwd_links + far_join
            line["terminal_links"] = terminal_links

            # Mark terminal and garage stations
            if fwd_stations:
                first_key = fwd_stations[0]["osm_node_id"]
                last_key = fwd_stations[-1]["osm_node_id"]
                if first_key in self._stations:
                    self._stations[first_key]["terminal"] = True
                    self._stations[first_key]["garage"] = True
                if last_key in self._stations:
                    self._stations[last_key]["terminal"] = True

    # ------------------------------------------------------------------
    def _configure_garages(self) -> None:
        """
        Configure garage stations with line info, train definitions,
        and route entries so the SubwayStation actor can dispatch trains.
        """
        for line_key, line in self._lines.items():
            interval = line["interval"]
            fwd_links = line["fwd_rail_links"]
            ret_links = line["ret_rail_links"]

            if not fwd_links:
                continue

            # Apply per-line overrides (matched by line_key_pattern)
            ov = self._line_overrides.get(line_key, {})
            interval        = ov.get("headway_seconds", interval)
            capacity        = ov.get("capacity",        self.cfg.train_capacity)
            velocity        = ov.get("velocity_ms",     self.cfg.train_velocity)
            stop_time       = ov.get("stop_time_seconds", self.cfg.train_stop_time)
            n_ports         = ov.get("number_of_ports", self.cfg.train_number_of_ports)
            max_trains_ov   = ov.get("max_trains",      self.cfg.max_trains_per_line)

            # Forward garage = first station of forward direction
            fwd_stations = line["forward_stations"]
            if not fwd_stations:
                continue
            fwd_garage_key = fwd_stations[0]["osm_node_id"]

            # Return garage = first station of return direction
            # (= last station of forward direction if no explicit return)
            ret_garage_key = None
            if line["return_stations"]:
                ret_garage_key = line["return_stations"][0]["osm_node_id"]
            elif fwd_stations:
                ret_garage_key = fwd_stations[-1]["osm_node_id"]

            # Configure forward garage
            if fwd_garage_key in self._stations:
                garage = self._stations[fwd_garage_key]
                garage["garage"] = True
                garage["terminal"] = True

                # Line info
                garage["lines"][line_key] = {
                    "interval": interval,
                    "nextTick": 0,
                }

                # Train definitions
                trains = []
                available = self.cfg.duration // interval
                n_trains = min(max_trains_ov, available) if max_trains_ov else available
                for t in range(n_trains):
                    trains.append({
                        "line": line_key,
                        "actorId": self.ids.id_for("subway", f"{line_key}_fwd_train_{t}"),
                        "capacity": capacity,
                        "numberOfPorts": n_ports,
                        "velocity": velocity,
                        "stopTime": stop_time,
                    })
                    self.stats["subway_trains"] += 1
                garage["subways"][line_key] = trains

                # Route entries: list of (next_station, rail_link_id) pairs
                route_entries = []
                for lnk in line.get("fwd_cycle", fwd_links + ret_links):
                    to_key = lnk["to_station_key"]
                    if to_key in self._stations:
                        route_entries.append({
                            "stationNode": {
                                "stationId": self._stations[to_key]["actor_id"],
                                "nodeId": self.ids.id_for("node", self._stations[to_key]['highway_node_id']),
                            },
                            "railLinkId": lnk["link_id"],
                        })
                garage["linesRoute"][line_key] = route_entries

            # Configure return garage
            if ret_garage_key and ret_garage_key in self._stations and ret_links:
                garage = self._stations[ret_garage_key]
                garage["garage"] = True
                garage["terminal"] = True

                ret_line_key = f"{line_key}_ret"

                garage["lines"][ret_line_key] = {
                    "interval": interval,
                    "nextTick": 0,
                }

                trains = []
                available = self.cfg.duration // interval
                n_trains = min(max_trains_ov, available) if max_trains_ov else available
                for t in range(n_trains):
                    trains.append({
                        "line": ret_line_key,
                        "actorId": self.ids.id_for("subway", f"{line_key}_ret_train_{t}"),
                        "capacity": capacity,
                        "numberOfPorts": n_ports,
                        "velocity": velocity,
                        "stopTime": stop_time,
                    })
                    self.stats["subway_trains"] += 1
                garage["subways"][ret_line_key] = trains

                route_entries = []
                for lnk in line.get("ret_cycle", ret_links + fwd_links):
                    to_key = lnk["to_station_key"]
                    if to_key in self._stations:
                        route_entries.append({
                            "stationNode": {
                                "stationId": self._stations[to_key]["actor_id"],
                                "nodeId": self.ids.id_for("node", self._stations[to_key]['highway_node_id']),
                            },
                            "railLinkId": lnk["link_id"],
                        })
                garage["linesRoute"][ret_line_key] = route_entries

    # ------------------------------------------------------------------
    def _write_all(self) -> None:
        """Write SubwayStation and RailLink actors to JSON files."""
        # Write station actors
        for key, st in self._stations.items():
            actor = {
                "id": st["actor_id"],
                "typeActor": "hybrid.actor.SubwayStation",
                "data": {
                    "dataType": "model.hybrid.entity.state.SubwayStationState",
                    "content": {
                        "startTick": 0,
                        "name": st["name"],
                        "nodeId": self.ids.id_for("node", st['highway_node_id']),
                        "terminal": st["terminal"],
                        "garage": st["garage"],
                        "lines": st["lines"],
                        "subways": st["subways"],
                        "linesRoute": st["linesRoute"],
                        "people": {},
                        "status": "Start",
                    },
                },
                "dependencies": {
                    "node": {
                        "id": self.ids.id_for("node", st['highway_node_id']),
                        "classType": "hybrid.actor.Node",
                    },
                },
            }
            self._station_writer.add(actor)

        # Write rail link actors
        for line_key, line in self._lines.items():
            rail_type = self._RAIL_TYPE_MAP.get(line["route_type"], "SUBWAY")
            for links_list in (line["fwd_rail_links"], line["ret_rail_links"], line.get("terminal_links", [])):
                for lnk in links_list:
                    from_key = lnk["from_station_key"]
                    to_key = lnk["to_station_key"]
                    if from_key not in self._stations or to_key not in self._stations:
                        continue

                    from_st = self._stations[from_key]
                    to_st = self._stations[to_key]

                    actor = {
                        "id": lnk["link_id"],
                        "typeActor": "hybrid.actor.RailLink",
                        "data": {
                            "dataType": "model.hybrid.entity.state.RailLinkState",
                            "content": {
                                "from": self.ids.id_for("node", lnk['from_hw_node']),
                                "to": self.ids.id_for("node", lnk['to_hw_node']),
                                "length": _sanitize(lnk["distance"], 100.0),
                                "lanes": 2,
                                "speedLimit": lnk["speed"],
                                "capacity": 10.0,
                                "freeSpeed": lnk["speed"],
                                "railType": lnk["rail_type"],
                                "subwayLine": lnk["line_key"],
                                "fromStation": from_st["actor_id"],
                                "toStation": to_st["actor_id"],
                                "gradient": 0.0,
                                "curvature": 0.0,
                                "simulationMode": "MESO",
                                "laneConfigurations": [],
                                "vehiclesByLane": {},
                                "microTimeStep": self.cfg.micro_time_step,
                                "microTicksPerGlobalTick": self.cfg.micro_ticks_per_tick,
                                "registered": [],
                                "scheduleOnTimeManager": False,
                            },
                        },
                        "dependencies": {
                            "from_node": {
                                "id": self.ids.id_for("node", lnk['from_hw_node']),
                                "classType": "hybrid.actor.Node",
                            },
                            "to_node": {
                                "id": self.ids.id_for("node", lnk['to_hw_node']),
                                "classType": "hybrid.actor.Node",
                            },
                            "from_station": {
                                "id": from_st["actor_id"],
                                "classType": "hybrid.actor.SubwayStation",
                            },
                            "to_station": {
                                "id": to_st["actor_id"],
                                "classType": "hybrid.actor.SubwayStation",
                            },
                        },
                    }
                    self._rail_link_writer.add(actor)


# ============================================================================
# TRAFFIC SIGNAL GENERATOR
# ============================================================================

class TrafficSignalGenerator:
    """
    Generates TrafficSignal actors from OSM traffic_signals nodes.

    Each signal node that has incoming links gets a signal actor with
    one phase per distinct incoming link.  Cycle time is split equally
    among phases (minus yellow + all-red transitions).
    """

    def __init__(self, cfg: Config, osm: OsmNetworkParser, data_dir: Path, ids: ScenarioIdRegistry):
        self.cfg = cfg
        self.osm = osm
        self.data_dir = data_dir
        self.ids = ids
        self._rng = random.Random(cfg.random_seed + 777)
        self.stats: Dict[str, int] = defaultdict(int)
        self._writer = StreamingWriter(data_dir, "traffic_signals", cfg.items_per_file)

    def generate(self) -> None:
        signal_serial = 0
        signal_node_ids = self.osm._signal_node_ids
        incoming = self.osm._node_incoming_links

        print(f"  [Signals] {len(signal_node_ids):,} traffic signal nodes in OSM")

        for nid in sorted(signal_node_ids):
            links = incoming.get(nid, [])
            if not links:
                self.stats["signals_skipped_no_approach"] += 1
                continue

            # Deduplicate (shouldn't happen, but safety)
            links = list(dict.fromkeys(links))

            actor = self._build_signal_actor(signal_serial, nid, links)
            self._writer.add(actor)
            signal_serial += 1

            if signal_serial % 1000 == 0:
                print(f"        … {signal_serial:,} signals written", end="\r")

        result = self._writer.close()
        self.stats["traffic_signals"] = result["total"]
        self.stats["files_traffic_signals"] = result["files"]
        print(f"  [Signals] {result['total']:,} traffic signals generated "
              f"({self.stats.get('signals_skipped_no_approach', 0):,} without incoming links skipped)")

    def _build_signal_actor(self, serial: int, node_id: str,
                            incoming_link_ids: List[str]) -> Dict:
        return build_signal_actor(self.cfg, self.ids, self._rng, node_id, incoming_link_ids)


# ============================================================================
# CITY MAP (streaming writer for potentially large graph)
# ============================================================================

class CityMapWriter:
    """Writes city_map.json incrementally (nodes + edges arrays)."""

    def __init__(self, path: Path, ids: ScenarioIdRegistry):
        self.ids = ids
        self._path = path
        self._f = open(path, "w")
        self._f.write('{"nodes":[')
        self._first_node = True
        self._nodes_done = False

    def add_node(self, nid: str, lat: float, lon: float) -> None:
        entry = json.dumps({
            "id":         self.ids.id_for("node", nid),
            "classType":  "hybrid.actor.Node",
            "resourceId": self.ids.id_for("node", nid),
            "latitude":   lat,
            "longitude":  lon,
        }, separators=(",", ":"))
        if not self._first_node:
            self._f.write(",")
        self._f.write(entry)
        self._first_node = False

    def _start_edges(self) -> None:
        if not self._nodes_done:
            self._f.write('],"edges":[')
            self._nodes_done = True
            self._first_edge = True

    def add_edge(self, lid: str, from_id: str, to_id: str,
                 length: float, speed: float,
                 class_type: str = "hybrid.actor.Link") -> None:
        self._start_edges()
        sanitized_length = _sanitize(length, 1.0)
        entry = json.dumps({
            "source_id":  self.ids.id_for("node", from_id),
            "target_id":  self.ids.id_for("node", to_id),
            "weight":     sanitized_length,
            "label": {
                "id":         self.ids.id_for("link", lid),
                "resourceId": self.ids.id_for("link", lid),
                "classType":  class_type,
                "length":     sanitized_length,
            },
        }, separators=(",", ":"))
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
# MAIN GENERATOR
# ============================================================================

class HybridInputGenerator:
    """Orchestrates the full conversion pipeline."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stats: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        cfg.validate()
        network_settings = {"bbox": list(cfg.bbox) if cfg.bbox else None,
                            "micro_ratio": cfg.micro_ratio, "micro_strategy": cfg.micro_strategy,
                            "random_seed": cfg.random_seed}
        network_config = cfg.output_dir / "data" / "network_config.json"
        if cfg.skip_network:
            if not network_config.exists() or json.loads(network_config.read_text()) != network_settings:
                raise ValueError("--skip-network requires a matching network_config.json; regenerate in a fresh scenario")
        elif cfg.output_dir.exists() and any(cfg.output_dir.iterdir()):
            raise FileExistsError(f"Output is not empty: {cfg.output_dir}; use a fresh scenario name or --skip-network")
        if cfg.input_format == "eqasim":
            for name in ("persons.csv", "activities.gpkg", "trips.csv"):
                if not (cfg.input_dir / name).is_file():
                    raise FileNotFoundError(cfg.input_dir / name)
        elif not any((cfg.input_dir / name).is_file() for name in
                     ([cfg.matsim_plans_file] if cfg.matsim_plans_file else ["output_plans.xml.gz", "output_plans.xml"])):
            raise FileNotFoundError("MATSim plans missing; check --matsim-plans")
        if cfg.include_transit:
            for provider in cfg.transit_providers:
                GtfsTransitConverter(cfg, None, cfg.output_dir, ScenarioIdRegistry())._active_services(cfg.input_dir / provider)
        skipping = cfg.skip_network
        n_steps = (
            (2 if not skipping else 0)
            + 2
            + (1 if cfg.include_transit else 0)
            + (1 if cfg.include_rail else 0)
            + (1 if cfg.include_signals else 0)
        )
        step = 0

        print("=" * 70)
        print(f"  EQAsim São Paulo → Hybrid Simulator Input Generator")
        print(f"  Scenario     : {cfg.scenario_name}")
        print(f"  Sample       : {cfg.sample_rate * 100:.1f}%")
        if cfg.bbox is not None:
            lat_mn, lon_mn, lat_mx, lon_mx = cfg.bbox
            print(f"  Bbox         : lat=[{lat_mn}, {lat_mx}], lon=[{lon_mn}, {lon_mx}]")
        print(f"  Bus Transit  : {'yes' if cfg.include_transit else 'no'}")
        print(f"  Rail Transit : {'yes' if cfg.include_rail else 'no'}")
        print(f"  Signals      : {'yes' if cfg.include_signals else 'no'}")
        print(f"  Skip network : {'yes (reusing nodes/links/city_map)' if skipping else 'no'}")
        print(f"  Output       : {cfg.output_dir}")
        print("=" * 70)

        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        data_dir = cfg.output_dir / "data"
        data_dir.mkdir(exist_ok=True)
        ids = ScenarioIdRegistry()

        if skipping:
            (cfg.output_dir / "simulation.json").unlink(missing_ok=True)
            # ── Rebuild OSM state from existing output files ──
            print(f"\n[rebuild] Loading network from existing output files …")
            ids.load(data_dir / "id_registry.json")
            osm = OsmNetworkParser.rebuild_from_output(cfg, data_dir, ids)
            self.stats.update(osm.stats)
            osm_file = None  # not needed

            _REGEN_PATTERNS = [
                "persons_*.json", "cars_*.json", "bikes_*.json",
                "motorcycles_*.json",
            ]
            _REGEN_PATTERNS += ["bus_stops_*.json", "bus_stations_*.json", "buses_*.json",
                                "subway_stations_*.json", "rail_links_*.json", "traffic_signals_*.json",
                                "transit_map.json", "transit_routes.json"]
            deleted = sum(
                1
                for pat in _REGEN_PATTERNS
                for fp in data_dir.glob(pat)
                if fp.unlink() or True
            )
            if deleted:
                print(f"  Removed {deleted} stale actor file(s)")
        else:
            # ── Step 1: OSM network ──
            step += 1
            print(f"\n[{step}/{n_steps}] Parsing OSM network …")
            osm = OsmNetworkParser(cfg, data_dir, ids)
            osm_file = self._resolve_input(cfg.osm_file) if cfg.osm_file else self._find_osm(cfg.input_dir)
            osm.parse(osm_file)
            ids.save(data_dir / "id_registry.json")
            network_config.write_text(json.dumps(network_settings, indent=2))
            self.stats.update(osm.stats)

            # ── Step 2: City map (streaming, built from same node/link data) ──
            step += 1
            print(f"\n[{step}/{n_steps}] Building city map …")
            cm_path = data_dir / "city_map.json"
            cm = CityMapWriter(cm_path, ids)
            for nid, (lat, lon) in osm._node_latlon.items():
                cm.add_node(nid, lat, lon)
            self._add_edges_to_city_map(cm, data_dir, osm.stats.get("files_links", 0))
            cm.close()
            print(f"  city_map.json written ({cm_path.stat().st_size // 1024:,} KB)")

            # Free heavy OSM data no longer needed (keeps KD-tree for nearest-node)
            osm.free_heavy_data()

        # ── Step 3: Population ──
        step += 1
        print(f"\n[{step}/{n_steps}] Converting population …")
        pop = PopulationConverter(cfg, osm, data_dir, ids)
        pop.convert()
        self.stats.update(pop.stats)
        self.stats["persons"] = pop.stats.get("persons_emitted", 0)

        # ── Step 4: Bus Transit (optional) ──
        step += 1
        gtfs: Optional[GtfsTransitConverter] = None
        if cfg.include_transit:
            print(f"\n[{step}/{n_steps}] Converting GTFS bus transit …")
            gtfs = GtfsTransitConverter(cfg, osm, data_dir, ids)
            gtfs.convert()
            self.stats.update({f"transit_{k}": v for k, v in gtfs.stats.items()})
        else:
            print(f"\n[{step}/{n_steps}] Bus transit skipped (use --transit to enable)")

        # ── Step 5: Rail Transit (optional) ──
        rail_conv: Optional[RailTransitConverter] = None
        if cfg.include_rail:
            step += 1
            print(f"\n[{step}/{n_steps}] Extracting rail/subway from OSM …")
            rail_file = self._resolve_input(cfg.rail_osm_file) if cfg.rail_osm_file else osm_file
            if rail_file is None:
                rail_file = self._resolve_input(cfg.osm_file) if cfg.osm_file else self._find_osm(cfg.input_dir)
            rail_ext = OsmRailExtractor(cfg)
            rail_ext.extract(rail_file)
            self.stats["rail_incomplete_relations_excluded"] = sum(bool(r.get("missing_station_nodes")) for r in rail_ext.route_relations)
            self.stats["rail_distance_method"] = "projected straight-line station separation (approximation)"
            print(f"  Converting to simulator format …")
            rail_conv = RailTransitConverter(cfg, rail_ext, osm, data_dir, ids)
            rail_conv.convert()
            self.stats.update({f"rail_{k}": v for k, v in rail_conv.stats.items()})

        if cfg.include_transit and not gtfs.stats.get("buses", 0):
            raise ValueError("GTFS selection produced no routed buses; check date, bbox, stop snapping and feed")
        if cfg.include_rail and not rail_conv.stats.get("rail_links", 0):
            raise ValueError("OSM selection produced no rail links; include complete route relation members")
        # ── Transit map (lightweight index for mode choice) ──
        if gtfs is not None or rail_conv is not None:
            n_entries = self._write_transit_map(data_dir, gtfs, rail_conv)
            self.stats["transit_map_entries"] = n_entries
            print(f"  transit_map.json written ({n_entries:,} stops)")

        # ── Transit routes (full stop sequences for RAPTOR) ──
        if gtfs is not None or rail_conv is not None:
            n_routes = self._write_transit_routes(data_dir, gtfs, rail_conv)
            self.stats["transit_route_entries"] = n_routes
            print(f"  transit_routes.json written ({n_routes:,} routes)")

        # ── Step 6: Traffic Signals (optional) ──
        if cfg.include_signals:
            step += 1
            print(f"\n[{step}/{n_steps}] Generating traffic signals …")
            sig_gen = TrafficSignalGenerator(cfg, osm, data_dir, ids)
            sig_gen.generate()
            self.stats.update({f"signal_{k}": v for k, v in sig_gen.stats.items()})

        ids.save(data_dir / "id_registry.json")
        (cfg.output_dir / "effective_config.json").write_text(json.dumps(asdict(cfg), default=str, indent=2))
        # ── Write config files ──
        print("  Writing scenario_metadata.json …")
        self._write_metadata()

        print("  Writing GENERATION_REPORT.md …")
        self._write_report()

        if cfg.output_format == "sqlite":
            print("\n  Converting data/ actor + city-map JSON to SQLite …")
            self._convert_data_dir_to_sqlite(data_dir)

        print("\n  Writing simulation.json …")
        self._write_simulation_json(data_dir)
        print("\n" + "=" * 70)
        print("  Done!")
        self._print_summary()
        print("=" * 70)

    # ------------------------------------------------------------------
    def _resolve_input(self, name: str) -> Path:
        path = Path(name)
        return path if path.is_absolute() else self.cfg.input_dir / path

    @staticmethod
    def _find_osm(input_dir: Path) -> Path:
        for candidate in [
            input_dir / "sao_paulo.osm.gz",
            input_dir / "sao_paulo.osm",
        ]:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"No OSM file found in {input_dir}. "
            "Expected sao_paulo.osm.gz or sao_paulo.osm"
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _add_edges_to_city_map(
        cm: CityMapWriter, data_dir: Path, num_link_files: int
    ) -> None:
        """Scan the already-written link JSON files and add edges to city_map."""
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
                to_id   = rev_node[c["to"]]
                cm.add_edge(lid, from_id, to_id, c["length"], c["speedLimit"])
            del links
            gc.collect()

    # ------------------------------------------------------------------
    _ACTOR_JSON_GLOBS = [
        "nodes_*.json", "links_*.json", "persons_*.json", "cars_*.json",
        "bikes_*.json", "motorcycles_*.json", "bus_stops_*.json",
        "bus_stations_*.json", "buses_*.json", "subway_stations_*.json",
        "rail_links_*.json", "traffic_signals_*.json",
    ]

    @classmethod
    def _convert_data_dir_to_sqlite(cls, data_dir: Path) -> None:
        """Convert every data/*.json actor-simulation file (and city_map.json) to
        the equivalent SQLite .db, deleting the source JSON once the conversion
        succeeds. Reuses tools/scenario-db-converter/convert.py — the same
        producer SqliteLoadData.scala/CityMapUtil.scala already read — instead of
        re-implementing the JSON→SQLite schema here.

        On conversion failure the .json is left in place (and any partial .db
        removed) so a re-run doesn't need to regenerate the data from scratch.
        """
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
        # DataSourceTypeEnum case names are literally "json"/"sqlite" (see
        # core.enumeration.DataSourceTypeEnum in hyperbolic-time-chamber), so
        # cfg.output_format doubles as the sourceType value.
        ext = "db" if cfg.output_format == "sqlite" else "json"

        def _static(fid: str, cls: str, base_name: str) -> Dict:
            return {
                "id": fid, "classType": cls,
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": "EAGER",
                "entityLifecycle": "STATIC",
                "dataSource": {"sourceType": cfg.output_format,
                               "info": {"path": f"{base}/{base_name}.{ext}"}},
            }

        def _dynamic(fid: str, cls: str, base_name: str) -> Dict:
            return {
                "id": fid, "classType": cls,
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": "PROGRESSIVE",
                "entityLifecycle": "DYNAMIC",
                "dataSource": {"sourceType": cfg.output_format,
                               "info": {"path": f"{base}/{base_name}.{ext}"}},
            }

        # Nodes
        for i in range(self.stats.get("files_nodes", 0)):
            sources.append(_static(f"nodes_{i}", "hybrid.actor.Node", f"nodes_{i}"))

        # Links
        for i in range(self.stats.get("files_links", 0)):
            sources.append(_static(f"links_{i}", "hybrid.actor.Link", f"links_{i}"))

        # Persons
        for i in range(self.stats.get("files_persons", 0)):
            sources.append(_dynamic(f"persons_{i}", "hybrid.actor.Person", f"persons_{i}"))

        # Cars
        for i in range(self.stats.get("files_cars", 0)):
            sources.append(_dynamic(f"cars_{i}", "hybrid.actor.Car", f"cars_{i}"))

        # Bikes
        for i in range(self.stats.get("files_bikes", 0)):
            sources.append(_dynamic(f"bikes_{i}", "hybrid.actor.Bicycle", f"bikes_{i}"))

        for i in range(self.stats.get("files_motorcycles", 0)):
            sources.append(_dynamic(f"motorcycles_{i}", "hybrid.actor.Motorcycle", f"motorcycles_{i}"))

        if cfg.include_transit:
            # Bus stops (static infrastructure)
            for i in range(self.stats.get("transit_files_bus_stops", 0)):
                sources.append(_static(f"bus_stops_{i}", "hybrid.actor.BusStop",
                                       f"bus_stops_{i}"))
            # Bus stations (static infrastructure)
            for i in range(self.stats.get("transit_files_bus_stations", 0)):
                sources.append(_static(f"bus_stations_{i}", "hybrid.actor.BusStation",
                                       f"bus_stations_{i}"))
            # Buses (dynamic)
            for i in range(self.stats.get("transit_files_buses", 0)):
                sources.append(_dynamic(f"buses_{i}", "hybrid.actor.Bus", f"buses_{i}"))

        if cfg.include_rail:
            # Subway stations (static infrastructure)
            for i in range(self.stats.get("rail_files_subway_stations", 0)):
                sources.append(_static(
                    f"subway_stations_{i}", "hybrid.actor.SubwayStation",
                    f"subway_stations_{i}"))
            # Rail links (static infrastructure)
            for i in range(self.stats.get("rail_files_rail_links", 0)):
                sources.append(_static(
                    f"rail_links_{i}", "hybrid.actor.RailLink",
                    f"rail_links_{i}"))

        if cfg.include_signals:
            # Traffic signals (static infrastructure)
            for i in range(self.stats.get("signal_files_traffic_signals", 0)):
                sources.append(_static(
                    f"traffic_signals_{i}", "hybrid.actor.TrafficSignal",
                    f"traffic_signals_{i}"))

        return sources

    # ------------------------------------------------------------------
    def _write_simulation_json(self, data_dir: Path) -> None:
        cfg = self.cfg
        actor_sources = self._build_actor_sources(data_dir)
        base = f"{cfg.docker_base}/{cfg.scenario_name}/data"

        config = {
            "id":             cfg.scenario_name,
            "name":           f"São Paulo EQAsim – {cfg.scenario_name}",
            "description":    (
                f"Generated from EQAsim São Paulo scenario. "
                f"Sample: {cfg.sample_rate*100:.1f}%. "
                f"Bus Transit: {'yes' if cfg.include_transit else 'no'}. "
                f"Rail Transit: {'yes' if cfg.include_rail else 'no'}. "
                f"Signals: {'yes' if cfg.include_signals else 'no'}."
            ),
            "startTick":      0,
            "endTick":        cfg.duration,
            "startRealTime":  cfg.start_real_time,
            "timeUnit":       "seconds",
            "timeStep":       1,
            "duration":       cfg.duration,
            "tickDuration":   1.0,
            "randomSeed":     cfg.random_seed,
            "enableDynamicModeChoice": cfg.enable_dynamic_mode_choice,
            "dynamicActorTypes": ["hybrid.actor.Subway"] if cfg.include_rail else [],
            "actorsDataSources": actor_sources,
            "cityMapFile":    f"{base}/city_map.{'db' if cfg.output_format == 'sqlite' else 'json'}",
            "warmupTargets":  ["org.interscity.htc.model.hybrid.util.CityMapUtil#warmUp"],
            "postLoadRegistrationClasses": self._post_load_registration_classes(),
        }
        if (data_dir / "transit_map.json").exists():
            config["transitMapFile"] = f"{base}/transit_map.json"
        if (data_dir / "transit_routes.json").exists():
            config["transitRoutesFile"] = f"{base}/transit_routes.json"
        out = cfg.output_dir / "simulation.json"
        with open(out, "w") as f:
            json.dump(config, f, separators=(",", ":"))

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    @staticmethod
    def _write_transit_routes(
        data_dir: Path,
        gtfs: Optional["GtfsTransitConverter"],
        rail_conv: Optional["RailTransitConverter"] = None,
    ) -> int:
        """Write transit_routes.json for the RAPTOR algorithm.

        Returns the number of route entries written.
        """
        entries: List[Dict] = gtfs.transit_route_entries() if gtfs else []
        if rail_conv:
            entries.extend(rail_conv.transit_route_entries())
        out = data_dir / "transit_routes.json"
        with open(out, "w") as f:
            json.dump(entries, f, separators=(",", ":"))
        return len(entries)

    # ------------------------------------------------------------------
    @staticmethod
    def _write_transit_map(
        data_dir: Path,
        gtfs: Optional["GtfsTransitConverter"],
        rail_conv: Optional["RailTransitConverter"],
    ) -> int:
        """Merge bus-stop and subway-station entries into transit_map.json.

        Returns the total number of entries written.
        """
        entries: List[Dict] = []
        if gtfs is not None:
            entries.extend(gtfs.transit_map_entries())
        if rail_conv is not None:
            entries.extend(rail_conv.transit_map_entries())
        out = data_dir / "transit_map.json"
        with open(out, "w") as f:
            json.dump(entries, f, separators=(",", ":"))
        return len(entries)

    # ------------------------------------------------------------------
    def _post_load_registration_classes(self) -> List[str]:
        """Actor classes that must register with other actors after loading.

        BusStop registers with its Node (so the node knows a bus stop exists).
        SubwayStation registers with its Node (so the node knows a station exists).
        BusStation registers with its Nodes for route resolution.
        """
        classes: List[str] = [
            "org.interscity.htc.model.hybrid.actor.BusStop",
            "org.interscity.htc.model.hybrid.actor.BusStation",
            "org.interscity.htc.model.hybrid.actor.SubwayStation",
        ]
        return classes

    # ------------------------------------------------------------------
    def _write_metadata(self) -> None:
        cfg = self.cfg
        snapshots = []
        for path in sorted(cfg.input_dir.glob("*")):
            if path.is_file():
                stat = path.stat()
                snapshots.append({"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        provenance = {}
        for provider in cfg.transit_providers if cfg.include_transit else []:
            path = cfg.input_dir / provider / "source.json"
            if path.exists():
                provenance[provider] = json.loads(path.read_text())
        meta = {
            "inputSnapshots": snapshots,
            "downloadProvenance": provenance,
            "name":        cfg.scenario_name,
            "description": "Generated from EQAsim São Paulo scenario",
            "generated":   datetime.now().isoformat(),
            "version":     "2.0.0",
            "source": {
                "directory": str(cfg.input_dir),
                "format":    cfg.input_format,
                "populationCrs": cfg.population_crs or self.stats.get("population_crs"),
                "gtfsServiceDate": cfg.service_date,
            },
            "target": {
                "directory": str(cfg.output_dir),
                "model":     "Hybrid (MICRO/MESO)",
            },
            "statistics": {k: int(v) if isinstance(v, (int, np.integer)) else v
                           for k, v in self.stats.items()},
            "configuration": {
                "sampleRate":        cfg.sample_rate,
                "microLinkRatio":    cfg.micro_ratio,
                "microStrategy":     cfg.micro_strategy,
                "includeTransit":    cfg.include_transit,
                "includeRail":       cfg.include_rail,
                "includeSignals":    cfg.include_signals,
                "signalCycleDuration": cfg.signal_cycle_duration,
                "itemsPerFile":      cfg.items_per_file,
                "randomSeed":        cfg.random_seed,
                "maxBusesPerRoute":  cfg.max_buses_per_route,
                "maxTrainsPerLine":  cfg.max_trains_per_line,
                "trainCapacity":     cfg.train_capacity,
                "trainInterval":     cfg.train_interval,
                "unsupportedMode": cfg.unsupported_mode,
                "maxSnapDistanceM": cfg.max_snap_distance,
            },
        }
        out = cfg.output_dir / "scenario_metadata.json"
        with open(out, "w") as f:
            json.dump(meta, f, indent=2)

    # ------------------------------------------------------------------
    def _write_report(self) -> None:
        cfg = self.cfg
        lines = [f"# Generation report: {cfg.scenario_name}", "",
                 f"Input format: `{cfg.input_format}`. Seed: `{cfg.random_seed}`. Sample: `{cfg.sample_rate}`.",
                 f"Clock: one second per tick; inclusive horizon: `{cfg.duration}`.",
                 f"GTFS service date: `{cfg.service_date}` (agency local day).",
                 f"Unresolved mode policy: `{cfg.unsupported_mode}`.", "",
                 "## Modeling assumptions", "",
                 "- Native Person plans use Activity, WalkLeg, PrivateVehicleLeg and explicitly requested PendingDecision elements.",
                 "- Replanning changes the source trip mode/itinerary; it is not MATSim transit-route preservation.",
                 "- GTFS controls bus activation times and ordered stop patterns. HTC buses circulate on a closed road path until the horizon; stop-time arrival adherence and trip-end retirement are not implemented by this converter.",
                 "- Frequency exact_times=0 windows use a deterministic regular headway approximation.",
                 "- Bus/train counts are not scaled with the sampled population. Review fleet caps before scientific use.",
                 "- Rail distances are projected straight-line station separations, with the existing 100 m floor; tracks are not followed geometrically.",
                 "- Distinct same-name terminal positions may use counted synthetic rail joins within max_terminal_gap_m (default 250 m); they are not surveyed track geometry.",
                 "- OSM rail headways, speeds, capacities and inferred reverse service are scenario assumptions, not measured operating timetables.",
                 "- Rail garages activate their first train after one configured interval; train routes include a return path for physical continuity.",
                 "- The road graph does not model OSM turn restrictions or every access restriction. Snapping uses one directed connected cohort, not mode-specific access networks.",
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
            print(f"  Transit : {s.get('transit_bus_stops', 0):,} stops, "
                  f"{s.get('transit_bus_stations', 0):,} stations, "
                  f"{s.get('transit_buses', 0):,} buses")
        if self.cfg.include_rail:
            print(f"  Rail    : {s.get('rail_subway_stations', 0):,} stations, "
                  f"{s.get('rail_rail_links', 0):,} rail links, "
                  f"{s.get('rail_subway_trains', 0):,} trains")
        if s.get('transit_map_entries'):
            print(f"  Transit map: {s.get('transit_map_entries', 0):,} stops indexed for mode choice")
        if self.cfg.include_signals:
            print(f"  Signals : {s.get('signal_traffic_signals', 0):,} traffic signals")
        print(f"  Output  : {self.cfg.output_dir}")


# ============================================================================
# CLI
# ============================================================================

def main() -> None:
    p = argparse.ArgumentParser(
        description="Generate Hybrid Simulator input from EQAsim São Paulo data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test: 0.1 % sample, no transit, output to ./output/sp_test
  python generate_hybrid_input.py --scenario sp_test --sample 0.001

  # 1 % with bus transit
  python generate_hybrid_input.py --scenario sp_1pct --sample 0.01 --transit

  # 1 % with rail (subway/train from OSM)
  python generate_hybrid_input.py --scenario sp_1pct --sample 0.01 --rail

  # Full transit: buses + rail
  python generate_hybrid_input.py --scenario sp_full --sample 0.05 --transit --rail

  # 5 % with 20 % MICRO links, output to simulator input folder
  python generate_hybrid_input.py --scenario sp_5pct --sample 0.05 \\
      --micro-ratio 0.2 --micro-strategy highway \\
      --output /home/dean/PhD/hyperbolic-time-chamber/simulations/input

  # Full dataset (no extra sampling on top of EQAsim baseline)
  python generate_hybrid_input.py --scenario sp_full --sample 1.0 --transit --rail
""",
    )

    p.add_argument("--config", type=Path, help="JSON configuration using CLI option names; CLI overrides file")
    p.add_argument("--population-crs", help="Source CRS; MATSim requires it, EQASim defaults to GeoPackage CRS")
    p.add_argument("--service-date", help="GTFS service date YYYY-MM-DD in agency local time")
    p.add_argument("--unsupported-mode", choices=["error", "walk", "skip-person", "replan"], default="error")
    p.add_argument("--declare-ownership", action=argparse.BooleanOptionalAction, default=True,
                   help="Write owner/part_of on each private vehicle (partial-simulation filter) [default: on]")
    p.add_argument("--max-snap-distance", type=float, default=1000.0, help="Maximum population/stop snap distance (metres)")
    p.add_argument("--osm-file", help="Road OSM XML or XML.gz path, relative to input-dir")
    p.add_argument("--rail-osm-file", help="Separate OSM XML/XML.gz railway extract, relative to input-dir")
    p.add_argument("--duration", type=int, default=86400, help="Inclusive horizon in seconds (one-second ticks)")
    p.add_argument("--start-real-time", default=None, help="Local simulation origin ISO datetime; defaults to service-date midnight")
    p.add_argument("--scenario",   required=True,
                   help="Scenario name (used as output directory name)")
    p.add_argument("--input-dir",  default="input",
                   help="EQAsim input directory [default: ./input]")
    p.add_argument("--output",     default="output",
                   help="Parent output directory [default: ./output]")
    p.add_argument("--input-format", choices=["eqasim", "matsim"], default="eqasim",
                   help=(
                       "eqasim: persons.csv/activities.gpkg/trips.csv (default). "
                       "matsim: parse raw MATSim output_plans.xml(.gz) directly."
                   ))
    p.add_argument("--matsim-plans", metavar="FILE", default=None,
                   help=(
                       "Path to MATSim output_plans.xml or output_plans.xml.gz "
                       "[default: <input-dir>/output_plans.xml.gz, falling back to "
                       "output_plans.xml]. Only used with --input-format matsim."
                   ))
    p.add_argument("--output-format", choices=["json", "sqlite"], default="json",
                   help=(
                       "json: split *_N.json actor files (default). "
                       "sqlite: convert each *_N.json / city_map.json to an equivalent "
                       "*_N.db / city_map.db via tools/scenario-db-converter and delete "
                       "the source JSON. Requires the 'ijson' package."
                   ))
    p.add_argument("--sample",     type=float, default=0.01,
                   help="Fraction of persons to simulate [default: 0.01]")
    p.add_argument("--transit",    action=argparse.BooleanOptionalAction, default=False,
                   help="Include public transit (SPTrans + EMTU)")
    p.add_argument("--transit-providers", nargs="+", default=["sptrans", "emtu"],
                   metavar="DIR",
                   help="GTFS provider sub-directories [default: sptrans emtu]")
    p.add_argument("--max-buses-per-route", type=int, default=10,
                   help="Max bus actor instances per GTFS route across patterns; 0=all trips [default: 10]")
    p.add_argument("--rail",       action=argparse.BooleanOptionalAction, default=False,
                   help="Include rail transit (subway/train/monorail from OSM)")
    p.add_argument("--signals",    action=argparse.BooleanOptionalAction, default=False,
                   help="Include traffic signals from OSM (highway=traffic_signals)")
    p.add_argument("--signal-cycle", type=int, default=120,
                   help="Default traffic signal cycle duration in seconds [default: 120]")
    p.add_argument("--rail-types", nargs="+", choices=["subway", "rail", "light_rail", "monorail"], default=["subway", "rail", "light_rail", "monorail"])
    p.add_argument("--rail-lines-config", metavar="FILE", default=None,
                   help="JSON file with per-line rail overrides (headway, capacity, velocity, …)")
    p.add_argument("--max-trains-per-line", type=int, default=6,
                   help="Train dispatch cap per line/direction; 0=all within horizon [default: 6]")
    p.add_argument("--train-capacity", type=int, default=1200,
                   help="Passenger capacity per train [default: 1200]")
    p.add_argument("--train-interval", type=int, default=300,
                   help="Default headway in seconds between trains [default: 300]")
    p.add_argument("--micro-ratio",    type=float, default=0.0,
                   help="Fraction of links as MICRO [default: 0.0 = full MESO]")
    p.add_argument("--micro-strategy", choices=["random", "highway", "arterial"],
                   default="highway",
                   help="How to choose MICRO links [default: highway]")
    p.add_argument("--items-per-file", type=int, default=50_000,
                   help="Max items per JSON split file [default: 50000]")
    p.add_argument("--seed",       type=int, default=42,
                   help="Random seed [default: 42]")
    p.add_argument("--docker-base",
                   default="/app/hyperbolic-time-chamber/simulations/input",
                   help="Base path used inside Docker for simulation.json references")
    p.add_argument("--skip-network", action=argparse.BooleanOptionalAction, default=False,
                   help=(
                       "Skip OSM network parsing and city_map generation. "
                       "Reuses existing nodes_*.json / links_*.json / city_map.json "
                       "and only regenerates population, transit and signal actors. "
                       "Requires a previous full run in the same output directory."
                   ))
    p.add_argument("--bbox",
                   default=None,
                   metavar="LAT_MIN,LON_MIN,LAT_MAX,LON_MAX",
                   help=(
                       "Restrict the OSM network and population to this WGS-84 bounding box "
                       "(comma-separated: lat_min,lon_min,lat_max,lon_max). "
                       "Persons with any activity outside the box are excluded. "
                       "Example (SP city centre): -23.57,-46.66,-23.53,-46.62"
                   ))

    # ── Driver attributes ──────────────────────────────────────────────
    p.add_argument("--driver-aggressiveness-min", type=float, default=0.3,
                   metavar="F", help="Driver aggressiveness lower bound [0.3]")
    p.add_argument("--driver-aggressiveness-max", type=float, default=0.9,
                   metavar="F", help="Driver aggressiveness upper bound [0.9]")
    p.add_argument("--driver-reaction-time-min",  type=float, default=0.8,
                   metavar="F", help="Reaction time lower bound in seconds [0.8]")
    p.add_argument("--driver-reaction-time-max",  type=float, default=1.2,
                   metavar="F", help="Reaction time upper bound in seconds [1.2]")
    p.add_argument("--driver-speed-factor-min",   type=float, default=0.9,
                   metavar="F", help="Max-speed-factor lower bound [0.9]")
    p.add_argument("--driver-speed-factor-max",   type=float, default=1.1,
                   metavar="F", help="Max-speed-factor upper bound [1.1]")
    p.add_argument("--driver-gap-factor-min",     type=float, default=0.8,
                   metavar="F", help="Min-gap-factor lower bound [0.8]")
    p.add_argument("--driver-gap-factor-max",     type=float, default=1.2,
                   metavar="F", help="Min-gap-factor upper bound [1.2]")

    # ── Dynamic mode choice ────────────────────────────────────────────
    p.add_argument("--dynamic-mode-choice", action=argparse.BooleanOptionalAction, default=False,
                   help=(
                       "Emit a PendingDecision plan element (instead of a fixed leg) for "
                       "every trip, resolved at runtime by the ModeDecisionEngine named "
                       "by --mc-strategy"
                   ))
    p.add_argument("--mc-strategy", choices=["raptor", "nearest-stop-utility", "travel-time"],
                   default="nearest-stop-utility",
                   help=(
                       "ModeDecisionEngine id (must match a registered engine exactly — "
                       "see ModeDecisionEngineRegistry) [default: nearest-stop-utility]. "
                       "Only 'travel-time' considers car/bicycle/motorcycle as candidates."
                   ))
    p.add_argument("--mc-modes", nargs="+",
                   default=["bus", "subway", "walk"],
                   metavar="MODE",
                   help="Modes evaluated by the strategy [default: bus subway walk]")
    p.add_argument("--mc-beta-mode",             type=float, default=1.0,
                   metavar="F", help="Scale for mode-preference score [1.0]")
    p.add_argument("--mc-beta-access",           type=float, default=0.001,
                   metavar="F", help="Per-metre penalty for PT access leg [0.001]")
    p.add_argument("--mc-beta-egress",           type=float, default=0.001,
                   metavar="F", help="Per-metre penalty for PT egress leg [0.001]")
    p.add_argument("--mc-beta-private",          type=float, default=0.00005,
                   metavar="F", help="Per-metre penalty for private vehicle O→D [0.00005]")
    p.add_argument("--mc-pref-subway",           type=float, default=2.0,
                   metavar="F", help="ASC for subway [2.0]")
    p.add_argument("--mc-pref-bus",              type=float, default=1.0,
                   metavar="F", help="ASC for bus [1.0]")
    p.add_argument("--mc-pref-walk",             type=float, default=0.0,
                   metavar="F", help="ASC for walking [0.0]")
    p.add_argument("--mc-pref-car",              type=float, default=3.0,
                   metavar="F", help="ASC for car [3.0]")
    p.add_argument("--mc-pref-bicycle",          type=float, default=1.5,
                   metavar="F", help="ASC for bicycle [1.5]")
    p.add_argument("--mc-pref-motorcycle",       type=float, default=2.0,
                   metavar="F", help="ASC for motorcycle [2.0]")
    p.add_argument("--mc-max-access-dist",       type=float, default=1500.0,
                   metavar="M", help="Max walk distance to PT stop in metres [1500]")
    p.add_argument("--mc-max-walk-dist",         type=float, default=2000.0,
                   metavar="M", help="Max O→D distance to offer walk as mode in metres [2000]")
    p.add_argument("--mc-beta-travel-time",      type=float, default=0.01,
                   metavar="F", help="(travel-time) Per-second travel-time penalty [0.01]")
    p.add_argument("--mc-walking-speed",         type=float, default=1.4,
                   metavar="MS", help="(travel-time) Walking speed m/s [1.4]")
    p.add_argument("--mc-avg-bus-speed",         type=float, default=8.33,
                   metavar="MS", help="(travel-time) Average bus speed m/s [8.33]")
    p.add_argument("--mc-avg-subway-speed",      type=float, default=12.5,
                   metavar="MS", help="(travel-time) Average subway speed m/s [12.5]")
    p.add_argument("--mc-avg-bicycle-speed",     type=float, default=5.56,
                   metavar="MS", help="(travel-time) Average bicycle speed m/s [5.56]")
    p.add_argument("--mc-avg-motorcycle-speed",  type=float, default=11.11,
                   metavar="MS", help="(travel-time) Average motorcycle speed m/s [11.11]")
    p.add_argument("--mc-avg-car-speed",         type=float, default=13.89,
                   metavar="MS", help="(travel-time) Average car speed m/s [13.89]")

    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", type=Path)
    config_path = preliminary.parse_known_args()[0].config
    if config_path:
        try:
            raw = json.loads(config_path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("configuration must be a JSON object")
            actions = {a.dest: a for a in p._actions}
            defaults = {}
            for key, value in raw.items():
                dest = key.replace("-", "_")
                if dest not in actions or dest in ("help", "config"):
                    raise ValueError(f"unknown configuration key: {key}")
                action = actions[dest]
                if isinstance(action, (argparse._StoreTrueAction, argparse.BooleanOptionalAction)):
                    if not isinstance(value, bool):
                        raise ValueError(f"{key} must be boolean")
                elif action.nargs == "+":
                    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
                        raise ValueError(f"{key} must be a nonempty array of strings")
                elif action.type:
                    if isinstance(value, (bool, list, dict)) or (action.type is int and (not isinstance(value, int))):
                        raise ValueError(f"invalid value for {key}")
                    value = action.type(value)
                elif not isinstance(value, str):
                    raise ValueError(f"{key} must be a string")
                candidates = value if action.nargs == "+" else [value]
                if action.choices and any(v not in action.choices for v in candidates):
                    raise ValueError(f"{key}: expected one of {action.choices}")
                if dest in ("input_dir", "output", "rail_lines_config"):
                    value = str((config_path.resolve().parent / value).resolve())
                defaults[dest] = value
            p.set_defaults(**defaults)
            if "scenario" in defaults:
                actions["scenario"].required = False
        except (OSError, ValueError, TypeError) as exc:
            p.error(f"--config: {exc}")
    args = p.parse_args()

    input_dir  = Path(args.input_dir).resolve()
    output_dir = Path(args.output).resolve() / args.scenario

    if not input_dir.exists():
        print(f"ERROR: input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    if not 0 < args.sample <= 1.0:
        print("ERROR: --sample must be in (0, 1]", file=sys.stderr)
        sys.exit(1)

    # Parse optional bounding box: "lat_min,lon_min,lat_max,lon_max"
    bbox_val: Optional[Tuple[float, float, float, float]] = None
    if args.bbox is not None:
        try:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("expected 4 values")
            lat_mn, lon_mn, lat_mx, lon_mx = parts
            if not (-90 <= lat_mn < lat_mx <= 90 and -180 <= lon_mn < lon_mx <= 180):
                raise ValueError("lat_min/lon_min must be less than lat_max/lon_max")
            bbox_val = (lat_mn, lon_mn, lat_mx, lon_mx)
            print(f"  Bounding box : lat=[{lat_mn}, {lat_mx}] lon=[{lon_mn}, {lon_mx}]")
        except ValueError as exc:
            print(
                f"ERROR: --bbox must be lat_min,lon_min,lat_max,lon_max — {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    cfg = Config(
        declare_ownership = args.declare_ownership,
        input_dir         = input_dir,
        output_dir        = output_dir,
        scenario_name     = args.scenario,
        sample_rate       = args.sample,
        random_seed       = args.seed,
        micro_ratio       = args.micro_ratio,
        micro_strategy    = args.micro_strategy,
        include_transit   = args.transit,
        transit_providers = args.transit_providers,
        max_buses_per_route = args.max_buses_per_route,
        include_rail       = args.rail,
        rail_types         = args.rail_types,
        include_signals    = args.signals,
        signal_cycle_duration = args.signal_cycle,
        max_trains_per_line = args.max_trains_per_line,
        train_capacity     = args.train_capacity,
        train_interval     = args.train_interval,
        rail_lines_config_file = args.rail_lines_config,
        items_per_file    = args.items_per_file,
        docker_base       = args.docker_base,
        skip_network      = args.skip_network,
        bbox              = bbox_val,
        input_format      = args.input_format,
        matsim_plans_file = args.matsim_plans,
        output_format     = args.output_format,
        population_crs    = args.population_crs,
        service_date      = args.service_date,
        unsupported_mode  = args.unsupported_mode,
        max_snap_distance = args.max_snap_distance,
        osm_file          = args.osm_file,
        rail_osm_file     = args.rail_osm_file,
        duration          = args.duration,
        start_real_time   = args.start_real_time or f"{args.service_date or '2025-01-01'}T00:00:00",
        # driver attributes
        driver_aggressiveness_min   = args.driver_aggressiveness_min,
        driver_aggressiveness_max   = args.driver_aggressiveness_max,
        driver_reaction_time_min    = args.driver_reaction_time_min,
        driver_reaction_time_max    = args.driver_reaction_time_max,
        driver_max_speed_factor_min = args.driver_speed_factor_min,
        driver_max_speed_factor_max = args.driver_speed_factor_max,
        driver_min_gap_factor_min   = args.driver_gap_factor_min,
        driver_min_gap_factor_max   = args.driver_gap_factor_max,
        # dynamic mode choice
        enable_dynamic_mode_choice  = args.dynamic_mode_choice,
        mode_choice_strategy        = args.mc_strategy,
        mc_included_modes           = args.mc_modes,
        mc_beta_mode                = args.mc_beta_mode,
        mc_beta_access              = args.mc_beta_access,
        mc_beta_egress              = args.mc_beta_egress,
        mc_beta_private_vehicle     = args.mc_beta_private,
        mc_mode_pref_subway         = args.mc_pref_subway,
        mc_mode_pref_bus            = args.mc_pref_bus,
        mc_mode_pref_walk           = args.mc_pref_walk,
        mc_mode_pref_car            = args.mc_pref_car,
        mc_mode_pref_bicycle        = args.mc_pref_bicycle,
        mc_mode_pref_motorcycle     = args.mc_pref_motorcycle,
        mc_max_access_distance_m    = args.mc_max_access_dist,
        mc_max_walk_distance_m      = args.mc_max_walk_dist,
        mc_beta_travel_time         = args.mc_beta_travel_time,
        mc_walking_speed_ms         = args.mc_walking_speed,
        mc_avg_bus_speed_ms         = args.mc_avg_bus_speed,
        mc_avg_subway_speed_ms      = args.mc_avg_subway_speed,
        mc_avg_bicycle_speed_ms     = args.mc_avg_bicycle_speed,
        mc_avg_motorcycle_speed_ms  = args.mc_avg_motorcycle_speed,
        mc_avg_car_speed_ms         = args.mc_avg_car_speed,
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
