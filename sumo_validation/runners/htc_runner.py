"""Runs HTC standalone (no docker-compose, no Kafka/Redis) against the assembled fatjar.

Verified locally (see README "How this was verified"): `htc.persistence.journal.plugin`
is in-memory by default and cluster sharding only needs a single self-seeded node
(`pekko.cluster.seed-nodes` pointed at itself via CLUSTER_IP/CLUSTER_PORT) -- Kafka/Redis
are only required for the `kafka`/`clickhouse` report strategies, which we do not enable.
We use the `parquet` report strategy only, matching what `docker-compose-simple-validation.yml`
scaffolding hints at but goes further by NOT requiring the Kafka/Zookeeper/Redis containers
that compose file spins up for its live-viz mirroring feature -- see README.

Known quirk (documented, not "fixed" here — it's an existing platform behavior, not
something this task's scope covers): the HTC JVM process does not exit on its own after
`global-time-manager: Simulation stopped` is logged (observed consistently across every
run: coordinated cluster shutdown singleton-actor teardown completes, but the JVM process
lingers indefinitely afterward). This runner watches the log for that line, gives a fixed
grace period for the Parquet writer's `postStop()` flush (which happens a few hundred ms
*after* that log line, as part of ClusterSingletonManager teardown), then kills the process.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

STOP_MARKER = "global-time-manager: Simulation stopped"
FLUSH_GRACE_S = 5.0
POLL_INTERVAL_S = 1.0


class HtcRunResult:
    def __init__(self, parquet_dir: Path, log_path: Path, timed_out: bool):
        self.parquet_dir = parquet_dir
        self.log_path = log_path
        self.timed_out = timed_out


def run_htc(
    simulation_json: Path,
    city_map_json: Path,
    jar_path: Path,
    work_dir: Path,
    parquet_dir: Path,
    micro_update_report_every: int = 1,
    timeout_s: float = 180.0,
    cluster_port: int = 1600,
) -> HtcRunResult:
    """Runs the HTC fatjar against `simulation_json` and returns once the simulation has
    logged its stop marker (or `timeout_s` elapses). Always kills the leftover JVM process
    before returning, since it does not exit on its own (see module docstring)."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    parquet_dir = Path(parquet_dir)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "htc_run.log"

    run_conf = work_dir / "run.conf"
    run_conf.write_text(
        "include \"application\"\n\n"
        "htc.report-manager.enabled-strategies = [\"parquet\"]\n"
        f'htc.report-manager.parquet.directory = "{parquet_dir}"\n'
        "htc.report-manager.parquet.number-of-instances = 1\n"
        "htc.report-manager.parquet.number-of-instances-per-node = 1\n"
    )

    env = {
        "CLUSTER_IP": "127.0.0.1",
        "CLUSTER_PORT": str(cluster_port),
        "SEED_PORT_1600_TCP_ADDR": "127.0.0.1",
        "HTC_SIMULATION_CONFIG_FILE": str(simulation_json),
        "HTC_MOBILITY_CITY_MAP_FILE": str(city_map_json),
        "HTC_MICRO_UPDATE_REPORT_EVERY": str(micro_update_report_every),
        "HTC_LOAD_BALANCE_ENABLED": "false",
    }
    import os

    full_env = {**os.environ, **env}

    cmd = [
        "java",
        f"-Dconfig.file={run_conf}",
        "--add-opens=java.base/java.lang=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.misc=ALL-UNNAMED",
        "-jar",
        str(jar_path),
    ]

    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, env=full_env)

    timed_out = False
    start = time.monotonic()
    stopped_at: float | None = None
    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout_s:
                timed_out = True
                break

            if stopped_at is not None and (time.monotonic() - stopped_at) > FLUSH_GRACE_S:
                break

            if stopped_at is None and log_path.exists():
                text = log_path.read_text(errors="ignore")
                if STOP_MARKER in text:
                    stopped_at = time.monotonic()

            if proc.poll() is not None and stopped_at is None:
                # Process died before logging the stop marker -- surface as a timeout so
                # the caller inspects the log rather than silently trusting empty output.
                timed_out = True
                break

            time.sleep(POLL_INTERVAL_S)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    return HtcRunResult(parquet_dir=parquet_dir, log_path=log_path, timed_out=timed_out)
