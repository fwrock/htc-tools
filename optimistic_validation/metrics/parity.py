"""Exact-match and tolerance-band comparisons between a conservative (`discrete-event`) run and
a `time-warp` run of the same scenario.

Event-type (Parquet `event_type` column, i.e. the `label` argument to HTC's `report()` calls)
and field-name literals below were verified by grepping the actual `report(...)` call sites in
`hyperbolic-time-chamber/src/main/scala/model/hybrid/support/`, not guessed:
  - Completion: `"journey_completed"` (`Car`/`Bus`/`Motorcycle`/`Bicycle` JourneyReporters) and
    `"person_schedule_complete"` (`PersonMetricsReporter.reportScheduleComplete` -- the label
    passed to `reportFn`, distinct from the `data["event_type"] == "schedule_complete"` field
    inside the payload).
  - Creation: `"bus_created"` (`BusStationBusCreator`), `"subway_created"`
    (`SubwayStationCreator`); `"journey_started"` is included per the task brief as the
    creation/activation proxy for vehicle types that exist from scenario load rather than being
    spawned at runtime (`Car`/`Bicycle`/`Motorcycle`/`Subway`/`Bus` all report it as their trip
    start).
  - Link visits: `"enter_link"`/`"leave_link"` (meso) and `"enter_micro_link"`/
    `"leave_micro_link"` (micro sub-tick mode) all carry a `link_id` field in `data`
    (`CarLinkHandler`/`CarMicroHandler`/`BicycleLinkHandler`/`BicycleMicroHandler`/
    `MotorcycleLinkHandler`/`MotorcycleMicroHandler`/`BusLinkHandler`/`BusMicroHandler`/
    `Subway.scala` all use the literal key `"link_id"`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

COMPLETION_EVENT_TYPES = {"journey_completed", "person_schedule_complete"}
CREATION_EVENT_TYPES = {"bus_created", "subway_created", "journey_started"}
LINK_EVENT_TYPES = {"enter_link", "leave_link", "enter_micro_link", "leave_micro_link"}
STATUS_FIELDS = ("status", "movableStatus")


@dataclass
class ParityConfig:
    travel_time_tolerance_ticks: int = 2
    row_count_tolerance_pct: float = 2.0
    link_sequence_edit_distance_flag_threshold: int = 1


@dataclass
class ParityFinding:
    check: str
    tier: str  # "exact" | "tolerance"
    entity_id: str | None
    event_type: str | None
    conservative_value: Any
    time_warp_value: Any
    delta: Any
    verdict: str  # PASS | FAIL | FLAG | KNOWN_GAP


@dataclass
class ParityResult:
    exact_fails: list[ParityFinding] = field(default_factory=list)
    tolerance_fails: list[ParityFinding] = field(default_factory=list)
    flags: list[ParityFinding] = field(default_factory=list)
    passes: list[ParityFinding] = field(default_factory=list)

    def all_findings(self) -> list[ParityFinding]:
        return [*self.exact_fails, *self.tolerance_fails, *self.flags, *self.passes]


def _bucket(result: ParityResult, finding: ParityFinding) -> None:
    if finding.verdict == "PASS":
        result.passes.append(finding)
    elif finding.verdict == "FLAG":
        result.flags.append(finding)
    elif finding.tier == "exact":
        result.exact_fails.append(finding)
    else:
        result.tolerance_fails.append(finding)


def levenshtein(a: tuple, b: tuple) -> int:
    """Classic O(len(a)*len(b)) edit distance, iterative, no external dependency."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _sorted_sub(df: pd.DataFrame, entity_id: str) -> pd.DataFrame:
    sub = df[df["entity_id"] == entity_id]
    return sub.sort_values(["tick", "lamport_tick"])


def _link_sequence(df: pd.DataFrame, entity_id: str) -> tuple:
    sub = _sorted_sub(df[df["event_type"].isin(LINK_EVENT_TYPES)], entity_id)
    return tuple(row["data"].get("link_id") for _, row in sub.iterrows())


def _link_set(df: pd.DataFrame, entity_id: str) -> frozenset:
    return frozenset(_link_sequence(df, entity_id))


def _final_status(df: pd.DataFrame, entity_id: str):
    sub = _sorted_sub(df, entity_id)
    if sub.empty:
        return None
    last = sub.iloc[-1]
    for field_name in STATUS_FIELDS:
        if field_name in last["data"]:
            return last["data"][field_name]
    return None


def _travel_time(df: pd.DataFrame, entity_id: str) -> float | None:
    sub = _sorted_sub(df, entity_id)
    if sub.empty:
        return None
    has_completion = sub["event_type"].isin(COMPLETION_EVENT_TYPES).any()
    if not has_completion:
        return None
    return float(sub["tick"].max() - sub["tick"].min())


def _count_by_entity_type(df: pd.DataFrame, event_types: set[str]) -> dict[str, int]:
    sub = df[df["event_type"].isin(event_types)]
    if sub.empty:
        return {}
    return sub.groupby("entity_type").size().to_dict()


def _compare_count_dicts(
    result: ParityResult, check: str, cons: dict[str, int], tw: dict[str, int]
) -> None:
    for entity_type in sorted(set(cons) | set(tw)):
        c_val = cons.get(entity_type, 0)
        t_val = tw.get(entity_type, 0)
        verdict = "PASS" if c_val == t_val else "FAIL"
        _bucket(
            result,
            ParityFinding(
                check=check,
                tier="exact",
                entity_id=None,
                event_type=entity_type,
                conservative_value=c_val,
                time_warp_value=t_val,
                delta=t_val - c_val,
                verdict=verdict,
            ),
        )


def compare_runs(
    conservative_df: pd.DataFrame, time_warp_df: pd.DataFrame, config: ParityConfig
) -> ParityResult:
    result = ParityResult()

    # --- EXACT tier ---
    _compare_count_dicts(
        result,
        "completed_trip_count_per_entity_type",
        _count_by_entity_type(conservative_df, COMPLETION_EVENT_TYPES),
        _count_by_entity_type(time_warp_df, COMPLETION_EVENT_TYPES),
    )
    _compare_count_dicts(
        result,
        "entity_creation_count_per_entity_type",
        _count_by_entity_type(conservative_df, CREATION_EVENT_TYPES),
        _count_by_entity_type(time_warp_df, CREATION_EVENT_TYPES),
    )

    all_entity_ids = sorted(set(conservative_df["entity_id"]) | set(time_warp_df["entity_id"]))

    link_set_pass_entities: set[str] = set()
    for entity_id in all_entity_ids:
        c_set = _link_set(conservative_df, entity_id)
        t_set = _link_set(time_warp_df, entity_id)
        verdict = "PASS" if c_set == t_set else "FAIL"
        if verdict == "PASS":
            link_set_pass_entities.add(entity_id)
        _bucket(
            result,
            ParityFinding(
                check="distinct_links_visited_set",
                tier="exact",
                entity_id=entity_id,
                event_type=None,
                conservative_value=sorted(v for v in c_set if v is not None),
                time_warp_value=sorted(v for v in t_set if v is not None),
                delta=None,
                verdict=verdict,
            ),
        )

    for entity_id in all_entity_ids:
        c_status = _final_status(conservative_df, entity_id)
        t_status = _final_status(time_warp_df, entity_id)
        if c_status is None and t_status is None:
            continue
        verdict = "PASS" if c_status == t_status else "FAIL"
        _bucket(
            result,
            ParityFinding(
                check="final_entity_status",
                tier="exact",
                entity_id=entity_id,
                event_type=None,
                conservative_value=c_status,
                time_warp_value=t_status,
                delta=None,
                verdict=verdict,
            ),
        )

    # --- TOLERANCE tier ---
    for entity_id in all_entity_ids:
        c_tt = _travel_time(conservative_df, entity_id)
        t_tt = _travel_time(time_warp_df, entity_id)
        if c_tt is None or t_tt is None:
            continue
        delta = t_tt - c_tt
        verdict = "PASS" if abs(delta) <= config.travel_time_tolerance_ticks else "FAIL"
        _bucket(
            result,
            ParityFinding(
                check="travel_time",
                tier="tolerance",
                entity_id=entity_id,
                event_type=None,
                conservative_value=c_tt,
                time_warp_value=t_tt,
                delta=delta,
                verdict=verdict,
            ),
        )

    for entity_id in link_set_pass_entities:
        c_seq = _link_sequence(conservative_df, entity_id)
        t_seq = _link_sequence(time_warp_df, entity_id)
        dist = levenshtein(c_seq, t_seq)
        if dist == 0:
            verdict = "PASS"
        elif dist <= config.link_sequence_edit_distance_flag_threshold:
            verdict = "FLAG"
        else:
            verdict = "FAIL"
        _bucket(
            result,
            ParityFinding(
                check="link_sequence_edit_distance",
                tier="tolerance",
                entity_id=entity_id,
                event_type=None,
                conservative_value=list(c_seq),
                time_warp_value=list(t_seq),
                delta=dist,
                verdict=verdict,
            ),
        )

    all_event_types = sorted(set(conservative_df["event_type"]) | set(time_warp_df["event_type"]))
    c_counts = conservative_df["event_type"].value_counts().to_dict()
    t_counts = time_warp_df["event_type"].value_counts().to_dict()
    for event_type in all_event_types:
        c_val = c_counts.get(event_type, 0)
        t_val = t_counts.get(event_type, 0)
        if c_val == 0:
            pct_delta = 0.0 if t_val == 0 else 100.0
        else:
            pct_delta = abs(t_val - c_val) / c_val * 100.0
        verdict = "PASS" if pct_delta <= config.row_count_tolerance_pct else "FAIL"
        _bucket(
            result,
            ParityFinding(
                check="row_count_per_event_type",
                tier="tolerance",
                entity_id=None,
                event_type=event_type,
                conservative_value=c_val,
                time_warp_value=t_val,
                delta=pct_delta,
                verdict=verdict,
            ),
        )

    return result
