"""Metrics: per-vehicle position/speed RMSE and travel-time delta between HTC and SUMO.

Resolution handling (see README "HTC output resolution is 1 Hz"): HTC's live MICRO tick
loop only reports one sample per vehicle per whole simulated second, while SUMO's FCD
output is at `--step-length 0.1` (10 Hz). Per the task brief: resample the FINER series
(SUMO) DOWN to the coarser grid (HTC's whole-second ticks) -- never interpolate HTC up.
Since SUMO's 0.1s step evenly divides 1s, every whole second is an exact sample in the
SUMO output (no interpolation needed, just row selection).

GEH is deliberately not used here -- GEH is a flow/volume-count validation statistic
(compares aggregate counts against a calibration threshold), not a single-vehicle
trajectory-accuracy statistic. Position/speed RMSE against a synchronized time axis is
the right tool for "how close is one vehicle's continuous trajectory to the reference".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


def resample_sumo_to_htc_grid(sumo_df: pd.DataFrame) -> pd.DataFrame:
    """Keeps only SUMO FCD rows that fall on a whole simulated second (HTC's reporting
    grid), by exact match on the rounded sim_time -- SUMO's step-length 0.1 makes every
    whole second an exact sample, so this is row selection, not interpolation."""
    if sumo_df.empty:
        return sumo_df
    rounded = sumo_df["sim_time"].round(6)
    on_grid = (rounded % 1.0).abs() < 1e-6
    out = sumo_df.loc[on_grid].copy()
    out["sim_time"] = out["sim_time"].round(0)
    return out


@dataclass
class VehicleMetrics:
    vehicle_id: str
    n_samples: int
    position_rmse_m: float
    speed_rmse_mps: float
    htc_travel_time_s: float
    sumo_travel_time_s: float
    travel_time_delta_s: float  # htc - sumo


def _travel_time(df: pd.DataFrame, vehicle_id: str) -> float:
    sub = df[df["vehicle_id"] == vehicle_id]
    if sub.empty:
        return float("nan")
    return float(sub["sim_time"].max() - sub["sim_time"].min())


def compute_vehicle_metrics(htc_df: pd.DataFrame, sumo_df: pd.DataFrame, vehicle_id: str) -> VehicleMetrics:
    htc_v = htc_df[htc_df["vehicle_id"] == vehicle_id][["sim_time", "position_m", "speed_mps"]]
    sumo_v = sumo_df[sumo_df["vehicle_id"] == vehicle_id][["sim_time", "position_m", "speed_mps"]]

    merged = htc_v.merge(sumo_v, on="sim_time", suffixes=("_htc", "_sumo"), how="inner")

    if merged.empty:
        pos_rmse = float("nan")
        speed_rmse = float("nan")
    else:
        pos_err = merged["position_m_htc"] - merged["position_m_sumo"]
        speed_err = merged["speed_mps_htc"] - merged["speed_mps_sumo"]
        pos_rmse = math.sqrt(float((pos_err**2).mean()))
        speed_rmse = math.sqrt(float((speed_err**2).mean()))

    htc_tt = _travel_time(htc_df, vehicle_id)
    sumo_tt = _travel_time(sumo_df, vehicle_id)

    return VehicleMetrics(
        vehicle_id=vehicle_id,
        n_samples=len(merged),
        position_rmse_m=pos_rmse,
        speed_rmse_mps=speed_rmse,
        htc_travel_time_s=htc_tt,
        sumo_travel_time_s=sumo_tt,
        travel_time_delta_s=htc_tt - sumo_tt,
    )


def compute_scenario_metrics(htc_df: pd.DataFrame, sumo_df: pd.DataFrame) -> list[VehicleMetrics]:
    """Computes per-vehicle metrics for every vehicle present in either dataset (HTC's
    output grid is the join key; SUMO is downsampled onto it first)."""
    sumo_resampled = resample_sumo_to_htc_grid(sumo_df)
    vehicle_ids = sorted(set(htc_df["vehicle_id"]) | set(sumo_df["vehicle_id"]))
    return [compute_vehicle_metrics(htc_df, sumo_resampled, vid) for vid in vehicle_ids]


def metrics_to_dataframe(metrics: list[VehicleMetrics]) -> pd.DataFrame:
    return pd.DataFrame([m.__dict__ for m in metrics])


def format_report(scenario_id: str, metrics: list[VehicleMetrics]) -> str:
    df = metrics_to_dataframe(metrics)
    lines = [f"=== {scenario_id} ===", ""]
    lines.append(
        df.to_string(
            index=False,
            formatters={
                "position_rmse_m": "{:.3f}".format,
                "speed_rmse_mps": "{:.3f}".format,
                "htc_travel_time_s": "{:.1f}".format,
                "sumo_travel_time_s": "{:.1f}".format,
                "travel_time_delta_s": "{:+.1f}".format,
            },
        )
    )
    lines.append("")
    valid = df.dropna(subset=["position_rmse_m"])
    if not valid.empty:
        lines.append(f"Mean position RMSE:      {valid['position_rmse_m'].mean():.3f} m")
        lines.append(f"Mean speed RMSE:         {valid['speed_rmse_mps'].mean():.3f} m/s")
        lines.append(f"Mean travel-time delta:  {valid['travel_time_delta_s'].mean():+.2f} s (HTC - SUMO)")
        lines.append(f"Max |travel-time delta|: {valid['travel_time_delta_s'].abs().max():.2f} s")
    return "\n".join(lines)
