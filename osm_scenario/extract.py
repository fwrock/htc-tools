"""Streaming XML/gzip area selection; node coordinates live in a scratch SQLite index."""
from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import math
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def elements(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as stream:
        parser = ET.iterparse(stream, events=("start", "end"))
        depth, root = 0, None
        for event, element in parser:
            if event == "start":
                depth += 1
                if root is None:
                    root = element
                    if root.tag != "osm":
                        raise ValueError("Expected an OSM XML extract; PBF is not supported by this importer")
            else:
                if depth == 2:
                    yield element
                    root.clear()
                depth -= 1


def intersects(points, bbox):
    """Test complete line segments, including those crossing a box with both endpoints outside."""
    if bbox is None:
        return True
    west, south, east, north = bbox
    if any(west <= x <= east and south <= y <= north for x, y in points):
        return True
    for (x, y), (x2, y2) in zip(points, points[1:]):
        dx, dy = x2 - x, y2 - y
        lo, hi = 0., 1.
        for p, q in ((-dx, x - west), (dx, east - x), (-dy, y - south), (dy, north - y)):
            if p == 0:
                if q < 0:
                    break
            else:
                ratio = q / p
                if p < 0:
                    lo = max(lo, ratio)
                else:
                    hi = min(hi, ratio)
                if lo > hi:
                    break
        else:
            if lo <= hi:
                return True
    return False


def extract(source, workspace, *, bbox, max_ways, supported_highways, progress=None):
    """Retain whole intersecting eligible ways and all their nodes. Never fabricate clipped edges."""
    source, workspace = Path(source), Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    diagnostics, excluded, selected, restrictions = [], Counter(), [], []
    emit = progress or (lambda event: None)
    emit({"phase": "source_fingerprint"})
    source_hash = fingerprint(source)
    statistics = {"source_nodes": 0, "source_ways": 0, "source_relations": 0, "eligible_intersecting_ways": 0}
    database = sqlite3.connect(workspace / "nodes.sqlite")
    try:
        # Rebuildable scratch data only; no user database is modified.
        database.execute("PRAGMA journal_mode=OFF")
        database.execute("PRAGMA synchronous=OFF")
        database.execute("PRAGMA cache_size=-32768")
        database.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, lon REAL NOT NULL, lat REAL NOT NULL)")
        batch = []
        emit({"phase": "index_nodes"})
        for node in elements(source):
            if node.tag != "node":
                continue
            lon, lat = float(node.attrib["lon"]), float(node.attrib["lat"])
            if not math.isfinite(lon) or not math.isfinite(lat) or abs(lon) > 180 or abs(lat) > 90:
                raise ValueError(f"Invalid geographic coordinate at OSM node {node.attrib['id']}")
            batch.append((node.attrib["id"], lon, lat))
            statistics["source_nodes"] += 1
            if len(batch) >= 10000:
                database.executemany("INSERT INTO nodes VALUES (?, ?, ?)", batch)
                batch.clear()
            if statistics["source_nodes"] % 250000 == 0:
                emit({"phase": "index_nodes", "nodes": statistics["source_nodes"]})
        if batch:
            database.executemany("INSERT INTO nodes VALUES (?, ?, ?)", batch)
        database.commit()

        def coordinates(refs):
            found = {}
            for offset in range(0, len(refs), 500):
                chunk = refs[offset:offset + 500]
                if chunk:
                    query = "SELECT id, lon, lat FROM nodes WHERE id IN (" + ",".join("?" for _ in chunk) + ")"
                    found.update({identity: [lon, lat] for identity, lon, lat in database.execute(query, chunk)})
            return found

        emit({"phase": "select_ways"})
        for element in elements(source):
            if element.tag == "relation":
                statistics["source_relations"] += 1
                tags = {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}
                if tags.get("type") == "restriction":
                    restrictions.append((element.attrib["id"], [member.attrib["ref"] for member in element.findall("member") if member.attrib.get("type") == "way"]))
                else:
                    excluded["non_road_relations"] += 1
                continue
            if element.tag != "way":
                continue
            statistics["source_ways"] += 1
            if statistics["source_ways"] % 100000 == 0:
                emit({"phase": "select_ways", "ways": statistics["source_ways"],
                      "selected": statistics["eligible_intersecting_ways"]})
            tags = {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}
            highway = tags.get("highway")
            if highway not in supported_highways:
                if highway:
                    excluded[f"source_highway:{highway}"] += 1
                continue
            refs = [node.attrib["ref"] for node in element.findall("nd")]
            found = coordinates(refs)
            if not refs or any(ref not in found for ref in refs):
                diagnostics.append({"level": "error", "code": "INCOMPLETE_WAY", "entity": f"way/{element.attrib['id']}",
                    "message": "A supported road has missing node coordinates; use a complete extract."})
                continue
            points = [found[ref] for ref in refs]
            if not intersects(points, bbox):
                continue
            identity = element.attrib["id"]
            access = tags.get("motorcar", tags.get("motor_vehicle", tags.get("vehicle", tags.get("access", "yes"))))
            if access in {"no", "private"}:
                excluded["in_area_motor_access_excluded"] += 1
                diagnostics.append({"level": "warning", "code": "ACCESS_EXCLUDED", "entity": f"way/{identity}",
                    "message": f"Road excluded from the car profile: access={access}."})
                continue
            if access not in {"yes", "permissive", "designated"}:
                diagnostics.append({"level": "error", "code": "ACCESS_RULE_UNSUPPORTED", "entity": f"way/{identity}",
                    "message": f"The fixed-route car profile cannot infer permission for access={access}."})
            statistics["eligible_intersecting_ways"] += 1
            if statistics["eligible_intersecting_ways"] > max_ways:
                continue
            for key in tags:
                if key.endswith(":conditional"):
                    diagnostics.append({"level": "error", "code": "CONDITIONAL_RULE_UNSUPPORTED", "entity": f"way/{identity}",
                        "message": f"The current profile cannot execute {key}; the rule is not discarded."})
            for key in ("maxspeed:forward", "maxspeed:backward", "oneway:motor_vehicle", "oneway:motorcar"):
                if key in tags:
                    diagnostics.append({"level": "error", "code": "DIRECTIONAL_RULE_UNSUPPORTED", "entity": f"way/{identity}",
                        "message": f"Explicit conversion support is required for {key}."})
            if "maxspeed" not in tags:
                diagnostics.append({"level": "warning", "code": "DEFAULT_SPEED", "entity": f"way/{identity}",
                    "message": "Missing maxspeed: the bounded profile uses 30 km/h."})
            if tags.get("lanes") != "1":
                diagnostics.append({"level": "warning", "code": "LANE_PROFILE", "entity": f"way/{identity}",
                    "message": "The bounded MICRO profile uses one lane; original source tags are retained."})
            selected.append((identity, ET.tostring(element, encoding="unicode"), refs))
            if any(abs(point[1]) > 85.05112878 for point in points):
                diagnostics.append({"level": "error", "code": "MAP_BOUNDS_UNSUPPORTED", "entity": f"way/{identity}",
                    "message": "Selected road coordinates exceed this viewer's Web Mercator bounds."})
        if statistics["eligible_intersecting_ways"] > max_ways:
            diagnostics.append({"level": "error", "code": "SELECTION_TOO_LARGE", "entity": "selection",
                "message": f"Selected {statistics['eligible_intersecting_ways']} roads; choose a smaller area (limit {max_ways})."})
        selected_ids = {identity for identity, _, _ in selected}
        for identity, members in restrictions:
            if selected_ids.intersection(members):
                diagnostics.append({"level": "error", "code": "TURN_RESTRICTION_UNSUPPORTED", "entity": f"relation/{identity}",
                    "message": "A turn restriction involves selected roads; this profile cannot execute it."})
        if not selected:
            diagnostics.append({"level": "error", "code": "EMPTY_SELECTION", "entity": "selection", "message": "No eligible road intersects the selected area."})
        root = ET.Element("osm", version="0.6", generator="htc-bounded-osm-importer")
        refs = sorted({ref for _, _, way_refs in selected for ref in way_refs})
        points = coordinates(refs)
        for ref in refs:
            lon, lat = points[ref]
            ET.SubElement(root, "node", id=ref, lon=str(lon), lat=str(lat))
        for _, text, _ in sorted(selected):
            root.append(ET.fromstring(text))
        destination = workspace / "selected.osm"
        ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
        emit({"phase": "verify_source"})
        if fingerprint(source) != source_hash:
            raise ValueError("Source changed while preparing the import; repeat against an immutable extract")
        extent = None if not points else {"west": min(p[0] for p in points.values()), "south": min(p[1] for p in points.values()),
            "east": max(p[0] for p in points.values()), "north": max(p[1] for p in points.values())}
        return destination, {"source": {"path": str(source.resolve()), "sha256": source_hash},
            "selection": {"bbox": bbox, "strategy": "whole_intersecting_ways", "ways": sorted(selected_ids), "nodes": len(refs),
                          "extent": extent, "truncated": statistics["eligible_intersecting_ways"] > max_ways,
                          "selected_sha256": fingerprint(destination)},
            "statistics": statistics, "excluded": dict(excluded), "diagnostics": diagnostics}
    finally:
        database.close()
