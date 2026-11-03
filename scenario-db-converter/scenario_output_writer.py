#!/usr/bin/env python3
"""Shared writer for the HTC scenario-generation output package.

Every tool under `tools/` that turns some source format (EQAsim CSV/GeoPackage, MATSim XML,
InterSCSimulator XML, ...) into HTC simulator input is expected to emit the SAME package shape,
because htc-bus / the engine's Load Data Manager / downstream htc-dl consumers all read this
shape, not a per-tool variant:

    <scenario_name>/
      GENERATION_REPORT.md
      scenario_metadata.json
      simulation.json
      data/
        city_map.json
        <group>_<n>.json   (sharded actor records, one series per actor type: nodes_0.json,
                             links_0.json, persons_0.json, cars_0.json, ...)

Historically this shape was implemented independently inside `tools/sao-paulo/
generate_hybrid_input.py` and `tools/toulouse/generate_hybrid_input.py` (each ~2000-4000 lines,
each with its own copy of the sharding + simulation.json/scenario_metadata.json/
GENERATION_REPORT.md writers). `tools/interscsimulator_scenario/convert_interscsimulator.py`
never got this treatment and instead wrote a single flat `actor_simulation.json` +
`city_map.json` with no `data/` dir and none of the three top-level manifest files — a silent
format drift from the platform's actual output contract.

This module is the extraction point going forward: NEW converters should use it directly rather
than re-deriving the shape from reading an existing tool's source. `sao-paulo` and `toulouse`'s
own writers are intentionally left as-is for now (they already produce conformant output and are
large, well-exercised pieces of code with no defect to fix — swapping their internals to call
through here is a separate, lower-urgency refactor to avoid format drift #2 the same way, not a
correctness fix).

Actor items are expected in the platform's standard shape (identical across every generator
verified so far — sao-paulo, toulouse, interscsimulator):

    {"id": "...", "typeActor": "hybrid.actor.<Kind>", "data": {...}, "dependencies": {...}}

`ACTOR_GROUP_SPEC` below is the source of truth for which on-disk file group ("nodes", "links",
"persons", ...) a given `typeActor` shards into, and whether the simulation.json entry for that
group is STATIC/EAGER (infrastructure, loaded once up front) or DYNAMIC/PROGRESSIVE (agents,
streamed in over the run) — copied from `HybridInputGenerator._build_actor_sources` in both
sao-paulo/generate_hybrid_input.py and toulouse/generate_hybrid_input.py, which agreed with each
other on every entry at the time of writing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# typeActor -> (file group prefix, entityLifecycle, loadingStrategy)
ACTOR_GROUP_SPEC: Dict[str, Tuple[str, str, str]] = {
    "hybrid.actor.Node":          ("nodes",           "STATIC",  "EAGER"),
    "hybrid.actor.Link":          ("links",           "STATIC",  "EAGER"),
    "hybrid.actor.Person":        ("persons",         "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.Car":           ("cars",            "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.Bicycle":       ("bikes",           "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.Motorcycle":    ("motorcycles",     "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.BusStop":       ("bus_stops",       "STATIC",  "EAGER"),
    "hybrid.actor.BusStation":    ("bus_stations",    "STATIC",  "EAGER"),
    "hybrid.actor.Bus":           ("buses",           "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.SubwayStation": ("subway_stations", "STATIC",  "EAGER"),
    "hybrid.actor.RailLink":      ("rail_links",      "STATIC",  "EAGER"),
    "hybrid.actor.Subway":        ("subways",         "DYNAMIC", "PROGRESSIVE"),
    "hybrid.actor.TrafficSignal": ("traffic_signals", "STATIC",  "EAGER"),
}

DEFAULT_ITEMS_PER_FILE = 50_000


def _group_for(type_actor: str) -> Tuple[str, str, str]:
    spec = ACTOR_GROUP_SPEC.get(type_actor)
    if spec is not None:
        return spec
    # Forward-compat fallback for an actor kind not yet in ACTOR_GROUP_SPEC: derive a group name
    # rather than silently dropping the actor. Defaults to STATIC/EAGER (the safer of the two —
    # DYNAMIC actors loaded EAGER just come in sooner, whereas STATIC infra loaded PROGRESSIVE
    # could arrive after something depends on it).
    kind = type_actor.rsplit(".", 1)[-1]
    group = "".join(
        f"_{c.lower()}" if c.isupper() and i > 0 else c.lower() for i, c in enumerate(kind)
    ).lstrip("_") + "s"
    print(f"  [scenario_output_writer] WARNING: unmapped typeActor {type_actor!r}; "
          f"sharding into {group!r} as STATIC/EAGER by default — add it to ACTOR_GROUP_SPEC")
    return group, "STATIC", "EAGER"


def shard_and_write_actors(
    actors: List[dict],
    data_dir: Path,
    items_per_file: int = DEFAULT_ITEMS_PER_FILE,
) -> Dict[str, Dict[str, Any]]:
    """Group `actors` by typeActor and write them as `<group>_<n>.json` shards under `data_dir`.

    Returns {group: {"files": n_files, "count": n_actors, "entityLifecycle": ..., "loadingStrategy": ...}}
    keyed by file-group name (e.g. "nodes", "persons"), suitable for feeding straight into
    `build_simulation_json`.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    buckets: Dict[str, List[dict]] = {}
    meta: Dict[str, Tuple[str, str]] = {}
    for actor in actors:
        type_actor = actor.get("typeActor", "")
        group, lifecycle, strategy = _group_for(type_actor)
        buckets.setdefault(group, []).append(actor)
        meta[group] = (lifecycle, strategy)

    result: Dict[str, Dict[str, Any]] = {}
    for group, items in buckets.items():
        n_files = max(1, (len(items) + items_per_file - 1) // items_per_file) if items else 0
        for i in range(n_files):
            chunk = items[i * items_per_file:(i + 1) * items_per_file]
            with open(data_dir / f"{group}_{i}.json", "w") as f:
                json.dump(chunk, f)
        lifecycle, strategy = meta[group]
        result[group] = {
            "files": n_files,
            "count": len(items),
            "entityLifecycle": lifecycle,
            "loadingStrategy": strategy,
        }
    return result


def write_city_map_json(city_map: dict, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / "city_map.json"
    with open(out, "w") as f:
        json.dump(city_map, f)
    return out


def build_simulation_json(
    scenario_id: str,
    name: str,
    description: str,
    group_counts: Dict[str, Dict[str, Any]],
    docker_base: str,
    output_format: str = "json",
    start_tick: int = 0,
    end_tick: int = 86_400,
    start_real_time: str = "2025-01-01T00:00:00.000",
    time_unit: str = "seconds",
    time_step: float = 1.0,
    tick_duration: float = 1.0,
    random_seed: int = 42,
    warmup_targets: Optional[List[str]] = None,
    post_load_registration_classes: Optional[List[str]] = None,
) -> dict:
    """Build the simulation.json payload. `group_counts` is the dict returned by
    `shard_and_write_actors`. `docker_base` is the base path used inside Docker for
    dataSource.info.path references, e.g. "/app/hyperbolic-time-chamber/simulations/input"
    (matching sao-paulo's/toulouse's --docker-base default) — the final path is
    f"{docker_base}/{scenario_id}/data/{group}_{i}.{ext}".
    """
    ext = "db" if output_format == "sqlite" else "json"
    base = f"{docker_base}/{scenario_id}/data"

    # Stable ordering: static infra groups before dynamic agent groups, alphabetical within
    # each, matching the sao-paulo/toulouse ordering convention (nodes, links, persons, cars, ...).
    ordered_groups = sorted(
        group_counts.keys(),
        key=lambda g: (group_counts[g]["entityLifecycle"] != "STATIC", g),
    )

    sources: List[Dict[str, Any]] = []
    type_actor_by_group = {v[0]: k for k, v in ACTOR_GROUP_SPEC.items()}
    for group in ordered_groups:
        info = group_counts[group]
        class_type = type_actor_by_group.get(group, f"hybrid.actor.{group.rstrip('s').title()}")
        for i in range(info["files"]):
            sources.append({
                "id": f"{group}_{i}",
                "classType": class_type,
                "creationType": "LoadBalancedDistributed",
                "loadingStrategy": info["loadingStrategy"],
                "entityLifecycle": info["entityLifecycle"],
                "dataSource": {
                    "sourceType": output_format,
                    "info": {"path": f"{base}/{group}_{i}.{ext}"},
                },
            })

    return {
        "id": scenario_id,
        "name": name,
        "description": description,
        "startTick": start_tick,
        "endTick": end_tick,
        "startRealTime": start_real_time,
        "timeUnit": time_unit,
        "timeStep": time_step,
        "duration": end_tick - start_tick,
        "tickDuration": tick_duration,
        "randomSeed": random_seed,
        "actorsDataSources": sources,
        "cityMapFile": f"{base}/city_map.{ext}",
        "warmupTargets": warmup_targets or ["org.interscity.htc.model.hybrid.util.CityMapUtil#warmUp"],
        "postLoadRegistrationClasses": post_load_registration_classes or [
            "org.interscity.htc.model.hybrid.actor.BusStop",
            "org.interscity.htc.model.hybrid.actor.SubwayStation",
        ],
    }


def write_simulation_json(output_dir: Path, sim: dict) -> Path:
    out = output_dir / "simulation.json"
    with open(out, "w") as f:
        json.dump(sim, f)
    return out


def build_scenario_metadata(
    name: str,
    description: str,
    source: dict,
    target: dict,
    statistics: Dict[str, Any],
    configuration: Dict[str, Any],
    version: str = "1.0.0",
) -> dict:
    return {
        "name": name,
        "description": description,
        "generated": datetime.now().isoformat(),
        "version": version,
        "source": source,
        "target": target,
        "statistics": statistics,
        "configuration": configuration,
    }


def write_scenario_metadata(output_dir: Path, metadata: dict) -> Path:
    out = output_dir / "scenario_metadata.json"
    with open(out, "w") as f:
        json.dump(metadata, f, indent=2)
    return out


def write_generation_report(
    output_dir: Path,
    title: str,
    source_dir: str,
    source_format: str,
    scenario_name: str,
    group_counts: Dict[str, Dict[str, Any]],
    extra_sections: str = "",
) -> Path:
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = "\n".join(
        f"| {group} | {info['count']:,} | {info['files']} |"
        for group, info in sorted(group_counts.items())
    )
    report = f"""# Generation Report: {title}

**Generated:** {ts_str}

## Source

- **Directory:** `{source_dir}`
- **Format:** {source_format}

## Target

- **Scenario:** `{scenario_name}`
- **Directory:** `{output_dir}`

## Actor Groups

| Group | Count | Files |
|-------|------:|------:|
{rows}
{extra_sections}
## Output Layout

```
{scenario_name}/
|-- GENERATION_REPORT.md
|-- scenario_metadata.json
|-- simulation.json
`-- data/
    |-- city_map.json
    `-- <group>_<n>.json
```
"""
    out = output_dir / "GENERATION_REPORT.md"
    out.write_text(report)
    return out
