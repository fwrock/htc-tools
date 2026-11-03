"""CSV + human-readable report writers for the conservative-vs-time-warp parity comparison.
Text formatting modeled on `tools/sumo_validation/metrics/rmse.py::format_report`.
"""

from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path

from metrics.parity import ParityFinding

_FIELDNAMES = ["check", "tier", "entity_id", "event_type", "conservative_value", "time_warp_value", "delta", "verdict"]


def write_csv(findings: list[ParityFinding], csv_path: Path) -> Path:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for finding in findings:
            writer.writerow(asdict(finding))
    return csv_path


def _format_finding_row(finding: ParityFinding) -> str:
    entity = finding.entity_id or "-"
    event_type = finding.event_type or "-"
    return (
        f"[{finding.verdict:10s}] {finding.tier:9s} {finding.check:38s} "
        f"entity={entity:32s} event_type={event_type:22s} "
        f"conservative={finding.conservative_value!r} time_warp={finding.time_warp_value!r} "
        f"delta={finding.delta!r}"
    )


def format_report(
    scenario_id: str,
    exact_fails: list[ParityFinding],
    tolerance_fails: list[ParityFinding],
    flags: list[ParityFinding],
    passes: list[ParityFinding],
    known_gap_findings: list[ParityFinding],
    conservative_timed_out: bool,
    time_warp_timed_out: bool,
    fail_on_known_gaps: bool,
) -> str:
    lines = [f"=== optimistic_validation: {scenario_id} ===", ""]

    if conservative_timed_out:
        lines.append("WARNING: conservative (discrete-event) run hit its timeout / did not log its stop marker.")
    if time_warp_timed_out:
        lines.append("WARNING: time-warp run hit its timeout / did not log its stop marker.")
    if conservative_timed_out or time_warp_timed_out:
        lines.append("")

    total = len(exact_fails) + len(tolerance_fails) + len(flags) + len(passes) + len(known_gap_findings)
    lines.append(f"Total findings: {total}")
    lines.append(f"  PASS:        {len(passes)}")
    lines.append(f"  FLAG:        {len(flags)}")
    lines.append(f"  FAIL (exact):     {len(exact_fails)}")
    lines.append(f"  FAIL (tolerance): {len(tolerance_fails)}")
    lines.append(f"  KNOWN_GAP:   {len(known_gap_findings)}")
    lines.append("")

    other_fail_count = len(exact_fails) + len(tolerance_fails)
    lines.append(
        f"Failures attributable to the known Movable rollback/replay gap "
        f"(see hyperbolic-time-chamber/docs/KNOWN_GAPS.md): {len(known_gap_findings)}"
    )
    lines.append(f"Other/unexplained failures: {other_fail_count}")
    exit_reason = "other/unexplained failures" if other_fail_count else None
    if fail_on_known_gaps and known_gap_findings:
        exit_reason = exit_reason or "known-gap failures (--fail-on-known-gaps set)"
    lines.append(f"Exit code will be nonzero due to: {exit_reason}" if exit_reason else "Exit code: 0 (clean)")
    lines.append("")

    if exact_fails:
        lines.append("--- EXACT-tier failures ---")
        for finding in exact_fails:
            lines.append(_format_finding_row(finding))
        lines.append("")

    if tolerance_fails:
        lines.append("--- TOLERANCE-tier failures ---")
        for finding in tolerance_fails:
            lines.append(_format_finding_row(finding))
        lines.append("")

    if flags:
        lines.append("--- FLAGS ---")
        for finding in flags:
            lines.append(_format_finding_row(finding))
        lines.append("")

    if known_gap_findings:
        lines.append("--- KNOWN_GAP (reclassified from the Movable rollback/replay gap) ---")
        for finding in known_gap_findings:
            lines.append(_format_finding_row(finding))
        lines.append("")

    lines.append("--- All findings ---")
    for finding in [*exact_fails, *tolerance_fails, *flags, *known_gap_findings, *passes]:
        lines.append(_format_finding_row(finding))

    return "\n".join(lines)
