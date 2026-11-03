"""Runs SUMO as a plain subprocess against an exported .sumocfg. No traci."""

from __future__ import annotations

import subprocess
from pathlib import Path


class SumoRunResult:
    def __init__(self, fcd_output: Path, returncode: int, stdout: str, stderr: str):
        self.fcd_output = fcd_output
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_sumo(
    sumocfg: Path,
    fcd_output: Path,
    sumo_bin: str = "sumo",
    timeout_s: float = 120.0,
) -> SumoRunResult:
    sumocfg = Path(sumocfg)
    # subprocess is launched with cwd=sumocfg.parent, so the -c argument must be the bare
    # filename, not the (possibly relative-to-repo-root) path we were handed. Passing the
    # full path here silently double-resolves it against the already-chdir'd cwd (e.g.
    # "output/scenario_a/sumo/scenario.sumocfg" looked up *inside*
    # "output/scenario_a/sumo/") and SUMO exits 1 with "Could not access configuration",
    # leaving whatever FCD file already happened to be on disk from a previous run silently
    # unrefreshed. Caught by re-running the harness end-to-end, not by the (previously
    # nonexistent) unit test for this module — see test_sumo_runner.py.
    result = subprocess.run(
        [sumo_bin, "-c", sumocfg.name],
        cwd=sumocfg.parent,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return SumoRunResult(
        fcd_output=Path(fcd_output),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
