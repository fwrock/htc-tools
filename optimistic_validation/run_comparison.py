#!/usr/bin/env python3
"""Validates HTC's Time Warp (optimistic) execution mode against the already-validated
conservative (`discrete-event`) mode by running the same scenario twice and comparing Parquet
report output.

Usage (drives both runs itself):
    python run_comparison.py --jar /path/to/hyperbolic-time-chamber-*.jar \\
        [--scenario-dir <path> | --generate] \\
        [--out-dir ./output] \\
        [--conservative-timeout 180] [--time-warp-timeout 240] \\
        [--travel-time-tolerance-ticks 2] [--row-count-tolerance-pct 2.0] \\
        [--fail-on-known-gaps]

Usage (compares Parquet output from two runs already on disk -- e.g. a manual/docker-compose
run, not one this tool launched itself):
    python run_comparison.py --compare-existing \\
        --conservative-parquet-dir /path/to/scenario_c \\
        --time-warp-parquet-dir /path/to/scenario_o \\
        [--time-warp-log /path/to/run.log] \\
        [--scenario-id my_scenario] [--out-dir ./output] [--fail-on-known-gaps]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# Appended, not inserted at 0: `sumo_validation` also has top-level `parsers`/`metrics` packages
# with different contents -- this tool's own same-named local packages must win name resolution,
# so `sumo_validation` is only reachable as a fallback for names this tool doesn't define itself
# (here, just `runners.htc_runner`).
sys.path.append(str(Path(__file__).parent.parent / "sumo_validation"))

from scenario.select import resolve as resolve_scenario
from variants.simulation_json import write_variant
from runners.htc_runner import run_htc
from parsers.htc_events import parse_htc_events
from metrics.parity import ParityConfig, ParityFinding, compare_runs
from metrics.known_gaps import detect_movable_rollback_gap
from reports.text_report import format_report, write_csv


def _reclassify_known_gaps(
    findings: list[ParityFinding], gap_entities: set[str]
) -> tuple[list[ParityFinding], list[ParityFinding]]:
    kept: list[ParityFinding] = []
    reclassified: list[ParityFinding] = []
    for finding in findings:
        if finding.entity_id is not None and finding.entity_id in gap_entities:
            reclassified.append(
                ParityFinding(
                    check=finding.check,
                    tier=finding.tier,
                    entity_id=finding.entity_id,
                    event_type=finding.event_type,
                    conservative_value=finding.conservative_value,
                    time_warp_value=finding.time_warp_value,
                    delta=finding.delta,
                    verdict="KNOWN_GAP",
                )
            )
        else:
            kept.append(finding)
    return kept, reclassified


def _compare_and_report(
    conservative_df,
    time_warp_df,
    time_warp_log_path: Path,
    scenario_id: str,
    scenario_dir: Path,
    config: ParityConfig,
    fail_on_known_gaps: bool,
    conservative_timed_out: bool = False,
    time_warp_timed_out: bool = False,
) -> int:
    """Shared tail end of both CLI modes: compare, reclassify known gaps, write reports, return
    the process exit code."""
    print(f"[{scenario_id}] comparing runs...")
    result = compare_runs(conservative_df, time_warp_df, config)

    gap_entities = detect_movable_rollback_gap(time_warp_log_path, time_warp_df, conservative_df)
    exact_fails, exact_gap = _reclassify_known_gaps(result.exact_fails, gap_entities)
    tolerance_fails, tolerance_gap = _reclassify_known_gaps(result.tolerance_fails, gap_entities)
    known_gap_findings = [*exact_gap, *tolerance_gap]

    all_findings = [*exact_fails, *tolerance_fails, *result.flags, *known_gap_findings, *result.passes]
    scenario_dir.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(all_findings, scenario_dir / "parity.csv")

    report = format_report(
        scenario_id=scenario_id,
        exact_fails=exact_fails,
        tolerance_fails=tolerance_fails,
        flags=result.flags,
        passes=result.passes,
        known_gap_findings=known_gap_findings,
        conservative_timed_out=conservative_timed_out,
        time_warp_timed_out=time_warp_timed_out,
        fail_on_known_gaps=fail_on_known_gaps,
    )
    report_path = scenario_dir / "report.txt"
    report_path.write_text(report + "\n")

    print(report)
    print(f"[{scenario_id}] wrote {csv_path} and {report_path}")

    other_fail_count = len(exact_fails) + len(tolerance_fails)
    if other_fail_count:
        return 1
    if fail_on_known_gaps and known_gap_findings:
        return 1
    return 0


def _run_compare_existing(args: argparse.Namespace) -> int:
    scenario_id = args.scenario_id or args.conservative_parquet_dir.parent.name or "comparison"
    scenario_dir = args.out_dir / scenario_id

    print(f"[{scenario_id}] parsing existing outputs...")
    print(f"  conservative: {args.conservative_parquet_dir}")
    print(f"  time-warp:    {args.time_warp_parquet_dir}")
    conservative_df = parse_htc_events(args.conservative_parquet_dir)
    time_warp_df = parse_htc_events(args.time_warp_parquet_dir)

    config = ParityConfig(
        travel_time_tolerance_ticks=args.travel_time_tolerance_ticks,
        row_count_tolerance_pct=args.row_count_tolerance_pct,
    )
    # A missing/absent log just means the known-gap detector finds no signature matches --
    # detect_movable_rollback_gap already treats a nonexistent path as "no gap entities".
    log_path = args.time_warp_log or Path("/nonexistent-time-warp-log")

    return _compare_and_report(
        conservative_df=conservative_df,
        time_warp_df=time_warp_df,
        time_warp_log_path=log_path,
        scenario_id=scenario_id,
        scenario_dir=scenario_dir,
        config=config,
        fail_on_known_gaps=args.fail_on_known_gaps,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--compare-existing",
        action="store_true",
        help="Compare Parquet output from two runs already on disk instead of driving both runs",
    )
    parser.add_argument("--jar", type=Path, help="Path to the HTC assembly fatjar (required unless --compare-existing)")
    scenario_group = parser.add_mutually_exclusive_group()
    scenario_group.add_argument("--scenario-dir", type=Path, help="Already-generated scenario directory")
    scenario_group.add_argument("--generate", action="store_true", help="Generate a fresh tiny validation scenario")
    parser.add_argument("--conservative-parquet-dir", type=Path, help="--compare-existing: conservative run's Parquet dir")
    parser.add_argument("--time-warp-parquet-dir", type=Path, help="--compare-existing: time-warp run's Parquet dir")
    parser.add_argument("--time-warp-log", type=Path, help="--compare-existing: time-warp run's log (for known-gap detection)")
    parser.add_argument("--scenario-id", help="--compare-existing: label for output files (default: parquet dir's parent name)")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "output", help="Output directory")
    parser.add_argument("--conservative-timeout", type=float, default=180.0, help="Conservative run timeout (s)")
    parser.add_argument("--time-warp-timeout", type=float, default=240.0, help="Time-warp run timeout (s)")
    parser.add_argument("--travel-time-tolerance-ticks", type=int, default=2)
    parser.add_argument("--row-count-tolerance-pct", type=float, default=2.0)
    parser.add_argument(
        "--fail-on-known-gaps",
        action="store_true",
        help="Exit nonzero even when the only failures are attributable to the known Movable rollback gap",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve scenario + write variants but do not invoke java (for wiring smoke-tests)",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.compare_existing:
        if not args.conservative_parquet_dir or not args.time_warp_parquet_dir:
            parser.error("--compare-existing requires --conservative-parquet-dir and --time-warp-parquet-dir")
        return _run_compare_existing(args)

    if not args.jar:
        parser.error("--jar is required unless --compare-existing is given")
    if not args.scenario_dir and not args.generate:
        parser.error("one of --scenario-dir or --generate is required unless --compare-existing is given")

    scenario = resolve_scenario(
        scenario_dir=args.scenario_dir,
        generate=args.generate,
        generated_output_dir=args.out_dir / "_generated_scenario",
    )
    scenario_dir = args.out_dir / scenario.scenario_id
    print(f"[{scenario.scenario_id}] scenario resolved -> {scenario.scenario_dir}")

    variants_dir = scenario_dir / "variants"
    conservative_json = write_variant(scenario.simulation_json, "discrete-event", variants_dir / "conservative_simulation.json")
    time_warp_json = write_variant(scenario.simulation_json, "time-warp", variants_dir / "time_warp_simulation.json")
    print(f"[{scenario.scenario_id}] wrote variants -> {conservative_json}, {time_warp_json}")

    if args.dry_run:
        print(f"[{scenario.scenario_id}] --dry-run: skipping java invocation and comparison")
        return 0

    conservative_htc_dir = scenario_dir / "conservative" / "htc"
    conservative_parquet_dir = scenario_dir / "conservative" / "htc_parquet"
    time_warp_htc_dir = scenario_dir / "time_warp" / "htc"
    time_warp_parquet_dir = scenario_dir / "time_warp" / "htc_parquet"

    print(f"[{scenario.scenario_id}] running conservative (discrete-event), timeout={args.conservative_timeout}s...")
    conservative_result = run_htc(
        simulation_json=conservative_json,
        city_map_json=scenario.city_map_json,
        jar_path=args.jar,
        work_dir=conservative_htc_dir,
        parquet_dir=conservative_parquet_dir,
        timeout_s=args.conservative_timeout,
        cluster_port=1600,
    )
    if conservative_result.timed_out:
        print(f"[{scenario.scenario_id}] WARNING: conservative run timed out -- see {conservative_result.log_path}")

    print(f"[{scenario.scenario_id}] running time-warp, timeout={args.time_warp_timeout}s...")
    time_warp_result = run_htc(
        simulation_json=time_warp_json,
        city_map_json=scenario.city_map_json,
        jar_path=args.jar,
        work_dir=time_warp_htc_dir,
        parquet_dir=time_warp_parquet_dir,
        timeout_s=args.time_warp_timeout,
        cluster_port=1601,
    )
    if time_warp_result.timed_out:
        print(f"[{scenario.scenario_id}] WARNING: time-warp run timed out -- see {time_warp_result.log_path}")

    print(f"[{scenario.scenario_id}] parsing outputs...")
    conservative_df = parse_htc_events(conservative_parquet_dir)
    time_warp_df = parse_htc_events(time_warp_parquet_dir)

    config = ParityConfig(
        travel_time_tolerance_ticks=args.travel_time_tolerance_ticks,
        row_count_tolerance_pct=args.row_count_tolerance_pct,
    )

    return _compare_and_report(
        conservative_df=conservative_df,
        time_warp_df=time_warp_df,
        time_warp_log_path=time_warp_result.log_path,
        scenario_id=scenario.scenario_id,
        scenario_dir=scenario_dir,
        config=config,
        fail_on_known_gaps=args.fail_on_known_gaps,
        conservative_timed_out=conservative_result.timed_out,
        time_warp_timed_out=time_warp_result.timed_out,
    )


if __name__ == "__main__":
    sys.exit(main())
