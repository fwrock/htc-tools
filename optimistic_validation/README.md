# HTC Time Warp vs conservative-mode validation

Validates HTC's Time Warp (`timeManagerType: "time-warp"`, optimistic synchronization with
rollback/anti-messages) against the conservative `discrete-event` mode by running the *same*
scenario twice and comparing Parquet report output row-for-row. Conservative mode is the one
already validated against real SUMO ground truth (`tools/sumo_validation/`) and against
InterSCSimulator historically; this tool does not re-validate physical accuracy -- it checks that
switching a scenario's synchronization strategy does not change *what happened in the
simulation*.

## Layout

```
tools/optimistic_validation/
  scenario/        resolves which scenario dir to use (reuse an existing one, or generate fresh)
  variants/        writes two simulation.json variants that differ only in timeManagerType
  parsers/         HTC Parquet reader -> common comparison schema (all event_types, not filtered)
  metrics/         exact/tolerance parity checks + known-gap detection
  reports/         CSV + human-readable text report writer
  tests/           pytest suite for every module above
  run_comparison.py   orchestrates resolve -> variants -> run -> parse -> compare -> report
  output/          generated scenario files + run artifacts + parity.csv + report.txt
```

This tool imports `run_htc` from `tools/sumo_validation/runners/htc_runner.py` directly (via
`sys.path`, appended so this tool's own same-named `parsers`/`metrics` packages take priority
over `sumo_validation`'s) rather than forking it -- `sumo_validation/` is not modified by this
tool.

## Methodology

1. **Scenario**: reuses `hyperbolic-time-chamber/scripts/scenario/generate_tiny_validation_scenario.py`'s
   output unchanged -- a small all-entities scenario (`Node`, `Link`, `Person`, `Car`, `BusStop`,
   `BusStation`, `SubwayStation`, `RailLink`), so the comparison exercises every actor type Time
   Warp needs to get right (`Movable` subclasses -- `Car`/`Bus`/`Subway` -- and their rollback
   path in particular), not just a single-vehicle happy path.
2. **Variants**: `variants/simulation_json.py::write_variant` loads the base `simulation.json` and
   overrides exactly the `timeManagerType` field (`"discrete-event"` vs `"time-warp"`,
   `core/enumeration/TimeManagerTypeEnum.scala`'s literal values) -- everything else in the
   scenario is byte-for-byte identical between the two runs.
3. **Runs**: each variant is run sequentially (not in parallel -- avoids CPU contention skewing
   Time Warp's own timing, which involves genuinely extra rollback/replay compute even on a tiny
   scenario) via the same `run_htc` subprocess wrapper `sumo_validation` uses -- no docker-compose,
   no Kafka/Redis, `parquet` report strategy only.
4. **Parse**: `parsers/htc_events.py::parse_htc_events` reads every row (all `event_type` values,
   not filtered to kinematic ones), decoding the `data` JSON column and deriving `entity_type`
   from the `htcaid:<type>;<name>` id convention.
5. **Compare**: `metrics/parity.py::compare_runs` runs a two-tier comparison (see below).
6. **Known-gap reclassification**: `metrics/known_gaps.py::detect_movable_rollback_gap` scans the
   time-warp run's log for the two log-line signatures of the known, already-diagnosed `Movable`
   route-state-after-rollback gap and reclassifies any failing finding for an entity that hits
   both the log signature and an incomplete trip.
7. **Report**: `parity.csv` (one row per finding) and `report.txt` (human-readable summary +
   full findings table) are written per scenario run, alongside each leg's own `htc/` (log,
   `run.conf`, `simulation.json`) and `htc_parquet/` output, mirroring `sumo_validation/output/`'s
   layout.

## Comparison tiers

- **EXACT** (zero tolerance, any mismatch is a `FAIL`): completed-trip count per `entity_type`
  (`journey_completed` for vehicles, `person_schedule_complete` for `Person`); entity-creation
  count per `entity_type` (`bus_created`, `subway_created`, and `journey_started` as the
  activation proxy for vehicle types that exist from scenario load rather than being spawned at
  runtime); per-entity distinct-link-visited SET (from `enter_link`/`leave_link`/
  `enter_micro_link`/`leave_micro_link`'s `link_id` field, compared order-insensitively); final
  entity status per entity (last row by tick, comparing a `status`/`movableStatus` field when
  present).
- **TOLERANCE**: per-entity travel time (last tick minus first tick of a completed trip) within
  `--travel-time-tolerance-ticks` (default 2); per-entity link-*sequence* Levenshtein edit
  distance (order-sensitive, only computed once the exact-tier link-SET check already passed) --
  0 is a `PASS`, 1 is a `FLAG` (Time Warp's rollback/replay is expected to occasionally reorder
  adjacent same-tick events without that being a real divergence), more is a `FAIL`; total row
  count per `event_type` scenario-wide within `--row-count-tolerance-pct` (default 2.0).

## Known, deliberately out-of-scope gap

Time Warp has one confirmed, already-diagnosed correctness gap that this tool does **not** try to
fix: after a rollback, `Movable` subclasses (`Bus`/`Subway`/`Car`/`Bicycle`/`Motorcycle`) can lose
their route-position state (`movableCurrentPath`/`bestRoute`) because
`RollbackHistoryHandler`'s checkpoint-plus-replay doesn't fully reproduce every mutation these
actors make along the way. See
`hyperbolic-time-chamber/docs/KNOWN_GAPS.md`, "Time Warp: `Movable` Route State
(`movableCurrentPath`/`bestRoute`) Not Correctly Reconstructed After a Rollback", for the full
mechanism and log evidence. `metrics/known_gaps.py` detects entities hit by this gap (log-line
signature *and* an incomplete trip that conservative mode did complete) and reclassifies their
findings to `KNOWN_GAP` instead of `FAIL`, so the report's "other/unexplained failures" line
reflects genuinely new regressions, not this already-tracked issue. Pass `--fail-on-known-gaps` to
make the exit code nonzero even when known-gap findings are the only failures (e.g. for a CI gate
that should only go green once the gap itself is fixed).

## Running it

Prerequisites: same as `tools/sumo_validation/` -- `hyperbolic-time-chamber` assembled
(`cd hyperbolic-time-chamber && sbt assembly`) and the `tools/sumo_validation_venv/` Python venv
(`pandas`, `pyarrow`, `pytest`).

```bash
cd tools/optimistic_validation
../sumo_validation_venv/bin/python run_comparison.py \
    --jar ../../hyperbolic-time-chamber/target/scala-3.3.5/hyperbolic-time-chamber-1.24.3.jar \
    --generate \
    --out-dir ./output \
    --conservative-timeout 180 --time-warp-timeout 240 \
    --travel-time-tolerance-ticks 2 --row-count-tolerance-pct 2.0
```

Or point at an already-generated scenario directory with `--scenario-dir <path>` instead of
`--generate`. Writes `output/<scenario_id>/parity.csv` and `output/<scenario_id>/report.txt`,
plus each leg's `conservative/htc/`, `conservative/htc_parquet/`, `time_warp/htc/`,
`time_warp/htc_parquet/`. Exit code is nonzero when there are failures not attributable to the
known Movable gap (or, with `--fail-on-known-gaps`, when there are any failures at all).

`--dry-run` resolves the scenario and writes both `simulation.json` variants without invoking
`java` -- useful for a quick CLI/wiring smoke-test.

### Tests

```bash
cd tools/optimistic_validation
../sumo_validation_venv/bin/pytest -q
```

24 tests: `write_variant` round-trip and byte-for-byte-except-one-field checks, parser tests
against a synthetic Parquet fixture (multiple event types, `htcaid:` prefix derivation, an
unmatched id falling back to `entity_type="unknown"`, null `data` handling), parity checks against
synthetic DataFrames (identical-run all-pass, an exact-tier mismatch, tolerance-tier pass/fail on
both sides of the boundary, the link-sequence single-edit `FLAG` case, row-count tolerance
pass/fail, and hand-computed Levenshtein distances), and known-gap detection (log signature alone
is not enough, incomplete trip alone is not enough, both together fires, a missing log file is
handled gracefully).

## Design deviations from the original brief

- **Stop-marker verification**: confirmed by reading source, not by a live run --
  `OptimisticGlobalTimeManager.stopSimulation()` calls `super.stopSimulation()`
  (`core/actor/manager/time/TimeManagerBase.scala`), which logs `logInfo("Simulation stopped")`,
  formatted by `BaseActor.logInfo` as `"$entityId: Simulation stopped"` with
  `entityId = "global-time-manager"` (`core/util/ManagerConstantsUtil.GLOBAL_TIME_MANAGER_ACTOR_NAME`).
  So `htc_runner.py`'s `STOP_MARKER = "global-time-manager: Simulation stopped"` fires identically
  under both time-manager types -- no patch needed, no discrepancy found.
- **JSON field name**: confirmed as `timeManagerType` (not just "likely") by reading
  `core/entity/configuration/Simulation.scala`'s field declaration directly, plus
  `core/actor/manager/SimulationManager.scala:388`'s `configuration.timeManagerType ==
  TimeManagerTypeEnum.TIME_WARP` branch -- as of this branch (`feat/time-warp`), Time Warp is
  actually wired into the config-driven factory path, not just present as an enum value with
  "nothing sets it yet" (that caveat in `TimeManagerTypeEnum.scala`'s own docstring is now stale
  relative to `Simulation.scala`/`SimulationManager.scala`).
- **Known-gap log signature wording**: the brief's assumed phrase was "anti-message for
  unknown/**already-pruned**" messageId; the actual, only log line in
  `SimulationBaseActor.scala:984` is `"anti-message for unknown messageId=..."` -- no
  "already-pruned" variant exists anywhere in the source. `metrics/known_gaps.py` matches the real
  line.
- **Completion/creation event-type literals**: verified directly against `report(...)` call sites
  rather than assumed -- `journey_completed` (not e.g. `car_journey_completed`) is the literal
  label for `Car`/`Bus`/`Motorcycle`/`Bicycle`; `person_schedule_complete` (not
  `journey_completed`) is `Person`'s daily-schedule completion label, distinct from its own
  per-trip `person_trip_completed` (not used as the completion signal here, since a `Person` can
  complete several trips before their full schedule finishes).
