#!/usr/bin/env python3
"""
Converts a scenario's data files from the old `htcaid:<type>;<name>` string-id convention to
plain, opaque, monotonically increasing `long` ids -- the convention the current
hyperbolic-time-chamber engine actually expects (see `ScenarioIdRegistry` in
tools/sao-paulo/generate_hybrid_input.py, which already made this switch for freshly generated
scenarios; the docstring there explains why).

Why this exists: `htc_hybrid_demo` and `htc_qa_full` under
/home/dean/hyperbolic-time-chamber/simulations/input/ predate that switch and still use the old
string convention everywhere (top-level actor `id`, `dependencies`, `ownedVehicles`, node/link
references, city_map.json, ...). Starting either scenario via the engine's REST API crashes
Jackson deserialization the moment it hits a Long-typed field holding a string like
`"htcaid:person;grid_0"` (confirmed live against org.interscity.htc.core.entity.actor.ActorSimulation
and friends). This script fixes the *data*, not the engine -- entity-id types in the engine
(Long) are correct and were not touched.

Reuses `ScenarioIdRegistry` from tools/sao-paulo/generate_hybrid_input.py (loaded dynamically
since `sao-paulo` has a hyphen and isn't an importable package) rather than reimplementing the
same "assign a deterministic, monotonically increasing long id per distinct source key" logic.

Approach (deliberately generic rather than hand-listing every Scala state-class field, since the
same ~30 keypaths repeat across many nested state shapes -- see the investigation notes in the
session that produced this script for the full per-file keypath audit):

  1. Scan every JSON file in the scenario's data/ directory. Any string value that matches
     `^htcaid:` is a candidate id reference.
  2. Two keypaths are the sole EXCEPTIONS -- they are Jackson-typed as plain opaque `String` in
     the current engine, matched only against each other by exact string equality, and never
     parsed as Long anywhere (confirmed against
     model.hybrid.entity.state.model.TransitStop.id: String and
     model.hybrid.entity.state.model.TransitRoute's `stops[].stopId: String`):
       - transit_map.json, top-level object key "id"
       - transit_routes.json, `stops[].stopId`
     These are left completely untouched.
  3. The JSON object key "resourceId" (and its ShardActorId alias "shardBucket") is always
     forced to `""` regardless of its current value. In the current engine these are either
     `ShardActorId.shardBucket: String` (empty = fall back to hash-based routing) or
     `NodeGraph`/`EdgeGraph.resourceId: String` (unused metadata) -- never parsed as Long, and
     the real (non-stale) sao-paulo/toulouse generator output never populates them with
     anything but the implicit default. The stale fixture data duplicates the id string into
     this field for no functional reason; converting it to a bare Long would risk a
     String-field/Long-value mismatch for zero benefit, so it's blanked instead.
  4. Every remaining `htcaid:`-prefixed string value, and every dict KEY that is itself such a
     string (e.g. `BusStationState.busStops: Map[Long, Long]`'s JSON object keys), is replaced
     by its registry-assigned long id -- the same source string always maps to the same id,
     everywhere it appears, across every file in the scenario (single registry per scenario
     directory, shared across all files so cross-file references like `city_map.json` naming
     the same node as `nodes_0.json` stay consistent).

Usage:
    python3 convert_htcaid_ids_to_long.py <scenario_dir> [<scenario_dir> ...]

Each `<scenario_dir>` must contain a `data/` subdirectory. Files are modified in place -- back
up the scenario directory yourself before running (this script does not do it for you).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SAO_PAULO_GENERATOR = (
    Path(__file__).resolve().parent / "sao-paulo" / "generate_hybrid_input.py"
)

# Keypaths (relative to the top of a single JSON document, list indices omitted) that hold an
# `htcaid:`-prefixed string but must NOT be converted -- see module docstring point 2.
EXCLUDED_KEYPATHS_BY_FILENAME: dict[str, set[tuple[str, ...]]] = {
    "transit_map.json": {("id",)},
    "transit_routes.json": {("stops", "stopId")},
}

RESOURCE_ID_KEYS = {"resourceId", "shardBucket"}


def load_scenario_id_registry():
    spec = importlib.util.spec_from_file_location(
        "generate_hybrid_input", SAO_PAULO_GENERATOR
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.ScenarioIdRegistry


def is_htcaid_string(value: object) -> bool:
    return isinstance(value, str) and value.startswith("htcaid:")


def convert_value(
    value: object,
    path: tuple[str, ...],
    excluded: set[tuple[str, ...]],
    registry,
) -> object:
    if isinstance(value, dict):
        new_dict = {}
        for k, v in value.items():
            if k in RESOURCE_ID_KEYS:
                new_dict[k] = ""
                continue
            new_key = k
            if is_htcaid_string(k):
                # A dict key that is itself an htcaid: reference (e.g. BusStationState.busStops
                # keys). JSON object keys are always strings; Jackson coerces a numeric string
                # key into a Long map key on the Scala side, so stringifying the new id is
                # correct here.
                new_key = str(registry.id_for("id", k))
            child_path = path + (k,)
            new_dict[new_key] = convert_value(v, child_path, excluded, registry)
        return new_dict
    if isinstance(value, list):
        return [convert_value(v, path, excluded, registry) for v in value]
    if is_htcaid_string(value):
        if path in excluded:
            return value
        return registry.id_for("id", value)
    return value


def convert_file(path: Path, excluded: set[tuple[str, ...]], registry) -> bool:
    original_text = path.read_text()
    data = json.loads(original_text)
    converted = convert_value(data, (), excluded, registry)
    new_text = json.dumps(converted, indent=2, ensure_ascii=False) + "\n"
    if new_text == original_text:
        return False
    path.write_text(new_text)
    return True


def convert_scenario(scenario_dir: Path, registry) -> None:
    data_dir = scenario_dir / "data"
    if not data_dir.is_dir():
        raise SystemExit(f"{scenario_dir}: no data/ subdirectory found")

    changed = []
    for f in sorted(data_dir.glob("*.json")):
        excluded = EXCLUDED_KEYPATHS_BY_FILENAME.get(f.name, set())
        if convert_file(f, excluded, registry):
            changed.append(f.name)

    print(f"[{scenario_dir.name}] converted {len(changed)} file(s): {', '.join(changed) or '(none)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario_dirs", nargs="+", type=Path)
    parser.add_argument(
        "--registry-out",
        type=Path,
        default=None,
        help="Optional path to dump the final id registry as JSON (for auditing/debugging).",
    )
    args = parser.parse_args()

    ScenarioIdRegistry = load_scenario_id_registry()
    registry = ScenarioIdRegistry()

    for scenario_dir in args.scenario_dirs:
        convert_scenario(scenario_dir.resolve(), registry)

    print(f"Total distinct ids assigned: {registry._next - 1}")

    if args.registry_out:
        args.registry_out.write_text(
            json.dumps(
                {f"{k[0]}:{k[1]}": v for k, v in registry._ids.items()},
                indent=2,
            )
        )
        print(f"Registry dumped to {args.registry_out}")


if __name__ == "__main__":
    main()
