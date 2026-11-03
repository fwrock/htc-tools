import xml.etree.ElementTree as ET

import pytest

from scenario.scenario_a import build_scenario as build_a
from scenario.scenario_b import build_scenario as build_b
from sumo_export.export import export_sumo

pytestmark = pytest.mark.sumo


def _netconvert_available() -> bool:
    import shutil

    return shutil.which("netconvert") is not None


@pytest.mark.skipif(not _netconvert_available(), reason="netconvert not on PATH")
def test_export_sumo_edge_length_and_speed_match_scenario_1to1(tmp_path):
    scenario = build_a()
    paths = export_sumo(scenario, tmp_path / "a")

    net = ET.parse(paths["net_xml"])
    edges = {e.get("id"): e for e in net.getroot().findall("edge") if e.get("function") != "internal"}
    link = scenario.links[0]
    edge = edges[link.link_id]
    lane = edge.find("lane")
    assert float(lane.get("length")) == pytest.approx(link.length_m, abs=0.5)
    assert float(lane.get("speed")) == pytest.approx(link.speed_limit_kmh / 3.6, abs=0.01)


@pytest.mark.skipif(not _netconvert_available(), reason="netconvert not on PATH")
def test_export_sumo_vtype_krauss_params_match_scenario_1to1(tmp_path):
    scenario = build_a()
    paths = export_sumo(scenario, tmp_path / "a")

    rou = ET.parse(paths["rou_xml"])
    vtype = rou.getroot().find("vType")
    vt = scenario.vehicle_type("car_krauss")
    assert vtype.get("carFollowModel") == "Krauss"
    assert float(vtype.get("accel")) == vt.max_accel_mps2
    assert float(vtype.get("decel")) == vt.max_decel_mps2
    assert float(vtype.get("minGap")) == vt.min_gap_m
    assert float(vtype.get("tau")) == vt.reaction_time_s
    assert float(vtype.get("sigma")) == 0.0, "sigma must be 0 for determinism"


@pytest.mark.skipif(not _netconvert_available(), reason="netconvert not on PATH")
def test_export_sumo_vehicle_departs_match_scenario_depart_ticks(tmp_path):
    scenario = build_a()
    paths = export_sumo(scenario, tmp_path / "a")

    rou = ET.parse(paths["rou_xml"])
    departs = {v.get("id"): float(v.get("depart")) for v in rou.getroot().findall("vehicle")}
    for vehicle in scenario.vehicles:
        assert departs[vehicle.vehicle_id] == vehicle.depart_tick


@pytest.mark.skipif(not _netconvert_available(), reason="netconvert not on PATH")
def test_export_sumo_scenario_b_writes_a_signal_program_with_a_red_phase(tmp_path):
    scenario = build_b()
    export_sumo(scenario, tmp_path / "b")

    tll = (tmp_path / "b" / "scenario.tll.xml").read_text()
    assert 'state="G"' in tll
    assert 'state="r"' in tll
