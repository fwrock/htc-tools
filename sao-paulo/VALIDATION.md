# Converter validation — 2026-10-27

Validation concerns Python scenario generation and a bounded native HTC run. It does not establish city-scale accuracy, traffic calibration or performance.

## Automated checks

All 15 focused tests passed. Python compilation checks and both example configuration parsers passed.

`python3 -m unittest discover -s tests -v` covers configuration-file precedence and unknown keys, EQASim GeoPackage CRS conversion, selected MATSim plans and gzip input, stage-activity collapse/durations, explicit unresolved-mode rejection, invalid clocks/snap limits, expired GTFS service, frequency expansion, opposing route patterns, bus-route continuity, owned parked cars, native manifests, reverse-only OSM roads, network reuse cleanup, local rail extraction, directional rail terminal continuity, and safe GTFS ZIP installation.

## Actual simulator smoke

Retained artifacts: `/tmp/htc-sao-paulo-smoke-final-20261027/`. `command.json`, `expected.json`, `outcomes.json` and `runtime/result.json` capture execution and checks. These temporary files should be copied to durable storage for long-term evidence.

Executable: `target/scala-3.3.5/hyperbolic-time-chamber-3.2.0.jar` from the sibling HTC checkout. SHA-256: `bfab98fde9dce619f753304876fc2a367655bb84e5826784714fc72c36a1ae1f`.

Configuration: one JVM, one LocalTimeManager, conservative `discrete-event`, MESO links, seed 42, one-second ticks, inclusive horizon 240. The fixture contains three road nodes/four directed links, two bus stops, two rail stations/two rail links, two people, one owned car, four bus actors and two train definitions. The runtime helper stops its managed JVM after observing completed execution and report flush; its forced process teardown is not the outcome acceptance criterion.

| Check | Expected | Observed |
|---|---:|---:|
| Completed person schedules | 2 | 2 |
| Completed person trips | 3 | 3 |
| Completed car journeys | 2 | 2 |
| Bus activation ticks | 10, 30, 50, 90 | 10, 30, 50, 90 |
| Train activations | 2 | 2 |
| Bus passengers boarded | 1 | 1 |
| Aborted person trips | 0 | 0 |
| Engine errors | 0 | 0 |

Subway arrival reports were present. Buses and trains circulate until the horizon and their final reports are horizon-censored, not completed GTFS trips. The smoke script validates these different outcomes separately. Both the engine result and model-outcome check passed.

Reproduce with a fresh directory:

```bash
python3 tests/run_smoke.py --output-root /tmp/htc-sp-smoke-new
python3 tests/run_smoke.py --output-root /tmp/htc-sp-smoke-new --validate-only
```

The current fixture tests bus boarding and car completion; it does not test subway passenger boarding, MICRO dynamics, optimistic rollback or a real city-wide population.

## Retrieved/inspected São Paulo inputs

The fresh SPTrans feed is saved at `input/fetched-2026-10-27/sptrans/`. Downloaded archive SHA-256: `5117749e93196e8dc9fccb603f72e2dc078808168d64fe3463c9d70a925f01e9`. Its calendar includes the requested 2026-10-27 date and ends on 2027-05-01. Source and per-table hashes are in `source.json`.

The pre-existing `input/sptrans/` calendar ends on 2026-10-01, and `input/emtu/` ends in 2018. Neither was overwritten. No fresh EMTU feed was acquired; obtain one from the operator if needed.

Overpass requests failed with HTTP 502/timeout. The rail extract was instead generated from the existing `input/sao_paulo.osm.gz`, preserving that snapshot's age. Output: `input/fetched-2026-10-27/rail.osm`, SHA-256 `e3322bcb16c93ac486bd606702c0af10c382ac0c955fb995b4bd6224e4e124bc`. Extraction/audit found 1,919 rail ways and 33 route relations: 10 subway, 2 monorail and 21 train variants. The railway parser recovered 381 ordered station references from complete relations. Two train-line-7 relations had missing stop coordinates and were excluded. `output/rail_audit_20261027.json` records relation IDs and station sequences; it is an infrastructure audit, not a calibrated service model.

The local GeoPackage CRS was inspected through its spatial reference metadata and identifies SAD69 / UTM zone 23S. A complete real São Paulo population conversion/run was not attempted. The supplied MATSim example uses EPSG:29193 only as an editable example; verify the source run's actual CRS before using it.
