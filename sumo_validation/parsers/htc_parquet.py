"""Parses HTC's Parquet report output for MICRO car events into the common schema:
`sim_time, vehicle_id, position_m, speed_mps, link_id`.

Column/label names verified by reading the actual `report()` call sites, not guessed:
  - `CarMicroHandler.handleMicroUpdate` reports label="micro_update" with a data map
    containing `position`, `velocity`, `link_id`, `tick`, `car_id`, `lane`, `sub_tick`
    (hyperbolic-time-chamber/src/main/scala/model/hybrid/support/car/CarMicroHandler.scala).
  - `CarMicroHandler.handleMicroLeaveLink` reports label="leave_micro_link" with
    `final_position`, `final_velocity`, `link_id`, `tick`, `car_id`.
  - The Parquet row shape itself (entity_id/tick/lamport_tick/event_type/simulation_id/data)
    is from `core/actor/manager/report/ParquetReportData.scala`'s Avro schema; `data` is a
    JSON-encoded string of the report map, decoded here.

IMPORTANT verified-by-run finding (see tools/sumo_validation/README.md "HTC output
resolution is 1 Hz, not the internal 0.1s sub-tick"): `DefaultMicroSimulationStrategy.
processMicroLane` (the actual live MICRO tick loop wired up via Link.scala ->
LinkMicroSimulationHandler) only returns a `MicroVehicleUpdate` for the LAST sub-tick of
each global tick (`if (newPosition >= linkLength || subTick >= microTicksPerGlobalTick - 1)`),
so even with `HTC_MICRO_UPDATE_REPORT_EVERY=1` there is exactly one `micro_update` row per
car per global (1-second) tick, not one per 0.1s sub-tick. The internal car-following math
still steps at 0.1s, but that resolution is not observable in the reported output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

_CAR_ID_RE = re.compile(r"^htcaid_car_(.+)$")


def _strip_car_prefix(entity_id: str) -> str:
    m = _CAR_ID_RE.match(entity_id)
    return m.group(1) if m else entity_id


def parse_htc_parquet(parquet_dir: Path) -> pd.DataFrame:
    """Reads every Parquet file under `parquet_dir` (a directory, since HTC's Parquet
    reporter writes one file per writer-actor instance) and returns a DataFrame with
    columns: sim_time, vehicle_id, position_m, speed_mps, link_id, event_type.
    """
    parquet_dir = Path(parquet_dir)
    raw = pd.read_parquet(parquet_dir)
    if raw.empty:
        return pd.DataFrame(
            columns=["sim_time", "vehicle_id", "position_m", "speed_mps", "link_id", "event_type"]
        )

    rows = []
    for _, row in raw.iterrows():
        if row["event_type"] not in ("micro_update", "leave_micro_link"):
            continue
        data = json.loads(row["data"]) if row["data"] else {}
        vehicle_id = _strip_car_prefix(row["entity_id"])
        if row["event_type"] == "micro_update":
            position = data.get("position")
            speed = data.get("velocity")
        else:  # leave_micro_link
            position = data.get("final_position")
            speed = data.get("final_velocity")
        if position is None or speed is None:
            continue
        rows.append(
            {
                "sim_time": float(row["tick"]),
                "vehicle_id": vehicle_id,
                "position_m": float(position),
                "speed_mps": float(speed),
                "link_id": data.get("link_id"),
                "event_type": row["event_type"],
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # A tick can carry both a micro_update and a leave_micro_link row for the same
    # vehicle (the link's exit is detected within the same sub-tick loop that produced
    # the last micro_update); prefer leave_micro_link's authoritative final position.
    priority = {"micro_update": 0, "leave_micro_link": 1}
    df["_priority"] = df["event_type"].map(priority)
    df = df.sort_values(["vehicle_id", "sim_time", "_priority"])
    df = df.drop_duplicates(subset=["vehicle_id", "sim_time"], keep="last")
    return df.drop(columns=["event_type", "_priority"]).reset_index(drop=True)
