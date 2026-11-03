#!/usr/bin/env python3
"""Bounded OSM XML → fixed-route HTC MICRO proof scenario with retained road shapes.

This deliberately supports a small road extract, not a general city/turn-restriction importer.
Vehicle demand is synthetic; OSM identities, geometry, direction and tags are retained.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

SUPPORTED_HIGHWAYS = frozenset({"residential", "living_street", "tertiary", "secondary", "unclassified", "service"})


class ProfileError(ValueError):
    def __init__(self, code, entity, message):
        super().__init__(message)
        self.code, self.entity = code, entity


def metres(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1, math.sqrt(h)))


def read_network(path):
    root = ET.parse(path).getroot()
    coordinates = {node.attrib["id"]: [float(node.attrib["lon"]), float(node.attrib["lat"])] for node in root.findall("node")}
    ways = []
    for way in root.findall("way"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        if "highway" not in tags:
            continue
        if tags["highway"] not in SUPPORTED_HIGHWAYS:
            raise ProfileError("UNSUPPORTED_HIGHWAY", f"way/{way.attrib['id']}", "This bounded MICRO importer supports ordinary urban roads only")
        refs = [node.attrib["ref"] for node in way.findall("nd")]
        if len(refs) < 2 or any(ref not in coordinates for ref in refs):
            raise ProfileError("INCOMPLETE_WAY", f"way/{way.attrib['id']}", "OSM ways require all referenced node coordinates")
        ways.append({"id": way.attrib["id"], "refs": refs, "tags": tags})
    if not ways or len(ways) > 100:
        raise ValueError("Select a bounded extract containing 1–100 road ways")
    if root.findall("relation"):
        raise ValueError("Relations/turn restrictions require the general importer and are not silently discarded")
    counts = Counter(ref for way in ways for ref in set(way["refs"]))
    roads, endpoints = [], set()
    for way in ways:
        refs, tags = way["refs"], way["tags"]
        breaks = [0] + [i for i in range(1, len(refs) - 1) if counts[refs[i]] > 1] + [len(refs) - 1]
        oneway = tags.get("oneway", "yes" if tags.get("junction") == "roundabout" else "no")
        if oneway not in {"yes", "1", "true", "-1", "no", "0", "false"}:
            raise ProfileError("UNSUPPORTED_ONEWAY", f"way/{way['id']}", f"Unsupported one-way rule: {oneway}")
        raw_speed = tags.get("maxspeed", "30").lower().strip()
        try:
            speed = float(raw_speed.removesuffix("mph").strip()) * (1.609344 if raw_speed.endswith("mph") else 1)
        except ValueError as exc:
            raise ProfileError("UNSUPPORTED_SPEED", f"way/{way['id']}", f"Explicit speed conversion needed for {raw_speed!r}") from exc
        if not math.isfinite(speed) or speed <= 0:
            raise ProfileError("INVALID_SPEED", f"way/{way['id']}", "Speed must be positive")
        for part, (begin, end) in enumerate(zip(breaks, breaks[1:])):
            section = refs[begin:end + 1]
            shape = [coordinates[ref] for ref in section]
            length = sum(metres(a, b) for a, b in zip(shape, shape[1:]))
            if length < 10 or section[0] == section[-1]:
                raise ProfileError("SHORT_OR_LOOP_LINK", f"way/{way['id']}", f"Segment {part} requires a non-loop link at least 10 m long (measured {length:.3f} m)")
            directions = ["backward"] if oneway == "-1" else ["forward"] if oneway in {"yes", "1", "true"} else ["forward", "backward"]
            for direction in directions:
                ordered = section if direction == "forward" else list(reversed(section))
                identity = f"way-{way['id']}-{part}-{direction}"
                roads.append({"id": identity, "from": ordered[0], "to": ordered[-1], "length": length, "speed": speed,
                    "osmWayId": way["id"], "osmNodeIds": ordered, "name": tags.get("name", ""), "tags": tags,
                    "direction": direction, "coordinates": [coordinates[ref] for ref in ordered]})
                endpoints.update((ordered[0], ordered[-1]))
    return coordinates, roads, endpoints


def convert(source, output, *, cars=16, duration=900):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Choose a new output directory; scenario revisions are immutable")
    if not 1 <= cars <= 1000 or duration <= (cars - 1) * 3:
        raise ValueError("Use 1–1000 cars and a horizon after every synthetic departure")
    coordinates, roads, endpoints = read_network(source)
    platform = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(platform / "tools/sumo_validation"))
    sys.path.insert(0, str(platform / "tools/scenario-db-converter"))
    from scenario.model import Scenario, Node, Link, Vehicle, VehicleType
    from htc_export import export as exporter
    from scenario_output_writer import (shard_and_write_actors, write_city_map_json, build_simulation_json,
        write_simulation_json, build_scenario_metadata, write_scenario_metadata, write_generation_report)

    exporter._IDS = exporter.ScenarioIdRegistry()
    routes = []
    for road in roads:
        onward = next((other for other in roads if other["from"] == road["to"] and other["to"] != road["from"]), None)
        routes.append((road["id"], onward["id"]) if onward else (road["id"],))
    # Exporter serializes Node.y/x as latitude/longitude. Physical lengths are explicit metres.
    scenario = Scenario(output.name, "OSM road geometry with synthetic fixed-route test demand",
        tuple(Node(ref, *coordinates[ref]) for ref in sorted(endpoints)),
        tuple(Link(road["id"], road["from"], road["to"], road["length"], road["speed"], 1) for road in roads),
        (VehicleType(),), tuple(Vehicle(f"car-{i}", i * 3, routes[i % len(routes)]) for i in range(cars)), duration)
    data = output / "data"
    data.mkdir(parents=True)
    city = exporter._build_city_map(scenario)
    actors = exporter._build_nodes(scenario) + exporter._build_links(scenario) + exporter._build_cars(scenario)
    groups = shard_and_write_actors(actors, data)
    write_city_map_json(city, data)
    simulation = build_simulation_json(output.name, scenario.description, scenario.description, groups,
        str(output.parent), end_tick=duration, warmup_targets=["org.interscity.htc.model.hybrid.util.CityMapUtil#warmUp"])
    write_simulation_json(output, simulation)
    metadata = {"kind": "openstreetmap", "name": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors"}
    network = {"schema": "htc.road-network.v1", "mode": "osm", "source": metadata,
        "links": {str(exporter._lid(road["id"])): {key: value for key, value in road.items()
            if key in {"osmWayId", "osmNodeIds", "name", "tags", "direction", "coordinates"}} for road in roads},
        "assumptions": ["Synthetic car demand; not observed traffic", "Single-lane MICRO proof profile; source lane tags retained",
                        "Missing maxspeed defaults to 30 km/h", "No inferred signal timings or unsupported restriction relations"]}
    (data / "road_network.json").write_text(json.dumps(network, indent=2) + "\n")
    (data / "road_source.osm").write_bytes(source.read_bytes())
    write_scenario_metadata(output, build_scenario_metadata(output.name, scenario.description, metadata,
        {"engine": "HTC", "map_mode": "osm"}, {"roads": len(roads), "cars": cars}, {"duration": duration, "assumptions": network["assumptions"]}))
    write_generation_report(output, "Bounded OSM compatibility fixture", str(source.parent), "OSM XML", output.name, groups,
        extra_sections="\n## Assumptions\n\n" + "\n".join(f"- {item}" for item in network["assumptions"]))
    return output / "simulation.json"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cars", type=int, default=16)
    parser.add_argument("--duration", type=int, default=900)
    args = parser.parse_args()
    print(convert(args.source, args.output, cars=args.cars, duration=args.duration))
