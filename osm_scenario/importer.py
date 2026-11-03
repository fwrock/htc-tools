#!/usr/bin/env python3
"""Preview and atomically publish bounded OSM XML/gzip imports for HTC."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ctypes
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET
import sqlite3

PLATFORM = Path(__file__).resolve().parents[2]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))
from tools.osm_scenario.convert import SUPPORTED_HIGHWAYS, convert, read_network
from tools.osm_scenario.extract import extract


@dataclass(frozen=True)
class Options:
    bbox: tuple[float, float, float, float] | None = None
    max_ways: int = 100
    cars: int = 16
    duration: int = 900
    allow_disconnected: bool = False

    def validate(self):
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (self.max_ways, self.cars, self.duration)):
            raise ValueError("max_ways, cars and duration must be integers")
        if not isinstance(self.allow_disconnected, bool):
            raise ValueError("allow_disconnected must be a boolean")
        if not 1 <= self.max_ways <= 100:
            raise ValueError("The bounded MICRO profile accepts max_ways between 1 and 100")
        if not 1 <= self.cars <= 1000 or self.duration <= (self.cars - 1) * 3:
            raise ValueError("Use 1–1000 cars and a horizon after all synthetic departures")
        if self.bbox is not None:
            if not isinstance(self.bbox, (tuple, list)) or len(self.bbox) != 4 or not all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in self.bbox):
                raise ValueError("bbox must contain west,south,east,north as finite degrees")
            west, south, east, north = self.bbox
            if not -180 <= west < east <= 180 or not -85.05112878 <= south < north <= 85.05112878:
                raise ValueError("bbox must be an ordered geographic box within map bounds; antimeridian wrapping is not supported")


def _components(roads):
    adjacency = {}
    for road in roads:
        adjacency.setdefault(road["from"], set()).add(road["to"])
        adjacency.setdefault(road["to"], set()).add(road["from"])
    remaining, components = set(adjacency), []
    while remaining:
        pending, found = [min(remaining)], set()
        while pending:
            identity = pending.pop()
            if identity in found:
                continue
            found.add(identity)
            pending.extend(adjacency[identity] - found)
        remaining -= found
        components.append(sorted(found))
    return sorted(components, key=lambda nodes: (-len(nodes), nodes))


def _prepare(source, options, workspace, progress):
    report = {"schema": "htc.osm-import.v1", "profile": "bounded-standalone-car-micro",
              "source": {"path": str(Path(source).resolve())}, "configuration": asdict(options),
              "diagnostics": [], "published": None}
    snapshot = None
    try:
        snapshot, extracted = extract(source, workspace, bbox=options.bbox, max_ways=options.max_ways,
            supported_highways=SUPPORTED_HIGHWAYS, progress=progress)
        report.update(extracted)
        if not any(item["level"] == "error" for item in report["diagnostics"]):
            _, roads, endpoints = read_network(snapshot)
            components = _components(roads)
            report["network"] = {"directed_links": len(roads), "simulation_nodes": len(endpoints),
                "weak_components": len(components), "component_sizes": [len(nodes) for nodes in components],
                "length_metres": sum(road["length"] for road in roads),
                "source_way_to_directed_links": {identity: sum(road["osmWayId"] == identity for road in roads)
                    for identity in report["selection"]["ways"]}}
            report["geometry_preview"] = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": road["id"], "geometry": {"type": "LineString", "coordinates": road["coordinates"]},
                 "properties": {"osm_way_id": road["osmWayId"], "name": road["name"], "direction": road["direction"],
                    "length_m": road["length"], "speed_limit_kmh": road["speed"], "model_lanes": 1, "source_tags": road["tags"]}}
                for road in roads]}
            if len(components) > 1:
                report["diagnostics"].append({"level": "warning" if options.allow_disconnected else "error",
                    "code": "DISCONNECTED_NETWORK", "entity": "network",
                    "message": f"The selection has {len(components)} disconnected components; refine the area or explicitly allow disconnected demand."})
    except (ValueError, OSError, ET.ParseError, sqlite3.Error) as exc:
        report["diagnostics"].append({"level": "error", "code": getattr(exc, "code", "IMPORT_UNSUPPORTED_OR_INVALID"),
            "entity": getattr(exc, "entity", "source"), "message": str(exc)})
    report["diagnostics"].sort(key=lambda item: (item["level"], item["entity"], item["code"], item["message"]))
    report["status"] = "invalid" if any(item["level"] == "error" for item in report["diagnostics"]) else "ready"
    plan = {key: report.get(key) for key in ("schema", "profile", "configuration", "selection", "network", "diagnostics")}
    plan["source_sha256"] = report["source"].get("sha256")
    report["preview_sha256"] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return report, snapshot


def preview(source, options=Options(), *, progress=None):
    options.validate()
    with tempfile.TemporaryDirectory(prefix="htc-osm-preview-") as directory:
        report, _ = _prepare(source, options, Path(directory), progress)
        return report


def _promote(staging, destination):
    """Linux no-replace rename: atomic directory visibility without clobbering another writer."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Atomic no-clobber publication currently requires Linux; preview remains portable")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise RuntimeError("renameat2 is required for atomic no-clobber publication")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError("The scenario revision already exists; choose a new destination")
        raise OSError(code, os.strerror(code), str(destination))


def publish(source, destination, options=Options(), *, expected_preview=None, progress=None):
    options.validate()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("The scenario revision already exists; choose a new destination")
    if destination.name in ("", ".", ".."):
        raise ValueError("Choose a scenario revision directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Same filesystem as the destination so final promotion is one atomic rename.
    with tempfile.TemporaryDirectory(prefix=".htc-osm-import-", dir=destination.parent) as directory:
        workspace = Path(directory)
        report, snapshot = _prepare(source, options, workspace / "selection", progress)
        if expected_preview and expected_preview != report["preview_sha256"]:
            report["diagnostics"].append({"level": "error", "code": "PREVIEW_CHANGED", "entity": "selection",
                "message": "Source, area, configuration or validation changed after preview; review the new report."})
            report["status"] = "invalid"
        if report["status"] != "ready":
            return report
        emit = progress or (lambda event: None)
        emit({"phase": "compile_package"})
        staging = workspace / "package" / destination.name
        simulation_path = convert(snapshot, staging, cars=options.cars, duration=options.duration)
        network_path = staging / "data/road_network.json"
        network = json.loads(network_path.read_text())
        network["source"]["parent_extract"] = report["source"]
        network["source"]["selection"] = report["selection"]
        network["source"]["preview_sha256"] = report["preview_sha256"]
        network_path.write_text(json.dumps(network, indent=2) + "\n")
        metadata_path = staging / "scenario_metadata.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["source"] = network["source"]
        metadata["configuration"]["import_options"] = asdict(options)
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        from pipeline.experiments.road_network import load_for_scenario
        load_for_scenario(simulation_path)
        # Convert absolute actor/geometry paths only after checking the staged package.
        simulation = json.loads(simulation_path.read_text())
        for actor_source in simulation["actorsDataSources"]:
            original = Path(actor_source["dataSource"]["info"]["path"])
            relative = original.relative_to(staging)
            actor_source["dataSource"]["info"]["path"] = str(destination / relative)
        simulation["cityMapFile"] = str(destination / Path(simulation["cityMapFile"]).relative_to(staging))
        simulation_path.write_text(json.dumps(simulation, indent=2) + "\n")
        report["published"] = {"directory": str(destination), "simulation": str(destination / "simulation.json")}
        report["status"] = "published"
        (staging / "import_report.json").write_text(json.dumps(report, indent=2) + "\n")
        with (staging / "GENERATION_REPORT.md").open("a") as stream:
            stream.write("\n## Import preview\n\nSee `import_report.json` for source fingerprints, whole-way selection, warnings and connectivity.\n")
        emit({"phase": "publish", "destination": str(destination)})
        _promote(staging, destination)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "publish"):
        command = commands.add_parser(name)
        command.add_argument("source", type=Path)
        if name == "publish":
            command.add_argument("destination", type=Path)
            command.add_argument("--expect-preview", help="Require the reviewed preview fingerprint")
        command.add_argument("--bbox", type=lambda value: tuple(float(v) for v in value.split(",")), metavar="WEST,SOUTH,EAST,NORTH")
        command.add_argument("--max-ways", type=int, default=100)
        command.add_argument("--cars", type=int, default=16)
        command.add_argument("--duration", type=int, default=900)
        command.add_argument("--allow-disconnected", action="store_true")
        command.add_argument("--report", type=Path, help="Also save the machine-readable report")
        command.add_argument("--progress", action="store_true", help="Print progress events to stderr")
    args = parser.parse_args()
    options = Options(args.bbox, args.max_ways, args.cars, args.duration, args.allow_disconnected)
    progress = (lambda event: print(json.dumps(event), file=sys.stderr, flush=True)) if args.progress else None
    try:
        if args.command == "preview":
            report = preview(args.source, options, progress=progress)
        else:
            report = publish(args.source, args.destination, options, expected_preview=args.expect_preview, progress=progress)
    except (ValueError, OSError, RuntimeError) as exc:
        report = {"status": "invalid", "published": None,
                  "diagnostics": [{"level": "error", "code": "IMPORT_FAILED", "entity": "configuration", "message": str(exc)}]}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 2 if report["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
