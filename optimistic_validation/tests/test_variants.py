import json

from variants.simulation_json import write_variant


def _write_base(tmp_path, **overrides):
    payload = {
        "id": "tiny_scenario",
        "name": "Tiny scenario",
        "startTick": 0,
        "endTick": 86400,
        "timeManagerType": "discrete-event",
        "actorsDataSources": [{"id": "nodes_0"}, {"id": "links_0"}],
        "randomSeed": 42,
        **overrides,
    }
    base_path = tmp_path / "simulation.json"
    base_path.write_text(json.dumps(payload, indent=2))
    return base_path, payload


def test_write_variant_sets_only_the_time_manager_type_field(tmp_path):
    base_path, base_payload = _write_base(tmp_path)

    out_path = write_variant(base_path, "time-warp", tmp_path / "time_warp_simulation.json")
    written = json.loads(out_path.read_text())

    assert written["timeManagerType"] == "time-warp"
    expected = dict(base_payload)
    expected["timeManagerType"] = "time-warp"
    assert written == expected


def test_two_variants_are_identical_except_the_time_manager_type_field(tmp_path):
    base_path, _ = _write_base(tmp_path)

    conservative = write_variant(base_path, "discrete-event", tmp_path / "conservative.json")
    time_warp = write_variant(base_path, "time-warp", tmp_path / "time_warp.json")

    conservative_payload = json.loads(conservative.read_text())
    time_warp_payload = json.loads(time_warp.read_text())

    diff_keys = {
        key
        for key in conservative_payload
        if conservative_payload.get(key) != time_warp_payload.get(key)
    }
    assert diff_keys == {"timeManagerType"}
    assert conservative_payload["timeManagerType"] == "discrete-event"
    assert time_warp_payload["timeManagerType"] == "time-warp"


def test_write_variant_creates_parent_directories(tmp_path):
    base_path, _ = _write_base(tmp_path)
    out_path = tmp_path / "nested" / "dir" / "variant.json"

    result = write_variant(base_path, "time-warp", out_path)

    assert result == out_path
    assert out_path.exists()
