# Native São Paulo MATSim export → HTC

Use `generate_matsim_input.py` for the native EQASim/MATSim export:

```text
input/
  sao_paulo_config.xml
  sao_paulo_network.xml.gz
  sao_paulo_population.xml.gz
  sao_paulo_transit_schedule.xml.gz
  sao_paulo_transit_vehicles.xml.gz
  meta.json
```

Uncompressed `.xml` inputs also work. The network, selected population plans,
bus services and metro/rail/tram schedules are converted directly. No OSM road
extract is required. The CSV and GeoPackage tables, facilities, households and
MATSim JAR are not needed for this conversion. The generator does not execute
the JAR or import household/facility behavior into HTC.

This entry point and Toulouse's `generate_hybrid_input.py` share
[`../scenario_common/matsim.py`](../scenario_common/matsim.py). The existing
São Paulo `generate_hybrid_input.py` remains the OSM + EQASim/GTFS alternative.

## Generate a scenario

From `tools/sao-paulo`, after installing `requirements.txt`:

```bash
python3 generate_matsim_input.py \
  --input-dir /data/sao-paulo --output output --scenario sp_matsim_1pct \
  --sample 0.01 --transit --rail --unsupported-mode replan \
  --mc-modes bus subway walk
```

The CRS is read from `global.coordinateSystem` in `sao_paulo_config.xml`.
The network, population and schedule must share a projected CRS in metres.
An explicit `--population-crs` overrides the config. If the config says
`Atlantis`, or is absent, provide the CRS established from your source dataset:

```bash
python3 generate_matsim_input.py \
  --input-dir /data/sao-paulo --scenario sp_matsim_1pct \
  --population-crs "$SOURCE_CRS" --sample 0.01
```

Set `SOURCE_CRS` to the verified CRS; this script does not assume a São Paulo EPSG
code. A mistaken CRS changes snapping, distances and output geography. Metadata
records the CRS and whether it came from the option, config or existing scenario.
Use `--matsim-config FILE` to override config discovery.

`--network-file`, `--population-file`, `--transit-schedule-file` and
`--transit-vehicles-file` override filename discovery. These paths are relative
to `--input-dir` unless absolute.

The output contains split actor files, city and transit indexes, `simulation.json`,
`effective_config.json`, source metadata and `GENERATION_REPORT.md`. Use
`--docker-base /scenarios` when that is the parent directory visible inside the
engine container. `--output-format sqlite` uses the existing SQLite converter
and requires `ijson`.

## Build or replace components

```bash
# Roads only: requires network and a known source CRS.
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_parts --parts network

# Add people; no source network is required on extension.
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_parts --extend --parts population \
  --sample 0.01 --unsupported-mode walk

# Add buses, metro, rail and tram from the MATSim schedule.
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_parts --extend --parts transit rail

# Regenerate only people and their private vehicles.
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_parts --extend --parts population --replace-parts \
  --sample 0.02 --unsupported-mode replan --mc-modes bus subway walk
```

`--parts` overrides the corresponding transit/rail/signals switches; a fresh
scenario needs `network`. An extension requires the generator's original
manifest, effective config, metadata and ID registry. It preserves global road
settings, CRS, seed, clock and output format, updates selected parts and merges
the bus/rail indexes. Existing selected parts require `--replace-parts`.
Extensions stage a full scenario copy; allow disk space for that copy and
temporary JSON when using SQLite. Use an idle scenario without concurrent writers.

For signals, supply a separate local OSM XML/XML.gz extract:

```bash
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_parts --extend --parts signals \
  --signal-osm-file signals.osm.gz --signal-max-snap-distance 50
```

Signal locations are projected into the declared MATSim CRS and matched to road
nodes. Timing and phases are synthetic, with the same documented MICRO/MESO
approach approximation as the Toulouse generator.

## Optional GTFS buses

Choose one source for buses. `--bus-source matsim` is the default. To use unpacked
GTFS tables instead, while retaining the MATSim road graph and MATSim rail:

```bash
python3 generate_matsim_input.py --input-dir /data/sao-paulo \
  --scenario sp_gtfs --sample 0.01 --bus-source gtfs \
  --transit-providers sptrans emtu --service-date 2026-11-04 --rail
```

Provider directories are relative to `--input-dir` unless absolute. Supply the
agency-local date covered by those feed snapshots. The existing São Paulo GTFS
converter handles service calendars, exceptions, frequency windows, stop snapping
and closed bus road routes; its UTM snapping coordinates are adapted to the
declared MATSim CRS. MATSim bus services are not emitted in GTFS mode. To use
GTFS without a MATSim transit schedule or vehicles file, also pass `--no-rail`,
or select `--parts network population transit` explicitly. On an existing scenario,
switch bus sources using `--extend --parts transit --replace-parts`.

Transit routing uses inferred headways rather than exact next-departure times.
The existing GTFS backend uses a 600-second fallback when a pattern has only one
departure and no frequency entry. That estimated wait can make a trip infeasible
in a short simulation horizon, even if a bus actor departs within the horizon.

## Sampling and modeling scope

Default sampling is a generated-person target in file order (`prefix`), which
may introduce ordering bias. Use `--sampling-strategy random` for seeded
full-file Bernoulli sampling. `--population-size N` avoids the initial counting
pass when N is known. Maps are reused in `OUTPUT/.sao_paulo-map-cache` when source
and map options match; `--no-reuse-map` forces regeneration.

Unknown population modes follow `--unsupported-mode`: `error`, `walk`,
`skip-person` or `replan` (the CLI default). Replanning does not preserve exact
MATSim PT itineraries. Bus and rail are enabled by default; use `--no-transit
--no-rail --unsupported-mode walk` for roads and people only.

Supported schedule modes are `bus`, `rail`, `subway` and `tram`; other modes are
tallied without conversion. Per-departure bus capacity/length comes from the
referenced MATSim vehicle type, including both nested and attribute capacity
formats. Missing references use the existing defaults and count bus fallbacks.
Rail selects the stop pattern with most departures per line, synthesizes its
reverse direction, and uses the most common referenced vehicle capacity in that
pattern (or the mode/config fallback). Rail segments use straight-line station
separation, not track geometry. Buses circulate on a closed road route; finite
scheduled trip retirement and stop-time adherence are not implemented. Fleets are
not scaled with the population sample. These assumptions appear in the report.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 tests/run_matsim_smoke.py --output-root /tmp/sp-native-check
python3 tests/run_matsim_smoke.py --output-root /tmp/sp-gtfs-check \
  --bus-source gtfs --base-port 32850
```

The smoke runner requires an existing HTC assembly, configurable with `--jar`
and `--htc-root`. It retains the synthetic native inputs, generation/runtime
commands, JAR checksum, reports and expected/observed outcomes. It checks person
schedules and completed trips, car journeys, bus activations and boarding, rail
activity and absence of aborted trips or engine errors. This verifies the bounded
fixture, not full-city scientific calibration or full-export compatibility.
