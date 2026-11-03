"""Writes a `simulation.json` variant that differs from the base scenario in exactly one
field: `timeManagerType`.

Field name and literal values verified by reading the Scala source directly (not guessed):
  - `core/entity/configuration/Simulation.scala`'s `timeManagerType: String` field -- Jackson's
    default case-class-field-name mapping (no `@JsonProperty` override present) means the JSON
    key is exactly `timeManagerType`.
  - `core/enumeration/TimeManagerTypeEnum.scala`: `DISCRETE_EVENT = "discrete-event"`,
    `TIME_WARP = "time-warp"`.
"""

from __future__ import annotations

import json
from pathlib import Path

TIME_MANAGER_TYPE_KEY = "timeManagerType"


def write_variant(base_simulation_json: Path, time_manager_type: str, out_path: Path) -> Path:
    """Loads `base_simulation_json`, sets `timeManagerType` to `time_manager_type`, and writes
    the result to `out_path`. Every other field/ordering is preserved byte-for-byte relative to
    what `json.load`+`json.dump` round-trips (whitespace/indentation is normalized to match the
    base file's own `json.dumps(..., indent=2)` convention, not literally byte-identical to the
    source file if the source used different formatting)."""
    base_simulation_json = Path(base_simulation_json)
    out_path = Path(out_path)

    payload = json.loads(base_simulation_json.read_text())
    payload[TIME_MANAGER_TYPE_KEY] = time_manager_type

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return out_path
