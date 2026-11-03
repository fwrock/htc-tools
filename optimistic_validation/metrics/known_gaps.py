"""Detects failures attributable to the known, already-diagnosed Time Warp gap: `Movable` route
state (`movableCurrentPath`/`bestRoute`) is not correctly reconstructed after a rollback (see
`hyperbolic-time-chamber/docs/KNOWN_GAPS.md`, "Time Warp: `Movable` Route State
(`movableCurrentPath`/`bestRoute`) Not Correctly Reconstructed After a Rollback"). This is
deliberately out of scope for this validation tool to fix -- it only needs to be *recognized*, so
genuinely new/unexplained failures aren't masked by an already-tracked one.

Log-line signatures verified by reading the actual Scala source (not the design brief's assumed
wording -- see the deviation note below):
  - `support/bus/BusSignalHandler.scala:83`: `logWarnFn("No current node")`, which
    `BaseActor.logWarn` formats as `"$entityId: No current node"`.
  - `core/actor/SimulationBaseActor.scala:984`: `s"$getEntityId: anti-message for unknown
    messageId=${event.messageId} (already retracted by an earlier overlapping rollback, or a
    genuine protocol anomaly)"`. The design brief's assumed phrase "unknown/already-pruned
    messageId" does not appear verbatim anywhere in the source -- the only real variant is
    "anti-message for unknown messageId=" (no "already-pruned" wording exists). This module
    matches the real line.

Both log lines are entityId-prefixed the same way every `BaseActor.logInfo/logWarn/logError`
call is (`"$entityId: <message>"`), so one shared regex format extracts the id from either.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from metrics.parity import COMPLETION_EVENT_TYPES

_NO_CURRENT_NODE_RE = re.compile(r"([^\s]+): No current node")
_ANTI_MESSAGE_RE = re.compile(r"([^\s]+): anti-message for unknown messageId=")


def _entities_with_gap_signature(log_text: str) -> set[str]:
    found: set[str] = set()
    found.update(_NO_CURRENT_NODE_RE.findall(log_text))
    found.update(_ANTI_MESSAGE_RE.findall(log_text))
    return found


def _entities_with_incomplete_trip(time_warp_df: pd.DataFrame, conservative_df: pd.DataFrame) -> set[str]:
    def completed_entities(df: pd.DataFrame) -> set[str]:
        return set(df[df["event_type"].isin(COMPLETION_EVENT_TYPES)]["entity_id"])

    cons_completed = completed_entities(conservative_df)
    tw_completed = completed_entities(time_warp_df)
    return cons_completed - tw_completed


def detect_movable_rollback_gap(
    time_warp_log_path: Path, time_warp_df: pd.DataFrame, conservative_df: pd.DataFrame
) -> set[str]:
    """Returns the set of `entity_id`s whose failure is attributable to the known Movable
    rollback/replay gap: the entity's log carries one of the gap's two log-line signatures AND
    the entity has an incomplete trip under time-warp (conservative completed it, time-warp did
    not). Either condition alone is not enough -- the log signature can appear for an entity that
    still eventually completes (a rollback that self-corrects), and an incomplete trip can have
    an entirely different cause."""
    time_warp_log_path = Path(time_warp_log_path)
    if not time_warp_log_path.exists():
        return set()

    log_text = time_warp_log_path.read_text(errors="ignore")
    signature_entities = _entities_with_gap_signature(log_text)
    incomplete_entities = _entities_with_incomplete_trip(time_warp_df, conservative_df)

    return signature_entities & incomplete_entities
