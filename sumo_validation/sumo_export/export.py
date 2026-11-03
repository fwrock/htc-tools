"""Canonical Scenario -> SUMO scenario files (.nod.xml/.edg.xml/.con.xml/.tll.xml, compiled
to .net.xml via `netconvert`, plus .rou.xml and .sumocfg).

Deliberate choices (see tools/sumo_validation/README.md for full rationale):
  - vType `sigma="0"` (deterministic, no driver-imperfection randomness) to match the
    determinism goal on the HTC side, without trying to match RNG seeds directly.
  - vType `carFollowModel="Krauss"` (SUMO's own default, so this is explicit rather than
    implicit) with accel/decel/minGap/tau taken 1:1 from the same `VehicleType` HTC's
    exporter reads, and `maxSpeed` set equal to the link's speed limit converted to m/s
    (HTC's live MICRO path always uses `desiredVelocity = link.speedLimit / 3.6`, i.e. the
    vehicle's own free-flow speed is not actually distinguishable from the link speed limit
    on the HTC side today -- see htc_export/export.py docstring).
  - `--step-length 0.1` and (see README) SUMO's classic (non-ballistic) integration is used
    deliberately, NOT `--step-method.ballistic`: HTC's live MICRO tick loop does not
    actually call `KraussModel.updateState`'s trapezoidal-average integration -- it computes
    a capped velocity and then advances position with `newPosition = position +
    cappedVelocity * microTimeStep` (i.e. using the *new* velocity directly, matching SUMO's
    classic Euler-like default, not the ballistic/trapezoidal one).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scenario.model import Scenario


def _kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def _write_nod_xml(scenario: Scenario, path: Path) -> None:
    controlled_nodes = {tl.node_id for tl in scenario.traffic_lights}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<nodes>"]
    for n in scenario.nodes:
        node_type = ' type="traffic_light"' if n.node_id in controlled_nodes else ""
        lines.append(f'    <node id="{n.node_id}" x="{n.x}" y="{n.y}"{node_type}/>')
    lines.append("</nodes>")
    path.write_text("\n".join(lines) + "\n")


def _write_edg_xml(scenario: Scenario, path: Path) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<edges>"]
    for l in scenario.links:
        speed_ms = _kmh_to_ms(l.speed_limit_kmh)
        lines.append(
            f'    <edge id="{l.link_id}" from="{l.from_node}" to="{l.to_node}" '
            f'numLanes="{l.lanes}" speed="{speed_ms}" length="{l.length_m}"/>'
        )
    lines.append("</edges>")
    path.write_text("\n".join(lines) + "\n")


def _write_tll_xml(scenario: Scenario, path: Path) -> Path | None:
    if not scenario.traffic_lights:
        return None
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tlLogics>"]
    for tl in scenario.traffic_lights:
        red_s = tl.cycle_s - tl.green_s
        lines.append(
            f'    <tlLogic id="{tl.node_id}" type="static" programID="0" offset="{tl.offset_s}">'
        )
        lines.append(f'        <phase duration="{tl.green_s}" state="G"/>')
        lines.append(f'        <phase duration="{red_s}" state="r"/>')
        lines.append("    </tlLogic>")
    lines.append("</tlLogics>")
    path.write_text("\n".join(lines) + "\n")
    return path


def _write_rou_xml(scenario: Scenario, path: Path) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<routes>"]
    for vt in scenario.vehicle_types:
        lines.append(
            f'    <vType id="{vt.type_id}" carFollowModel="Krauss" '
            f'accel="{vt.max_accel_mps2}" decel="{vt.max_decel_mps2}" '
            f'minGap="{vt.min_gap_m}" tau="{vt.reaction_time_s}" '
            f'length="{vt.length_m}" sigma="{vt.sigma}" '
            f'maxSpeed="{_kmh_to_ms(scenario.links[0].speed_limit_kmh)}"/>'
        )
    for v in sorted(scenario.vehicles, key=lambda v: v.depart_tick):
        edges = " ".join(v.route_link_ids)
        lines.append(
            f'    <vehicle id="{v.vehicle_id}" type="{v.vehicle_type}" '
            f'depart="{v.depart_tick}" departSpeed="{v.initial_velocity_mps}">'
        )
        lines.append(f'        <route edges="{edges}"/>')
        lines.append("    </vehicle>")
    lines.append("</routes>")
    path.write_text("\n".join(lines) + "\n")


def _write_sumocfg(
    net_path: Path, rou_path: Path, fcd_path: Path, duration_s: int, cfg_path: Path
) -> None:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file value="{net_path.name}"/>
        <route-files value="{rou_path.name}"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="{duration_s}"/>
        <step-length value="0.1"/>
    </time>
    <processing>
        <default.carfollowmodel value="Krauss"/>
        <collision.action value="warn"/>
    </processing>
    <output>
        <fcd-output value="{fcd_path.name}"/>
    </output>
</configuration>
"""
    cfg_path.write_text(xml)


def export_sumo(scenario: Scenario, out_dir: Path, netconvert_bin: str = "netconvert") -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nod_path = out_dir / "scenario.nod.xml"
    edg_path = out_dir / "scenario.edg.xml"
    net_path = out_dir / "scenario.net.xml"
    rou_path = out_dir / "scenario.rou.xml"
    fcd_path = out_dir / "scenario_fcd.xml"
    cfg_path = out_dir / "scenario.sumocfg"

    _write_nod_xml(scenario, nod_path)
    _write_edg_xml(scenario, edg_path)
    tll_path = _write_tll_xml(scenario, out_dir / "scenario.tll.xml")

    cmd = [
        netconvert_bin,
        "--node-files", str(nod_path),
        "--edge-files", str(edg_path),
        "--output-file", str(net_path),
        "--no-turnarounds",
        "--tls.guess-signals", "false",
    ]
    if tll_path is not None:
        cmd += ["--tllogic-files", str(tll_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"netconvert failed (exit {result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    _write_rou_xml(scenario, rou_path)
    _write_sumocfg(net_path, rou_path, fcd_path, scenario.duration_s, cfg_path)

    return {
        "net_xml": net_path,
        "rou_xml": rou_path,
        "sumocfg": cfg_path,
        "fcd_output": fcd_path,
        "out_dir": out_dir,
    }
