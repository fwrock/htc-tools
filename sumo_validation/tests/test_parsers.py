import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from parsers.htc_parquet import parse_htc_parquet
from parsers.sumo_fcd import parse_sumo_fcd


def _write_htc_parquet_sample(path):
    rows = [
        {
            "entity_id": "htcaid_car_car_0",
            "tick": 5,
            "real_time_ms": 0,
            "lamport_tick": 5,
            "event_type": "micro_update",
            "simulation_id": "test",
            "data": json.dumps({"position": 12.5, "velocity": 3.0, "link_id": "htcaid_link_link_main"}),
        },
        {
            "entity_id": "htcaid_car_car_0",
            "tick": 6,
            "real_time_ms": 0,
            "lamport_tick": 6,
            "event_type": "micro_update",
            "simulation_id": "test",
            "data": json.dumps({"position": 16.0, "velocity": 3.5, "link_id": "htcaid_link_link_main"}),
        },
        {
            "entity_id": "htcaid_car_car_0",
            "tick": 6,
            "real_time_ms": 0,
            "lamport_tick": 7,
            "event_type": "leave_micro_link",
            "simulation_id": "test",
            "data": json.dumps(
                {"final_position": 16.2, "final_velocity": 3.6, "link_id": "htcaid_link_link_main"}
            ),
        },
        {
            "entity_id": "htcaid_car_car_0",
            "tick": 6,
            "real_time_ms": 0,
            "lamport_tick": 8,
            "event_type": "journey_started",
            "simulation_id": "test",
            "data": json.dumps({"note": "should be filtered out, has no position field"}),
        },
    ]
    table = pa.Table.from_pylist(rows)
    path.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path / "sample.parquet")


def test_parse_htc_parquet_extracts_common_schema(tmp_path):
    _write_htc_parquet_sample(tmp_path)
    df = parse_htc_parquet(tmp_path)

    assert list(df.columns) == ["sim_time", "vehicle_id", "position_m", "speed_mps", "link_id"]
    assert set(df["vehicle_id"]) == {"car_0"}
    assert len(df) == 2, "one row for tick 5, one merged row for tick 6 (leave wins over update)"


def test_parse_htc_parquet_prefers_leave_micro_link_over_micro_update_at_same_tick(tmp_path):
    _write_htc_parquet_sample(tmp_path)
    df = parse_htc_parquet(tmp_path)

    row_tick6 = df[df["sim_time"] == 6].iloc[0]
    # leave_micro_link's final_position (16.2) must win over micro_update's position (16.0)
    # -- it is HTC's authoritative end-of-link value (see module docstring).
    assert row_tick6["position_m"] == 16.2
    assert row_tick6["speed_mps"] == 3.6


def test_parse_htc_parquet_ignores_non_kinematic_events(tmp_path):
    _write_htc_parquet_sample(tmp_path)
    df = parse_htc_parquet(tmp_path)
    assert 8 not in set(df["sim_time"]), "journey_started (no position field) must be dropped"


_FCD_SAMPLE = """<?xml version="1.0"?>
<fcd-export>
    <timestep time="0.00">
        <vehicle id="car_0" x="0.00" y="0.00" angle="90.00" type="car_krauss" speed="0.00" pos="0.00" lane="link_main_0"/>
    </timestep>
    <timestep time="0.10">
        <vehicle id="car_0" x="0.03" y="0.00" angle="90.00" type="car_krauss" speed="0.26" pos="0.03" lane="link_main_0"/>
    </timestep>
    <timestep time="1.00">
        <vehicle id="car_0" x="2.30" y="0.00" angle="90.00" type="car_krauss" speed="2.60" pos="2.30" lane="link_main_0"/>
    </timestep>
</fcd-export>
"""


def test_parse_sumo_fcd_extracts_position_and_speed_directly_from_pos_attribute(tmp_path):
    fcd_path = tmp_path / "fcd.xml"
    fcd_path.write_text(_FCD_SAMPLE)

    df = parse_sumo_fcd(fcd_path)

    assert list(df.columns) == ["sim_time", "vehicle_id", "position_m", "speed_mps", "link_id"]
    assert len(df) == 3
    row = df[df["sim_time"] == 1.0].iloc[0]
    assert row["position_m"] == 2.30
    assert row["speed_mps"] == 2.60
    assert row["link_id"] == "link_main"
