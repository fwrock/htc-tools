import pandas as pd

from metrics.known_gaps import detect_movable_rollback_gap


def _row(entity_id, entity_type, event_type, tick):
    return {
        "event_type": event_type,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "tick": tick,
        "lamport_tick": tick,
        "data": {},
    }


def _df(rows):
    return pd.DataFrame(rows, columns=["event_type", "entity_id", "entity_type", "tick", "lamport_tick", "data"])


def _write_log(tmp_path, text):
    log_path = tmp_path / "htc_run.log"
    log_path.write_text(text)
    return log_path


def test_detects_entity_with_both_log_signature_and_incomplete_trip(tmp_path):
    entity_id = "htcaid:bus;mini_loop_bus_0"
    log_path = _write_log(
        tmp_path,
        f"2026-09-05 00:00:01 [WARN] some.Logger - {entity_id}: No current node\n",
    )
    conservative_df = _df([_row(entity_id, "bus", "journey_completed", 100)])
    time_warp_df = _df([_row(entity_id, "bus", "enter_link", 10)])  # never completed

    gap_entities = detect_movable_rollback_gap(log_path, time_warp_df, conservative_df)

    assert gap_entities == {entity_id}


def test_detects_via_anti_message_log_signature(tmp_path):
    entity_id = "htcaid:subway;mini_line_train_0"
    log_path = _write_log(
        tmp_path,
        f"2026-09-05 00:00:02 [WARN] core.actor.SimulationBaseActor - {entity_id}: "
        "anti-message for unknown messageId=42 (already retracted by an earlier overlapping "
        "rollback, or a genuine protocol anomaly)\n",
    )
    conservative_df = _df([_row(entity_id, "subway", "journey_completed", 200)])
    time_warp_df = _df([_row(entity_id, "subway", "enter_link", 5)])

    gap_entities = detect_movable_rollback_gap(log_path, time_warp_df, conservative_df)

    assert gap_entities == {entity_id}


def test_does_not_fire_when_log_signature_present_but_trip_actually_completed(tmp_path):
    entity_id = "htcaid:bus;mini_loop_bus_0"
    log_path = _write_log(tmp_path, f"{entity_id}: No current node\n")
    conservative_df = _df([_row(entity_id, "bus", "journey_completed", 100)])
    # Time-warp logged the gap signature but the trip still completed (self-corrected rollback).
    time_warp_df = _df(
        [
            _row(entity_id, "bus", "enter_link", 10),
            _row(entity_id, "bus", "journey_completed", 105),
        ]
    )

    gap_entities = detect_movable_rollback_gap(log_path, time_warp_df, conservative_df)

    assert gap_entities == set()


def test_does_not_fire_when_trip_incomplete_but_no_log_signature(tmp_path):
    entity_id = "htcaid:bus;mini_loop_bus_0"
    log_path = _write_log(tmp_path, "2026-09-05 00:00:01 [INFO] nothing interesting here\n")
    conservative_df = _df([_row(entity_id, "bus", "journey_completed", 100)])
    time_warp_df = _df([_row(entity_id, "bus", "enter_link", 10)])  # incomplete, but no signature

    gap_entities = detect_movable_rollback_gap(log_path, time_warp_df, conservative_df)

    assert gap_entities == set()


def test_missing_log_file_returns_empty_set(tmp_path):
    conservative_df = _df([_row("htcaid:bus;x", "bus", "journey_completed", 100)])
    time_warp_df = _df([_row("htcaid:bus;x", "bus", "enter_link", 10)])

    gap_entities = detect_movable_rollback_gap(tmp_path / "does_not_exist.log", time_warp_df, conservative_df)

    assert gap_entities == set()


def test_entity_completed_in_both_runs_is_never_flagged_even_with_log_signature(tmp_path):
    entity_id = "htcaid:bus;mini_loop_bus_0"
    log_path = _write_log(tmp_path, f"{entity_id}: No current node\n")
    conservative_df = _df([_row(entity_id, "bus", "journey_completed", 100)])
    time_warp_df = _df([_row(entity_id, "bus", "journey_completed", 100)])

    gap_entities = detect_movable_rollback_gap(log_path, time_warp_df, conservative_df)

    assert gap_entities == set()
