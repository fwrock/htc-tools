#!/usr/bin/env python3
"""End-to-end HTC-vs-SUMO MICRO validation pipeline.

Exports scenarios A and B to both HTC and SUMO formats, runs both simulators, parses
both outputs into the common schema, computes per-vehicle metrics, and writes a report
(CSV + human-readable summary) per scenario.

Usage:
    python run_validation.py --jar /path/to/hyperbolic-time-chamber-1.24.3.jar \\
        [--scenarios a b] [--out-dir ./output] [--timeout 180]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scenario.scenario_a import build_scenario as build_scenario_a
from scenario.scenario_b import build_scenario as build_scenario_b
from htc_export.export import export_htc
from sumo_export.export import export_sumo
from runners.htc_runner import run_htc
from runners.sumo_runner import run_sumo
from parsers.htc_parquet import parse_htc_parquet
from parsers.sumo_fcd import parse_sumo_fcd
from metrics.rmse import compute_scenario_metrics, format_report, metrics_to_dataframe

SCENARIOS = {"a": build_scenario_a, "b": build_scenario_b}


def run_one(scenario_id: str, out_dir: Path, jar_path: Path, timeout_s: float) -> str:
    build_fn = SCENARIOS[scenario_id]
    scenario = build_fn()
    scenario_dir = out_dir / scenario.scenario_id

    htc_dir = scenario_dir / "htc"
    sumo_dir = scenario_dir / "sumo"
    parquet_dir = scenario_dir / "htc_parquet"

    print(f"[{scenario.scenario_id}] exporting HTC scenario -> {htc_dir}")
    htc_paths = export_htc(scenario, htc_dir)

    print(f"[{scenario.scenario_id}] exporting SUMO scenario -> {sumo_dir}")
    sumo_paths = export_sumo(scenario, sumo_dir)

    print(f"[{scenario.scenario_id}] running HTC (timeout={timeout_s}s)...")
    htc_result = run_htc(
        simulation_json=htc_paths["simulation_json"],
        city_map_json=htc_paths["city_map_json"],
        jar_path=jar_path,
        work_dir=htc_dir,
        parquet_dir=parquet_dir,
        timeout_s=timeout_s,
    )
    if htc_result.timed_out:
        print(
            f"[{scenario.scenario_id}] WARNING: HTC run hit the timeout/did not log its "
            f"stop marker -- see {htc_result.log_path}"
        )

    print(f"[{scenario.scenario_id}] running SUMO...")
    sumo_result = run_sumo(sumocfg=sumo_paths["sumocfg"], fcd_output=sumo_paths["fcd_output"])
    if sumo_result.returncode != 0:
        print(f"[{scenario.scenario_id}] WARNING: SUMO exit code {sumo_result.returncode}")
        print(sumo_result.stderr)

    print(f"[{scenario.scenario_id}] parsing outputs...")
    htc_df = parse_htc_parquet(parquet_dir)
    sumo_df = parse_sumo_fcd(sumo_paths["fcd_output"])

    print(f"[{scenario.scenario_id}] computing metrics...")
    metrics = compute_scenario_metrics(htc_df, sumo_df)

    metrics_df = metrics_to_dataframe(metrics)
    csv_path = scenario_dir / "metrics.csv"
    metrics_df.to_csv(csv_path, index=False)

    report = format_report(scenario.scenario_id, metrics)
    report_path = scenario_dir / "report.txt"
    report_path.write_text(report + "\n")

    print(report)
    print(f"[{scenario.scenario_id}] wrote {csv_path} and {report_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", required=True, type=Path, help="Path to the HTC assembly fatjar")
    parser.add_argument(
        "--scenarios", nargs="+", default=["a", "b"], choices=list(SCENARIOS), help="Which scenarios to run"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path(__file__).parent / "output", help="Output directory"
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="HTC run timeout in seconds")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    for scenario_id in args.scenarios:
        reports.append(run_one(scenario_id, args.out_dir, args.jar, args.timeout))

    print("\n\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in reports:
        print(r)
        print()


if __name__ == "__main__":
    main()
