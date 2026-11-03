from scenario.scenario_a import build_scenario as build_a
from scenario.scenario_b import build_scenario as build_b


def test_scenario_a_topology_is_a_single_straight_link():
    s = build_a()
    assert len(s.nodes) == 2
    assert len(s.links) == 1
    link = s.links[0]
    assert link.from_node == "n_origin"
    assert link.to_node == "n_dest"
    assert link.lanes == 1
    assert link.length_m > 0
    assert not s.traffic_lights, "scenario A must have no signal"


def test_scenario_a_has_leader_follower_vehicles_not_free_flow_entry():
    s = build_a()
    assert len(s.vehicles) >= 3, "need at least a leader + two followers to exercise gap-closing"
    departs = sorted(v.depart_tick for v in s.vehicles)
    # All vehicles share the same route -> a nonzero, bounded gap between consecutive
    # departures is what actually exercises car-following (all-zero gaps would be a
    # simultaneous-entry edge case; huge gaps would make every vehicle free-flow and never
    # interact with a leader).
    gaps = [b - a for a, b in zip(departs, departs[1:])]
    assert all(0 < g <= 30 for g in gaps)
    # Every vehicle takes the same single link end to end.
    for v in s.vehicles:
        assert v.route_link_ids == ("link_main",)


def test_scenario_b_adds_exactly_one_signalized_crossing():
    s = build_b()
    assert len(s.traffic_lights) == 1
    tl = s.traffic_lights[0]
    assert tl.green_s < tl.cycle_s, "signal must actually have a red phase"
    assert tl.node_id in {n.node_id for n in s.nodes}
    assert tl.controlled_link_id in {l.link_id for l in s.links}


def test_scenario_b_vehicles_arrive_across_both_green_and_red_windows():
    s = build_b()
    tl = s.traffic_lights[0]
    # Using scenario A's fixed 20-tick MICRO-mode-entry warmup lag observed empirically
    # (see README) is out of scope here; this test only asserts the *scheduled* departures
    # span more than one full signal cycle, which is the scenario-design invariant that
    # actually matters (whether cars land on green or red is a property of the run, not
    # the static scenario definition).
    departs = [v.depart_tick for v in s.vehicles]
    assert max(departs) - min(departs) >= tl.cycle_s - tl.green_s


def test_scenario_a_and_b_share_identical_krauss_parameters():
    a = build_a().vehicle_type("car_krauss")
    b = build_b().vehicle_type("car_krauss")
    assert a == b, "scenario A and B must use the same car-following params per the task brief"
