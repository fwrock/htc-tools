import json

import pyarrow as pa
import pyarrow.parquet as pq

from parsers.htc_events import parse_htc_events


def _write_sample(path):
    # Real Parquet output (verified against a live `tw_livelock_repro` run, 2026-09-07) uses
    # plain numeric entity_id strings post the String->Long entity-id migration -- there is no
    # `htcaid:<type>;<name>` prefix left to parse. entity_type must instead be inferred from the
    # `data` payload's own id fields (see parsers/htc_events.py's module docstring).
    rows = [
        {
            "entity_id": "40",
            "tick": 5,
            "real_time_ms": 0,
            "lamport_tick": 5,
            "event_type": "journey_started",
            "simulation_id": "test",
            "data": json.dumps({"event_type": "journey_started", "car_id": "40", "vehicle_id": "40"}),
        },
        {
            "entity_id": "40",
            "tick": 6,
            "real_time_ms": 0,
            "lamport_tick": 6,
            "event_type": "enter_link",
            "simulation_id": "test",
            "data": json.dumps({"link_id": 25, "car_id": "40"}),
        },
        {
            "entity_id": "40",
            "tick": 20,
            "real_time_ms": 0,
            "lamport_tick": 20,
            "event_type": "journey_completed",
            "simulation_id": "test",
            "data": json.dumps({"event_type": "journey_completed", "car_id": "40", "status": "Completed"}),
        },
        {
            "entity_id": "36",
            "tick": 0,
            "real_time_ms": 0,
            "lamport_tick": 0,
            "event_type": "bus_created",
            "simulation_id": "test",
            "data": json.dumps({"event_type": "bus_created", "station_id": "36", "bus_id": 37}),
        },
        {
            "entity_id": "99",
            "tick": 1,
            "real_time_ms": 0,
            "lamport_tick": 1,
            "event_type": "person_leg_metrics",
            "simulation_id": "test",
            "data": None,
        },
    ]
    table = pa.Table.from_pylist(rows)
    path.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path / "sample.parquet")


def test_parse_htc_events_returns_all_event_types_unfiltered(tmp_path):
    _write_sample(tmp_path)
    df = parse_htc_events(tmp_path)

    assert len(df) == 5
    assert set(df["event_type"]) == {
        "journey_started",
        "enter_link",
        "journey_completed",
        "bus_created",
        "person_leg_metrics",
    }


def test_parse_htc_events_derives_entity_type_from_matching_id_field(tmp_path):
    _write_sample(tmp_path)
    df = parse_htc_events(tmp_path)

    car_rows = df[df["entity_id"] == "40"]
    assert set(car_rows["entity_type"]) == {"car"}

    # bus_created is reported by the station (entity_id matches station_id, not bus_id).
    station_rows = df[df["entity_id"] == "36"]
    assert set(station_rows["entity_type"]) == {"station"}


def test_parse_htc_events_falls_back_to_unknown_entity_type_for_unmatched_ids(tmp_path):
    _write_sample(tmp_path)
    df = parse_htc_events(tmp_path)

    other_rows = df[df["entity_id"] == "99"]
    assert list(other_rows["entity_type"]) == ["unknown"]


def test_parse_htc_events_parses_data_json_into_dict(tmp_path):
    _write_sample(tmp_path)
    df = parse_htc_events(tmp_path)

    enter_link_row = df[df["event_type"] == "enter_link"].iloc[0]
    assert enter_link_row["data"] == {"link_id": 25, "car_id": "40"}


def test_parse_htc_events_handles_null_data_gracefully(tmp_path):
    _write_sample(tmp_path)
    df = parse_htc_events(tmp_path)

    null_data_row = df[df["entity_id"] == "99"].iloc[0]
    assert null_data_row["data"] == {}


def test_parse_htc_events_returns_empty_dataframe_with_expected_columns_when_no_parquet_files(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    df = parse_htc_events(empty_dir)

    assert list(df.columns) == ["event_type", "entity_id", "entity_type", "tick", "lamport_tick", "data"]
    assert df.empty
