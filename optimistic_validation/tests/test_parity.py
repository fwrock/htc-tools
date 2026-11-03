import pandas as pd

from metrics.parity import ParityConfig, compare_runs, levenshtein


def _row(entity_id, entity_type, event_type, tick, lamport_tick=None, data=None):
    return {
        "event_type": event_type,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "tick": tick,
        "lamport_tick": lamport_tick if lamport_tick is not None else tick,
        "data": data or {},
    }


def _df(rows):
    return pd.DataFrame(rows, columns=["event_type", "entity_id", "entity_type", "tick", "lamport_tick", "data"])


def _identical_trip(entity_id="htcaid:car;0", entity_type="car"):
    return [
        _row(entity_id, entity_type, "journey_started", 0, data={"link_id": None}),
        _row(entity_id, entity_type, "enter_link", 1, data={"link_id": "link_a"}),
        _row(entity_id, entity_type, "leave_link", 5, data={"link_id": "link_a"}),
        _row(entity_id, entity_type, "enter_link", 6, data={"link_id": "link_b"}),
        _row(entity_id, entity_type, "leave_link", 10, data={"link_id": "link_b", "status": "Completed"}),
        _row(entity_id, entity_type, "journey_completed", 10, data={"status": "Completed"}),
    ]


def test_identical_runs_produce_only_passes():
    rows = _identical_trip()
    cons = _df(rows)
    tw = _df(rows)

    result = compare_runs(cons, tw, ParityConfig())

    assert not result.exact_fails
    assert not result.tolerance_fails
    assert not result.flags
    assert result.passes


def test_exact_tier_mismatch_on_completed_trip_count_is_a_fail():
    cons = _df(_identical_trip())
    tw = _df([])  # time-warp never completed the trip

    result = compare_runs(cons, tw, ParityConfig())

    fails = [f for f in result.exact_fails if f.check == "completed_trip_count_per_entity_type"]
    assert len(fails) == 1
    assert fails[0].verdict == "FAIL"
    assert fails[0].conservative_value == 1
    assert fails[0].time_warp_value == 0


def test_travel_time_within_tolerance_passes():
    cons = _df(_identical_trip())
    tw_rows = _identical_trip()
    # Shift the completion tick by 1 (within the default 2-tick tolerance).
    tw_rows[-1] = {**tw_rows[-1], "tick": 11, "lamport_tick": 11}
    tw = _df(tw_rows)

    result = compare_runs(cons, tw, ParityConfig(travel_time_tolerance_ticks=2))

    travel_findings = [f for f in result.passes if f.check == "travel_time"]
    assert len(travel_findings) == 1
    assert travel_findings[0].verdict == "PASS"


def test_travel_time_outside_tolerance_fails():
    cons = _df(_identical_trip())
    tw_rows = _identical_trip()
    tw_rows[-1] = {**tw_rows[-1], "tick": 20, "lamport_tick": 20}
    tw = _df(tw_rows)

    result = compare_runs(cons, tw, ParityConfig(travel_time_tolerance_ticks=2))

    travel_findings = [f for f in result.tolerance_fails if f.check == "travel_time"]
    assert len(travel_findings) == 1
    assert travel_findings[0].verdict == "FAIL"


def test_link_sequence_single_swap_is_flagged_not_failed():
    # Same link SET (link_a, link_b) on both sides -- conservative revisits link_a a second time
    # (a plausible real-world artifact), giving a sequence one element longer than time-warp's.
    # Levenshtein(("a","b","a"), ("a","b")) == 1 (a single trailing delete), so this is a clean
    # edit-distance-1 case while the exact-tier visited-link SET still matches on both sides.
    entity_id = "htcaid:car;0"
    cons = _df(
        [
            _row(entity_id, "car", "enter_link", 1, data={"link_id": "link_a"}),
            _row(entity_id, "car", "enter_link", 2, data={"link_id": "link_b"}),
            _row(entity_id, "car", "enter_link", 3, data={"link_id": "link_a"}),
        ]
    )
    tw = _df(
        [
            _row(entity_id, "car", "enter_link", 1, data={"link_id": "link_a"}),
            _row(entity_id, "car", "enter_link", 2, data={"link_id": "link_b"}),
        ]
    )

    result = compare_runs(cons, tw, ParityConfig(link_sequence_edit_distance_flag_threshold=1))

    set_findings = [f for f in result.exact_fails if f.check == "distinct_links_visited_set"]
    assert set_findings == []  # sets match -> the sequence check is allowed to run

    seq_findings = [f for f in result.flags if f.check == "link_sequence_edit_distance"]
    assert len(seq_findings) == 1
    assert seq_findings[0].verdict == "FLAG"
    assert seq_findings[0].delta == 1


def test_link_sequence_edit_distance_only_computed_when_link_set_already_matches():
    entity_id = "htcaid:car;0"
    cons = _df([_row(entity_id, "car", "enter_link", 1, data={"link_id": "link_a"})])
    tw = _df([_row(entity_id, "car", "enter_link", 1, data={"link_id": "link_z"})])

    result = compare_runs(cons, tw, ParityConfig())

    set_findings = [f for f in result.exact_fails if f.check == "distinct_links_visited_set"]
    assert len(set_findings) == 1

    seq_findings = [f for f in result.all_findings() if f.check == "link_sequence_edit_distance"]
    assert seq_findings == []


def test_row_count_per_event_type_within_tolerance_passes():
    cons_rows = _identical_trip() * 50
    tw_rows = _identical_trip() * 51  # ~2% more rows, within default 2% tolerance is borderline

    cons = _df(cons_rows)
    tw = _df(tw_rows)

    result = compare_runs(cons, tw, ParityConfig(row_count_tolerance_pct=5.0))

    row_count_findings = [f for f in result.all_findings() if f.check == "row_count_per_event_type"]
    assert row_count_findings
    assert all(f.verdict == "PASS" for f in row_count_findings)


def test_row_count_per_event_type_outside_tolerance_fails():
    cons = _df(_identical_trip() * 100)
    tw = _df(_identical_trip() * 50)  # 50% fewer rows

    result = compare_runs(cons, tw, ParityConfig(row_count_tolerance_pct=2.0))

    row_count_fails = [f for f in result.tolerance_fails if f.check == "row_count_per_event_type"]
    assert row_count_fails


def test_levenshtein_hand_computed_cases():
    assert levenshtein((), ()) == 0
    assert levenshtein(("a",), ()) == 1
    assert levenshtein(("a", "b"), ("b", "a")) == 2
    assert levenshtein(("a", "b", "c"), ("a", "x", "c")) == 1
