"""Parses SUMO's FCD (`--fcd-output`) XML into the common schema:
`sim_time, vehicle_id, position_m, speed_mps, link_id`.

Verified against an actual SUMO run (see README "How this was verified"): each
`<vehicle>` element already carries a `pos` attribute that is the along-lane distance
from the lane/edge start (0..edge length) -- exactly HTC's `positionInLink` convention.
No x/y-to-along-link projection is needed even though our links happen to be straight
(SUMO's `pos` is lane-local regardless of geometry). `lane` is `"<edgeId>_<laneIndex>"`;
since every scenario here uses a single lane per edge, stripping the trailing `_<n>`
recovers the HTC link id directly (both exporters use the same link ids on both sides).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd


def parse_sumo_fcd(fcd_path: Path) -> pd.DataFrame:
    rows = []
    for _, elem in ET.iterparse(str(fcd_path), events=("end",)):
        if elem.tag != "timestep":
            continue
        sim_time = float(elem.attrib["time"])
        for veh in elem.findall("vehicle"):
            lane = veh.attrib.get("lane", "")
            link_id = lane.rsplit("_", 1)[0] if "_" in lane else lane
            rows.append(
                {
                    "sim_time": sim_time,
                    "vehicle_id": veh.attrib["id"],
                    "position_m": float(veh.attrib["pos"]),
                    "speed_mps": float(veh.attrib["speed"]),
                    "link_id": link_id,
                }
            )
        elem.clear()
    return pd.DataFrame(rows, columns=["sim_time", "vehicle_id", "position_m", "speed_mps", "link_id"])
