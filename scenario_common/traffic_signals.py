"""OSM signal locations and synthetic HTC signal timing.

OSM supplies locations, not calibrated phase plans. Every generated approach
gets a non-overlapping green window and a red interval, including single-link
stop lines (which occur on one-way roads).
"""

import gzip
import json
import math
import xml.etree.ElementTree as ET


def iter_osm_signals(path):
    """Stream (OSM node id, latitude, longitude) from XML or gzipped XML."""
    if path.suffix.lower() == ".pbf":
        raise ValueError("Signal input must be OSM XML/XML.gz; convert PBF with osmium first")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        root = None
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = elem
                if elem.tag != "osm":
                    raise ValueError("Signal input must be an OSM XML document")
            if event != "end" or elem.tag not in ("node", "way", "relation"):
                continue
            if elem.tag == "node" and any(
                tag.get("k") == "highway" and tag.get("v") == "traffic_signals"
                for tag in elem.findall("tag")
            ):
                lat, lon = float(elem.get("lat")), float(elem.get("lon"))
                if not (math.isfinite(lat) and math.isfinite(lon)
                        and -90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError(f"Invalid coordinates for OSM signal {elem.get('id')}")
                yield elem.get("id"), lat, lon
            root.remove(elem)


def validate_signal_config(cfg):
    if cfg.signal_cycle_duration <= cfg.signal_yellow_duration + cfg.signal_all_red_duration:
        raise ValueError("signal-cycle must leave time for green after yellow/all-red clearance")
    if cfg.signal_yellow_duration < 0 or cfg.signal_all_red_duration < 0:
        raise ValueError("Signal clearance durations must be nonnegative")
    if not 0 < cfg.signal_green_ratio < 1:
        raise ValueError("signal_green_ratio must be in (0,1)")


def build_signal_actor(cfg, ids, rng, node_id, incoming_link_ids):
    links = list(dict.fromkeys(incoming_link_ids))
    cycle = cfg.signal_cycle_duration
    clearance = cfg.signal_yellow_duration + cfg.signal_all_red_duration
    if not links or cycle // len(links) <= clearance:
        raise ValueError(f"signal-cycle {cycle} is too short for {len(links)} approaches at node {node_id}")
    phases = []
    states = {}
    for i, lid in enumerate(links):
        start, end = i * cycle // len(links), (i + 1) * cycle // len(links)
        green = end - start - clearance
        if len(links) == 1:
            green = max(1, min(cycle - 1, int(green * cfg.signal_green_ratio)))
        origin = ids.id_for("link", lid)
        phases.append({"origin": origin, "greenStart": start, "greenDuration": green, "state": "Red"})
        states[origin] = {"state": "Red", "remainingTime": 0, "nextTick": 0}
    node = ids.id_for("node", node_id)
    return {
        "id": ids.id_for("signal", f"node_{node_id}"),
        "typeActor": "hybrid.actor.TrafficSignal",
        "data": {"dataType": "model.hybrid.entity.state.TrafficSignalState", "content": {
            "startTick": 0, "cycleDuration": cycle, "offset": rng.randrange(cycle),
            "nodes": [node], "phases": phases, "signalStates": states,
        }},
        # TrafficSignal resolves each affected node by its decimal entity id.
        "dependencies": {str(node): {"id": node, "classType": "hybrid.actor.Node"}},
    }


def clear_signal_nodes(data_dir, controlled_nodes):
    """Remove generated gates when reusing a network with signals disabled."""
    for path in sorted(data_dir.glob("nodes_*.json")):
        actors = json.loads(path.read_text())
        changed = False
        for actor in actors:
            if actor["id"] in controlled_nodes:
                content = actor["data"]["content"]
                content.update(connections={}, approachConnections={}, signals={})
                changed = True
        if changed:
            path.write_text(json.dumps(actors, separators=(",", ":")))


def wire_signal_nodes(data_dir):
    """Wire generated phases into both MESO gates and MICRO approach notifications.

    HTC's MESO gate is indexed by the *outgoing* link. Without turn movements,
    associate it with the straightest incoming approach (a documented synthetic
    approximation). MICRO notifications use the exact incoming phase/link pair.
    """
    controls = {}
    for path in sorted(data_dir.glob("traffic_signals_*.json")):
        for actor in json.loads(path.read_text()):
            content = actor["data"]["content"]
            controls[content["nodes"][0]] = {p["origin"] for p in content["phases"]}
    if not controls:
        return
    incoming = {n: [] for n in controls}
    outgoing = {n: [] for n in controls}
    needed = set(controls)
    for path in sorted(data_dir.glob("links_*.json")):
        for actor in json.loads(path.read_text()):
            c = actor["data"]["content"]
            if c["to"] in controls and actor["id"] in controls[c["to"]]:
                incoming[c["to"]].append((actor["id"], c["from"]))
                needed.add(c["from"])
            if c["from"] in controls:
                outgoing[c["from"]].append((actor["id"], c["to"]))
                needed.add(c["to"])
    coords = {}
    for path in sorted(data_dir.glob("nodes_*.json")):
        for actor in json.loads(path.read_text()):
            if actor["id"] in needed:
                c = actor["data"]["content"]
                coords[actor["id"]] = (c["longitude"], c["latitude"])

    def identify(origin):
        return {"id": origin, "classType": "hybrid.actor.Link", "resourceId": origin}

    for path in sorted(data_dir.glob("nodes_*.json")):
        actors = json.loads(path.read_text())
        changed = False
        for actor in actors:
            node = actor["id"]
            if node not in controls:
                continue
            c = actor["data"]["content"]
            c["approachConnections"] = {str(link): identify(link) for link, _ in incoming[node]}
            c["connections"] = {}
            lon, lat = coords[node]
            scale = math.cos(math.radians(lat))
            for link, target in outgoing[node]:
                tx, ty = coords[target]
                dx, dy = (tx - lon) * scale, ty - lat

                def alignment(approach):
                    lid, source = approach
                    sx, sy = coords[source]
                    ax, ay = (lon - sx) * scale, lat - sy
                    denom = math.hypot(ax, ay) * math.hypot(dx, dy)
                    cosine = (ax * dx + ay * dy) / denom if denom else -1.0
                    return cosine, -lid

                origin, _ = max(incoming[node], key=alignment)
                c["connections"][str(link)] = identify(origin)
            changed = True
        if changed:
            path.write_text(json.dumps(actors, separators=(",", ":")))
