import math

import pandas as pd

from metrics.rmse import (
    compute_scenario_metrics,
    compute_vehicle_metrics,
    resample_sumo_to_htc_grid,
)


def test_resample_sumo_to_htc_grid_keeps_only_whole_seconds():
    sumo_df = pd.DataFrame(
        {
            "sim_time": [0.0, 0.1, 0.2, 1.0, 1.1, 2.0],
            "vehicle_id": ["v"] * 6,
            "position_m": [0, 1, 2, 3, 4, 5],
            "speed_mps": [0, 1, 2, 3, 4, 5],
            "link_id": ["l"] * 6,
        }
    )
    out = resample_sumo_to_htc_grid(sumo_df)
    assert sorted(out["sim_time"].tolist()) == [0.0, 1.0, 2.0]


def test_compute_vehicle_metrics_matches_hand_computed_rmse():
    # HTC: position 0, 10, 20 at t=0,1,2. SUMO: position 1, 9, 22 at the same ticks.
    # errors: -1, +1, -2 -> RMSE = sqrt((1+1+4)/3) = sqrt(2) ~= 1.41421356
    htc_df = pd.DataFrame(
        {
            "sim_time": [0, 1, 2],
            "vehicle_id": ["car_0"] * 3,
            "position_m": [0.0, 10.0, 20.0],
            "speed_mps": [0.0, 10.0, 10.0],
            "link_id": ["l"] * 3,
        }
    )
    sumo_df = pd.DataFrame(
        {
            "sim_time": [0, 1, 2],
            "vehicle_id": ["car_0"] * 3,
            "position_m": [1.0, 9.0, 22.0],
            "speed_mps": [0.0, 9.0, 12.0],
            "link_id": ["l"] * 3,
        }
    )

    m = compute_vehicle_metrics(htc_df, sumo_df, "car_0")

    assert m.n_samples == 3
    assert math.isclose(m.position_rmse_m, math.sqrt(2.0), rel_tol=1e-9)
    # speed errors: 0, +1, -2 -> RMSE = sqrt((0+1+4)/3) = sqrt(5/3)
    assert math.isclose(m.speed_rmse_mps, math.sqrt(5.0 / 3.0), rel_tol=1e-9)
    assert m.htc_travel_time_s == 2.0
    assert m.sumo_travel_time_s == 2.0
    assert m.travel_time_delta_s == 0.0


def test_compute_vehicle_metrics_travel_time_delta_sign_is_htc_minus_sumo():
    htc_df = pd.DataFrame(
        {"sim_time": [10, 15], "vehicle_id": ["car_0"] * 2, "position_m": [0, 10], "speed_mps": [0, 1], "link_id": ["l"] * 2}
    )
    sumo_df = pd.DataFrame(
        {"sim_time": [10, 20], "vehicle_id": ["car_0"] * 2, "position_m": [0, 10], "speed_mps": [0, 1], "link_id": ["l"] * 2}
    )

    m = compute_vehicle_metrics(htc_df, sumo_df, "car_0")

    assert m.htc_travel_time_s == 5.0
    assert m.sumo_travel_time_s == 10.0
    assert m.travel_time_delta_s == -5.0  # HTC finished 5s faster


def test_compute_vehicle_metrics_returns_nan_rmse_when_no_overlapping_ticks():
    htc_df = pd.DataFrame(
        {"sim_time": [0], "vehicle_id": ["car_0"], "position_m": [0.0], "speed_mps": [0.0], "link_id": ["l"]}
    )
    sumo_df = pd.DataFrame(
        {"sim_time": [99], "vehicle_id": ["car_0"], "position_m": [0.0], "speed_mps": [0.0], "link_id": ["l"]}
    )

    m = compute_vehicle_metrics(htc_df, sumo_df, "car_0")

    assert m.n_samples == 0
    assert math.isnan(m.position_rmse_m)
    assert math.isnan(m.speed_rmse_mps)


def test_compute_scenario_metrics_covers_the_union_of_vehicle_ids():
    htc_df = pd.DataFrame(
        {"sim_time": [0], "vehicle_id": ["car_0"], "position_m": [0.0], "speed_mps": [0.0], "link_id": ["l"]}
    )
    sumo_df = pd.DataFrame(
        {"sim_time": [0], "vehicle_id": ["car_1"], "position_m": [0.0], "speed_mps": [0.0], "link_id": ["l"]}
    )
    metrics = compute_scenario_metrics(htc_df, sumo_df)
    assert {m.vehicle_id for m in metrics} == {"car_0", "car_1"}
