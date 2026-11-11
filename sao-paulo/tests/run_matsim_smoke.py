#!/usr/bin/env python3
"""Retain a native MATSim fixture, run HTC, and check completed model outcomes."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from test_matsim import SCRIPT, fixtures, gtfs_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--htc-root", type=Path, default=Path(__file__).resolve().parents[3] / "hyperbolic-time-chamber")
    parser.add_argument("--jar", type=Path)
    parser.add_argument("--base-port", type=int, default=32750)
    parser.add_argument("--bus-source", choices=["matsim", "gtfs"], default="matsim")
    args = parser.parse_args()
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    inp = fixtures(root)
    if args.bus_source == "gtfs":
        gtfs_fixture(inp)
    output = root / "scenarios"
    generation = [sys.executable, str(SCRIPT), "--input-dir", str(inp), "--output", str(output),
        "--scenario", "native", "--sample", "1", "--duration", "240", "--docker-base", str(output),
        "--mc-strategy", "raptor", "--mc-modes", "bus", "--max-trains-per-line", "1",
        "--bus-source", args.bus_source]
    if args.bus_source == "gtfs":
        generation += ["--service-date", "2026-11-04", "--transit-providers", "bus"]
    (root / "generation_command.json").write_text(json.dumps(generation, indent=2))
    subprocess.run(generation, check=True)
    expected = {"schedules_completed": 2, "person_trips": 3, "car_journeys": 2,
                "bus_activations": [10, 70],
                "bus_passengers_boarded": 1, "train_activations": 2}
    (root / "expected.json").write_text(json.dumps(expected, indent=2))
    htc = args.htc_root.resolve()
    jar = args.jar or htc / "target/scala-3.3.5/hyperbolic-time-chamber-3.2.0.jar"
    digest = hashlib.sha256()
    with jar.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    (root / "executable.json").write_text(json.dumps({"jar": str(jar), "sha256": digest.hexdigest()}, indent=2))
    command = [sys.executable, str(htc / "scripts/scenario/run_cluster_validation.py"),
               "--jar", str(jar), "--simulation", str(output / "native/simulation.json"),
               "--output", str(root / "runtime"), "--nodes", "1", "--timeout", "90",
               "--base-port", str(args.base_port)]
    (root / "runtime_command.json").write_text(json.dumps(command, indent=2))
    subprocess.run(command, check=True)
    records = [json.loads(line)["data"] for path in (root / "runtime/reports").rglob("*.jsonl")
               for line in path.read_text().splitlines()]
    completed = [d for d in records if d.get("event_type") == "schedule_complete"]
    observed = {
        "schedules_completed": len(completed),
        "person_trips": sum(d.get("total_trips", 0) for d in completed),
        "car_journeys": sum(d.get("event_type") == "sumo_tripinfo" and d.get("vehicle_type") == "car"
                            and d.get("arrival", -1) >= 0 for d in records),
        "bus_activations": sorted(d["tick"] for d in records if d.get("event_type") == "journey_started" and "bus_id" in d),
        "bus_passengers_boarded": sum(d.get("passengers_loaded", 0) for d in records if d.get("event_type") == "bus_load_passengers"),
        "train_activations": sum(d.get("event_type") == "sumo_tripinfo" and d.get("vehicle_type") == "subway" for d in records),
    }
    failures = [key for key in expected if expected[key] != observed[key]]
    engine = json.loads((root / "runtime/result.json").read_text())
    if not engine["engineCompleted"] or engine["runtimeFailed"] or any(engine["engineErrors"]):
        failures.append("engine_completion")
    if any(d.get("event_type") == "trip_aborted" for d in records):
        failures.append("trip_aborted")
    if not any(d.get("event_type") == "subway_arrival" for d in records):
        failures.append("subway_arrival")
    result = {"expected": expected, "observed": observed, "failed_checks": failures, "passed": not failures,
              "scope": "Synthetic São Paulo MATSim fixture; one JVM, conservative MESO, seed 42. Transit is horizon-censored."}
    (root / "outcomes.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
