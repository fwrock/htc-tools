"""Resolves which scenario directory to run the conservative-vs-time-warp comparison against.

Either an already-generated scenario directory is reused as-is (`--scenario-dir`), or
`scripts/scenario/generate_tiny_validation_scenario.py` (in the `hyperbolic-time-chamber`
repo) is invoked fresh via `sys.path.insert`, the same import pattern
`tools/sumo_validation/run_validation.py` uses for its own scenario builders -- imported, not
subprocessed, so a real Python traceback surfaces directly instead of being swallowed by a
subprocess boundary.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

HTC_REPO_ROOT = Path(__file__).resolve().parents[3] / "hyperbolic-time-chamber"


@dataclass
class ResolvedScenario:
    scenario_id: str
    scenario_dir: Path
    simulation_json: Path
    city_map_json: Path


def _simulation_json_path(scenario_dir: Path) -> Path:
    candidate = scenario_dir / "simulation.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"No simulation.json found under {scenario_dir}")


def _city_map_json_path(scenario_dir: Path) -> Path:
    candidate = scenario_dir / "data" / "city_map.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"No data/city_map.json found under {scenario_dir}")


def from_existing_dir(scenario_dir: Path) -> ResolvedScenario:
    scenario_dir = Path(scenario_dir).resolve()
    simulation_json = _simulation_json_path(scenario_dir)
    city_map_json = _city_map_json_path(scenario_dir)
    return ResolvedScenario(
        scenario_id=scenario_dir.name,
        scenario_dir=scenario_dir,
        simulation_json=simulation_json,
        city_map_json=city_map_json,
    )


def generate_fresh(output_dir: Path, person_plan_mode: str = "auto") -> ResolvedScenario:
    """Invokes the tiny-validation-scenario generator directly (import, not subprocess) and
    returns the resulting scenario. `output_dir` doubles as the generator's own
    `--data-path-prefix`, so `simulation.json`'s embedded data paths point at itself."""
    sys.path.insert(0, str(HTC_REPO_ROOT / "scripts" / "scenario"))
    from generate_tiny_validation_scenario import build_scenario  # noqa: E402

    output_dir = Path(output_dir).resolve()
    build_scenario(output_dir, str(output_dir), person_plan_mode)
    return from_existing_dir(output_dir)


def resolve(scenario_dir: Path | None, generate: bool, generated_output_dir: Path) -> ResolvedScenario:
    if scenario_dir is not None and generate:
        raise ValueError("Pass exactly one of --scenario-dir or --generate, not both")
    if scenario_dir is not None:
        return from_existing_dir(scenario_dir)
    if generate:
        return generate_fresh(generated_output_dir)
    raise ValueError("Must pass one of --scenario-dir or --generate")
