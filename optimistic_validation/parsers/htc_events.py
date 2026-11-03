"""Parses HTC's Parquet report output into a common comparison schema, covering every
`event_type` (unlike `tools/sumo_validation/parsers/htc_parquet.py`, which filters down to
`micro_update`/`leave_micro_link` only -- this tool needs the full event stream, since it is
comparing two runs of the *same* engine against each other, not against an external reference
that only has kinematic samples).

Row shape verified against `core/actor/manager/report/ParquetReportData.scala`'s Avro schema
(`entity_id`, `tick`, `real_time_ms`, `lamport_tick`, `event_type`, `simulation_id`, `data` --
`data` is a JSON-encoded string of the report map) and against
`scripts/metrics/htc_report.py`'s streaming reader, which reads the same columns. The tiny
validation scenario is small enough that a single `pd.read_parquet` over the whole directory is
fine -- no need for `htc_report.py`'s batched/streaming aggregation machinery here.

`entity_type` was originally assumed to derive from a `htcaid:<type>;<name>` id convention, per
`core/util/StringPool.scala`'s docstring. **That assumption is stale on this branch**: real
Parquet output from a live run (`tw_livelock_repro_c`/`_o`, 2026-09-07) shows plain numeric
`entity_id` strings (e.g. `"51"`, `"40"`) -- the recent String->Long entity-id migration
(`git log`: "fix: complete String->Long entity id migration across the actor layer") replaced the
old `htcaid:...` id with the routing-layer numeric id everywhere `report()` sends `entityId`, not
just in the shard-routing layer as the original docstring-based assumption expected. There is no
id-string type tag left to parse.

Instead, `entity_type` is inferred per-row from the `data` payload's own id fields, verified by
grepping real rows: `sumo_tripinfo` carries `vehicle_type`/`vType` alongside a matching
`vehicle_id`; `journey_started`/`journey_completed`/`enter_link`/`leave_link` carry type-specific
id fields (`car_id`, `bicycle_id`, ...) where the one matching `entity_id` names the type;
`bus_created` carries `station_id` matching `entity_id` (the event is reported by the station that
spawned the bus, not by the bus itself) and a separate `bus_id` for the new bus. Rows lacking any
matching id field (e.g. `vehicle_event_count`, which only carries `car_id`) still resolve once
that `car_id` matches. Per-entity type is then resolved by majority vote across all of that
entity's rows, so any single ambiguous/typeless row doesn't leave the entity's other rows
unlabeled.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pandas as pd

_COLUMNS = ["event_type", "entity_id", "entity_type", "tick", "lamport_tick", "data"]

# Ordered specific -> generic: a row can carry multiple id fields (e.g. both `car_id` and
# `bicycle_id` keys from the union of every entity that ever reports this event_type), so only
# the one whose *value* matches this row's own `entity_id` is authoritative.
_ID_FIELD_TYPES = [
    ("person_id", "person"),
    ("car_id", "car"),
    ("bicycle_id", "bicycle"),
    ("motorcycle_id", "motorcycle"),
    ("subway_id", "subway"),
    ("bus_id", "bus"),
    ("station_id", "station"),
    ("vehicle_id", "vehicle"),
]


def _row_entity_type(entity_id: str, data: dict) -> str:
    vehicle_type = data.get("vehicle_type") or data.get("vType")
    if vehicle_type and str(data.get("vehicle_id")) == str(entity_id):
        return str(vehicle_type)
    for key, etype in _ID_FIELD_TYPES:
        val = data.get(key)
        if val is not None and str(val) == str(entity_id):
            return etype
    return "unknown"


def _safe_parse(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_htc_events(parquet_dir: Path) -> pd.DataFrame:
    """Reads every Parquet file under `parquet_dir` (HTC's Parquet reporter writes one file per
    writer-actor instance) and returns a DataFrame with columns:
    `event_type, entity_id, entity_type, tick, lamport_tick, data` (the last one a parsed dict,
    not a JSON string)."""
    parquet_dir = Path(parquet_dir)
    if not parquet_dir.exists() or not any(parquet_dir.glob("**/*.parquet")):
        return pd.DataFrame(columns=_COLUMNS)

    raw = pd.read_parquet(parquet_dir)
    if raw.empty:
        return pd.DataFrame(columns=_COLUMNS)

    rows = []
    for _, row in raw.iterrows():
        entity_id = str(row["entity_id"])
        data = _safe_parse(row["data"])
        rows.append(
            {
                "event_type": row["event_type"],
                "entity_id": entity_id,
                "entity_type": _row_entity_type(entity_id, data),
                "tick": int(row["tick"]),
                "lamport_tick": int(row["lamport_tick"]),
                "data": data,
            }
        )

    df = pd.DataFrame(rows, columns=_COLUMNS)

    # Resolve one stable entity_type per entity_id by majority vote across its own rows, so a
    # single typeless/ambiguous row (e.g. `vehicle_event_count` before its `car_id` match) never
    # leaves that entity's other rows grouped separately from it.
    votes: dict[str, Counter] = {}
    for entity_id, etype in zip(df["entity_id"], df["entity_type"]):
        if etype == "unknown":
            continue
        votes.setdefault(entity_id, Counter())[etype] += 1
    resolved = {eid: counter.most_common(1)[0][0] for eid, counter in votes.items()}
    df["entity_type"] = df["entity_id"].map(lambda eid: resolved.get(eid, "unknown"))

    return df
