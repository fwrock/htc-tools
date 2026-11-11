# São Paulo synthetic population → HTC

Convert an EQASim synthetic population or a selected MATSim plans file into HTC's native hybrid-model actor JSON. The converter combines population activities, an OSM road graph, date-filtered GTFS bus services, and optional subway/train infrastructure from OSM.

For an export containing `sao_paulo_network.xml.gz`, `sao_paulo_population.xml.gz`
and the MATSim transit schedule/vehicles, use **[`generate_matsim_input.py`](generate_matsim_input.py)**.
It shares the MATSim converter with Toulouse and supports optional GTFS buses.
See **[native MATSim usage and examples](MATSIM.md)** for CRS discovery, parts,
extensions and validation. The commands below describe the OSM-based generator.

For an AI agent extending this code, start with [AGENT_IMPLEMENTATION_GUIDE.md](AGENT_IMPLEMENTATION_GUIDE.md). For users, the commands below are sufficient. This is an offline converter; HTC does not read MATSim XML directly.

## Generate selected parts and extend a scenario

Use `--parts network population transit rail signals` to select components. A fresh
scenario must include `network`. `population` includes people and their private vehicles;
`transit` means GTFS buses; `rail` means OSM metro/trains. Parts override the corresponding
`--transit`, `--rail` and `--signals` switches.

```bash
# From tools/sao-paulo: create just the roads (no population inputs required).
python3 generate_hybrid_input.py --input-dir input --scenario sp_parts --parts network
# Add MATSim people; existing roads need no source OSM file.
python3 generate_hybrid_input.py --input-dir input --scenario sp_parts \
  --extend --parts population --input-format matsim --population-crs EPSG:32723
# Add date-filtered buses, preserving existing people and rail.
python3 generate_hybrid_input.py --input-dir input --scenario sp_parts \
  --extend --parts transit --service-date 2026-10-27 --transit-providers sptrans emtu
# Replace only those buses and stops, keeping all other components.
python3 generate_hybrid_input.py --input-dir input --scenario sp_parts \
  --extend --parts transit --replace-parts --service-date 2026-10-27
```

`--extend` requires explicit `--parts` and the original generator's manifest,
effective config, metadata and `data/id_registry.json`. It adds missing components;
`--replace-parts` explicitly allows regenerating existing selected components.
Unselected actor files and custom manifest settings are preserved; bus/rail transit
indexes are merged. Road nodes are rewired when signals change. The network cannot be
replaced in an extension. Use `--extend` instead of combining parts with `--skip-network`.

Both JSON and SQLite outputs are supported. The existing format, road settings, seed,
CRS, simulation horizon/origin and manifest path prefix are inherited. Input paths and
selected-part options come from the new command. Extensions build a sibling copy and
publish after successful conversion; allow disk space for that copy and temporary JSON
when using SQLite. Use an idle scenario without simultaneous writers. Ordinary runs
still require empty output directories.

## Install and check

Run commands from this directory. Python 3.10+ is required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 generate_hybrid_input.py --help
```

SQLite output additionally requires `python3 -m pip install ijson`. The converter reuses the sibling `../scenario-db-converter/convert.py` and its schema.

## Get transit and map data

SPTrans publishes static GTFS through its [developer portal](https://www.sptrans.com.br/desenvolvedores/). The downloader accepts the public endpoint or an operator-provided ZIP. EMTU's [GTFS portal](https://www.emtu.sp.gov.br/emtu/dados-abertos/dados-abertos-principal/gtfs.fss) may require obtaining the feed manually. Do not assume an old local archive still represents the requested date.

```bash
# A separate snapshot directory preserves previously downloaded inputs.
python3 fetch_inputs.py --input-dir input/fetched-2026-10-27 --sptrans

# An operator-provided feed, downloaded through its portal:
python3 fetch_inputs.py --input-dir input/fetched-2026-10-27 \
  --gtfs emtu=/absolute/path/to/emtu.zip

# OSM subway, train, light-rail and monorail relations, including recursive members:
python3 fetch_inputs.py --input-dir input/fetched-2026-10-27 --rail \
  --bbox=-24.1,-47.1,-23.2,-46.1

# Reliable offline alternative using the existing local OSM snapshot:
python3 fetch_inputs.py --input-dir input/fetched-2026-10-27 \
  --rail-from-osm input/sao_paulo.osm.gz
```

`fetch_inputs.py` caches existing targets. Use `--refresh` explicitly to replace a snapshot. GTFS tables are staged and checked before installation. Each feed has a `source.json` recording retrieval time and SHA-256 hashes. Rail provenance is in `rail.source.json`. Local rail extraction uses the entire local extract and ignores `--bbox`; it does not make the OSM snapshot newer.

Overpass endpoints can time out. A failed request does not replace an existing extract. Set `--overpass-url` to another appropriate endpoint, retry later, or use the local alternative. The rail query selects `type=route` relations with `route=subway|train|light_rail|monorail` and their members; see the [OpenStreetMap Overpass documentation](https://wiki.openstreetmap.org/wiki/Overpass_API).

Road input must be OSM XML or XML.gz, normally `input/sao_paulo.osm.gz`. Supply another file with `--osm-file`. A PBF cannot be parsed as XML: convert it first, for example with `osmium cat sao_paulo.osm.pbf -o sao_paulo.osm`. Preserve OSM attribution and check the source's licensing before redistributing scenarios. Downloading roads is separate from the small rail query.

## Traffic lights from OSM

Add `--signals --signal-cycle 120` to a generation command. Locations come from
`highway=traffic_signals` nodes in the road OSM XML/XML.gz (`--osm-file`). Each
node with an incoming road link gets a `TrafficSignal` actor, including stop
lines on one-way roads. Phases use incoming link IDs and synthetic timing;
single-approach lights retain a red interval. OSM does not supply these phase plans.
Generated node maps connect the phases to MICRO approaches and MESO outgoing gates.
Because turn movements are unavailable, each MESO outgoing gate uses the phase of
the straightest incoming approach; this is a synthetic turning approximation.

`data/osm_signal_nodes.json` preserves the extraction even if `--signals` is off,
so a subsequent `--skip-network --signals` can enable lights. Older output without
this extraction or signal actors must be regenerated once. Signal actors are
included in both JSON and SQLite manifests.

## Population inputs and coordinates

| Input mode | Required files | Coordinates |
|---|---|---|
| `eqasim` | `persons.csv`, `trips.csv`, `activities.gpkg` | Read activities CRS from GeoPackage metadata; override with `--population-crs` if necessary |
| `matsim` | `output_plans.xml.gz` or `output_plans.xml` | **Supply `--population-crs`** from MATSim's `global.coordinateSystem` |

EQASim CSVs use semicolons. The [São Paulo population pipeline](https://github.com/eqasim-org/eqasim-sao-paulo) produces the demographic, activity and trip exports; see its [generation instructions](https://github.com/matsim-scenarios/matsim-sao-paulo/blob/master/docs/howto.md). `activities.csv` has no coordinates and does not replace the GeoPackage.

The local `activities.gpkg` inspected on 2026-10-27 declares **SAD69 / UTM zone 23S**, using a custom SRS record. The old script's WGS84 assumption was incorrect for that input. All activities are now transformed to the road graph's EPSG:32723 working coordinates before snapping. Do not guess a MATSim CRS from its city or filename.

The maximum snap distance is 1,000 m by default. A person with an invalid, outside-bbox or unsnappable activity is excluded as a whole, with counters in the generation report. Trips are never stitched across a discarded activity. Stops outside the bbox or snap limit are excluded; a bus pattern with any missing stop is excluded in full.

## Configurable generation

Use [scenario.example.json](scenario.example.json) for EQASim or [scenario.matsim.example.json](scenario.matsim.example.json) for MATSim. These are editable starting points, not validated city-scale scenarios.

```bash
python3 generate_hybrid_input.py --config scenario.example.json

# Command-line values override the JSON file.
python3 generate_hybrid_input.py --config scenario.example.json \
  --scenario sp_smaller --sample 0.0001

# MATSim, without a config file; replace the CRS with the run's actual CRS.
python3 generate_hybrid_input.py --scenario sp_matsim --input-format matsim \
  --matsim-plans /absolute/path/output_plans.xml.gz \
  --population-crs EPSG:29193 --sample 0.001 \
  --transit --transit-providers fetched-2026-10-27/sptrans \
  --service-date 2026-10-27 --unsupported-mode replan \
  --mc-strategy raptor --mc-modes bus
```

JSON keys are CLI option names without `--`, using hyphens or underscores. Unknown keys and invalid choices fail. `input-dir`, `output` and `rail-lines-config` in a config file are resolved relative to that file. OSM/plans paths are relative to `input-dir` unless absolute. CLI relative paths are relative to the current directory. Boolean CLI flags support both forms, for example `--rail` / `--no-rail`.

| Option | Default | Meaning |
|---|---|---|
| `scenario` | required | A single output directory name |
| `input-dir`, `output` | `input`, `output` | Input directory and output parent |
| `input-format` | `eqasim` | `eqasim` or `matsim` |
| `population-crs` | GeoPackage metadata | Required explicitly for MATSim |
| `sample`, `seed` | `0.01`, `42` | Additional population sample and deterministic RNG seed |
| `duration` | `86400` | Inclusive horizon in one-second runtime ticks |
| `start-real-time` | service-date midnight, otherwise 2025-01-01 | Local scenario origin, ISO datetime |
| `bbox` | absent | `south,west,north,east`; pass negative CLI bounds with `--bbox=...` |
| `max-snap-distance` | `1000` | Maximum population/stop snap distance in metres |
| `unsupported-mode` | `error` | `error`, `skip-person`, `walk`, or `replan` |
| `transit`, `rail`, `signals` | false | Enable each infrastructure/service source |
| `service-date` | required with transit | `YYYY-MM-DD`, interpreted in the feed's agency-local service day |
| `transit-providers` | `sptrans emtu` | Feed directories under `input-dir`; specify only the feeds you have |
| `max-buses-per-route` | `10` | Bus cap across patterns of each provider's source route; `0` removes the cap |
| `rail-osm-file` | road OSM | Separate railway XML/XML.gz extract |
| `rail-types` | all four types | `subway rail light_rail monorail`; `rail` selects OSM `route=train` |
| `rail-lines-config` | absent | JSON overrides indexed by `subway_1`, `train_7`, etc. |
| `max-trains-per-line` | `6` | Dispatch cap per direction; `0` fills the horizon |
| `train-capacity`, `train-interval` | `1200`, `300` | Scenario assumptions: passengers and seconds |
| `micro-ratio`, `micro-strategy` | `0`, `highway` | Optional MICRO assignment; strategy is `highway`, `arterial`, or `random` |
| `signal-cycle` | `120` | Synthetic OSM signal-cycle duration in seconds |
| `items-per-file` | `50000` | Maximum actors per split JSON file |
| `output-format` | `json` | `json` or `sqlite` |
| `docker-base` | `/app/hyperbolic-time-chamber/simulations/input` | Runtime-visible **parent** of scenario directories |
| `skip-network` | false | Reuse a matching generated JSON road network |

Additional driver ranges and mode-choice weights are exposed by `--help`. Dynamic choice uses `PendingDecision`; it does not use the obsolete `dailySchedule` / `arrivalLogistics.mode="auto"` schema.

### Preserve or change source modes explicitly

Car, bike/bicycle, motorcycle and walking map to native HTC legs. Unresolved `pt`, taxi, car-passenger and custom modes fail by default. Choose a policy deliberately:

- `error`: stop instead of changing trip meaning.
- `skip-person`: exclude the person's complete plan.
- `walk`: explicitly substitute walking and count replacements.
- `replan`: resolve only unresolved trips at runtime through `PendingDecision`; this changes the MATSim/EQASim itinerary. Use `raptor` and `--mc-modes bus subway` for transit with transfer/access/egress support, enabling the matching data sources.

`--dynamic-mode-choice` replans every trip. Private candidate modes receive owned parked vehicle actors. The `nearest-stop-utility` engine filters allowed modes after choosing its candidate, so a restricted mode set can abort a trip; prefer RAPTOR for transit-only decisions. RAPTOR needs the generated transit route index and does not choose standalone private trips.

MATSim import reads exactly one selected plan, supports XML.gz, keeps the population parse bounded to one person's XML subtree, and collapses `* interaction` stage activities into main-activity trips. Access/egress walks surrounding PT become one unresolved PT trip. Main activities require coordinates; link/facility-only locations and trips with mixed non-walking main modes fail. `end_time` takes priority over duration; `dur` / `max_dur` becomes native `Duration` when no absolute departure is available. Missing final activity ends use the configured horizon. Counters disclose these decisions.

### Transit assumptions that affect scientific use

GTFS filtering applies `calendar.txt` and `calendar_dates.txt`, separates ordered stop patterns/directions, restricts bus route types, expands each frequency window and preserves times above 24:00:00. Frequency `exact_times=0` becomes a deterministic regular service approximation. These rules follow the [GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/).

**HTC buses circulate until the horizon.** The converter uses GTFS departures as actor activation times, computes a directed road path through the pattern's ordered stops, and adds a road return path to prevent a discontinuity when HTC repeats the route. It does not reproduce scheduled intermediate arrivals, layovers, trip-end retirement, `block_id` fleet reuse or GTFS shape geometry. A cap removes later services deterministically and is not a capacity scaling model. Repeated stop identities in a GTFS pattern cannot be represented by HTC's stop map and are counted and excluded. Unroutable bus patterns are also counted and excluded. No BusStation duplicates these buses.

OSM provides rail topology and ordered stops, **not an operating timetable**. Headway, capacity, velocity, dwell, reverse service and fleet size are assumptions/overrides. Station distance remains projected straight-line separation with a 100 m floor. Rail routes include both directions. Distinct terminal stop positions with matching names are joined by explicitly counted synthetic links when within 250 m; other directional seams fail. Override `max_terminal_gap_m` only after reviewing the station correspondence. These links approximate terminal circulation, not surveyed tracks. The current garage starts the first train after one interval; the default six-train cap does not provide all-day service. Set `max-trains-per-line: 0` for horizon-wide dispatch definitions and review the resulting fleet size.

Audit OSM line relations before choosing variants:

```bash
python3 explore_rail.py --osm-file input/fetched-2026-10-27/rail.osm \
  --output output/rail_audit.json
```

More than two distinct variants for a line require explicit selection. Per-line configuration can include `relation_ids` as well as positive `headway_seconds`, `capacity`, `velocity_ms`, `stop_time_seconds`, `number_of_ports` and nonnegative `max_trains`. Example:

```json
{"subway_1": {"headway_seconds": 240, "capacity": 1200, "max_trains": 0}}
```

Add `relation_ids: [123, 456]` only after reviewing the actual source relation IDs. OSM station entrances/platform ways are not a general station-correspondence solution; this extractor uses route stop-node members.

## Output and running HTC

Each scenario contains `simulation.json`, `effective_config.json`, `scenario_metadata.json`, `GENERATION_REPORT.md`, and `data/`. Actors include road nodes/links, persons, owned vehicles, bus stops/buses, and optional rail stations/links and signals. `city_map.json`, `transit_map.json` and `transit_routes.json` support routing. IDs are globally unique positive integers and are persisted in `id_registry.json`. Motorcycles have their own actor source.

Fresh generation requires an empty output directory, preventing leftover actor chunks from an earlier larger population. `--skip-network` retains only the road network and regenerates population/transit/signals; seed, bbox and MICRO settings must match `network_config.json`. It requires a JSON network from this converter version. A failed reuse run removes the old manifest so it cannot silently run partially regenerated actors. A failed fresh run can leave diagnostic partial files: use a fresh scenario name after correcting the error.

`--output-format sqlite` converts actor/city-map JSON to the existing HTC database schema and updates manifest references; transit indexes remain JSON. The original actor JSON is removed only after its individual conversion succeeds. Full-population and full-network conversion still requires substantial memory/disk. Population sampling is additional to EQASim's source sampling rate; no road capacity, fleet or demand expansion factor is inferred.

For a local run, set `docker-base` to the absolute host output parent so every manifest path exists. For Docker, set it to the mounted path inside the container. Mount the same scenario data on every JVM. Then run HTC with `HTC_SIMULATION_CONFIG_FILE=/runtime/path/scenario/simulation.json` and its normal runtime configuration. See [HTC's README](../../hyperbolic-time-chamber/README.md).

The supported converter clock is **one second per tick**. Person and transit scenarios must not be changed to fractional ticks; HTC rejects unsupported fractional model combinations. Synthetic signals and MICRO assignment do not establish calibrated traffic behavior. The active MICRO path uses simplified Krauss following, not an asserted IDM/MOBIL integration.

## Validation evidence

```bash
python3 -m unittest discover -s tests -v

# Requires an existing HTC assembly JAR and a free port range.
python3 tests/run_smoke.py --output-root /tmp/htc-sp-smoke-new

# Inspect/recheck preserved reports without rerunning the simulator.
python3 tests/run_smoke.py --output-root /tmp/htc-sp-smoke-new --validate-only
```

The smoke check derives expected trips and bus activations from converted actors, then checks reports for person completion, completed car journeys, bus boarding, subway traversal, and engine completion. Read [VALIDATION.md](VALIDATION.md) for the retained 2026-10-27 result and scope. Passing a small fixture does not validate São Paulo demand, network calibration, optimistic execution, or city-scale performance.
