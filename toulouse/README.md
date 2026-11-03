# MATSim Toulouse → HTC Input Generator

Converts the native MATSim Toulouse scenario (eqasim export) into the JSON actor format the
**Hyperbolic Time Chamber (HTC)** hybrid simulator loads at startup.

`generate_hybrid_input.py` produces **the same output contract as**
[`tools/sao-paulo/generate_hybrid_input.py`](../sao-paulo/generate_hybrid_input.py): the same actor
field names, the same `PersonState.originalPlan` elements, the same explicit unresolved-mode
policy, routed `Bus` actors, `transit_map.json` / `transit_routes.json`, and the same
`simulation.json` keys. Only the *input side* differs, because Toulouse ships native MATSim XML —
not OSM + GTFS + eqasim CSV/GeoPackage — so the network, population and transit parsers are
Toulouse-specific. Traffic light locations can additionally be extracted from a local OSM
XML/XML.gz file and matched to the MATSim road network (see "Traffic lights from OSM").

## Folder layout

```
tools/toulouse/
├── generate_hybrid_input.py   the converter (all logic)
├── generate_tl_1pct_meso.sh   wrapper: full scenario with sensible defaults, env-configurable
├── README.md                  this file
├── tests/test_converter.py    regression tests on a tiny synthetic MATSim scenario
├── input/                     the 5 input files the converter reads (see below)
└── output/<scenario>/         created on the first run
tools/toulouse_raw_unused/     rest of the eqasim export + convert_pt_to_walk.py (not read; deletable)
```

## Run it

Two equivalent entry points; both write `output/<scenario>/`, which must be new or empty. They
generate the road network, the people, **and public transport** (buses via `--transit`, train /
metro / tram via `--rail`).

**1. The wrapper** (1 % of the population, buses + rail, MESO by default):

```bash
cd tools/toulouse
./generate_tl_1pct_meso.sh                                    # MESO
MICRO_RATIO=0.2 SCENARIO=tl_1pct_micro ./generate_tl_1pct_meso.sh   # 20 % of links MICRO
```

| Variable | Default | Meaning |
|---|---|---|
| `SCENARIO` | `tl_1pct_meso` | Output sub-directory name |
| `OUTPUT` | `output` | Parent output directory (point it at the simulator's input folder if you like) |
| `INPUT_DIR` | `input` | Directory with the five input files |
| `SAMPLE` | `0.01` | Fraction of the source population to generate |
| `SAMPLING_STRATEGY` | `prefix` | Stop at the generated target; `random` preserves seeded full-file sampling |
| `POPULATION_SIZE` | auto | Known number of source persons; skips the initial count pass |
| `REUSE_MAP` | `1` | Reuse unchanged network/map cache; `0` forces a rebuild |
| `MAP_CACHE_DIR` | `OUTPUT/.toulouse-map-cache` | Shared cache location, including across different output parents |
| `SEED` | `42` | Random seed |
| `MICRO_RATIO` | `0` | Fraction of links as MICRO; `0` = all MESO |
| `MICRO_STRATEGY` | `highway` | `highway` / `arterial` / `random` |
| `UNSUPPORTED_MODE` | `replan` | Policy for `pt` / `car_passenger` trips: `replan` / `walk` / `skip-person` / `error` |
| `MC_MODES` | `bus subway walk` | Modes a replanned trip may pick |
| `MAX_BUSES_PER_ROUTE` | `0` | Bus actors per route; `0` = all departures |
| `MAX_TRAINS_PER_LINE` | `0` | Trains per line and direction; `0` = all within the horizon |
| `DOCKER_BASE` | `/app/hyperbolic-time-chamber/simulations/input` | Parent of the scenario folders **as the engine sees it**, written into `simulation.json`. Use `/scenarios` for Docker Compose |
| `OUTPUT_FORMAT` | `json` | `json` or `sqlite` |

It stops with a clear message if an input file is missing or the output directory is not empty.
Both entry points convert **all** bus routes and use uncapped bus and train fleets by default.
Traffic lights are included when `SIGNAL_OSM_FILE` (wrapper) or `--signal-osm-file` (CLI) is supplied;
the MATSim inputs alone do not provide traffic-light locations.

**2. The converter directly** — what the wrapper runs:

```bash
python3 generate_hybrid_input.py \
  --scenario tl_1pct_meso --input-dir input --output output \
  --sample 0.01 --seed 42 \
  --micro-ratio 0 \
  --transit --max-transit-routes 0 --max-buses-per-route 0 \
  --rail --max-trains-per-line 0 \
  --unsupported-mode replan --mc-modes bus subway walk
```

For MICRO use `--micro-ratio 0.2 --micro-strategy highway`. Use `--no-transit --no-rail --unsupported-mode walk` for the
road network and people only. Unsupported `pt` / `car_passenger` trips are replanned by default;
use `--unsupported-mode error` to reject them instead.

The default `prefix` strategy generates `ceil(source_person_count * sample)` valid persons and
stops reading as soon as that target is reached. Skipped people do not count toward the target.
If too few valid people exist, the converter reads to EOF and reports the shortfall. `--sample 1`
always reads the full population. This changes which people are selected: taking people in file
order can introduce ordering bias. For scientific random sampling use `--sampling-strategy random`,
which preserves seeded Bernoulli selection and scans the entire file (its generated count can be
lower than the requested percentage after invalid people are skipped).

The first fractional prefix run counts source persons once, caching the result next to the XML as
`<population-file>.count.json`. Later runs reuse it when source size and modification time match.
A full conversion also writes this cache. With read-only inputs generation still works, but the
count cannot be cached. Supply `--population-size N` or `POPULATION_SIZE=N` to skip the counting
pass immediately; N must be the number of persons in that exact source file. Sampling strategy,
scanned/total/generated counts, target and early-stop status are recorded in the output metadata.
Historical runtime and fleet counts below describe the previous random/capped configuration.

### Reusing unchanged maps

Map reuse is enabled by default. The first run saves the road network actors (`nodes_*.json`,
`links_*.json`), `city_map.json`, bus routing links, and the network IDs and snapping state in
`OUTPUT/.toulouse-map-cache/`. Later generations with the same network and map settings load
these files and skip network XML parsing, coordinate conversion, connected-component discovery
and city-map construction. Population changes, sample size, scenario name, transit settings and
JSON versus SQLite output do not require rebuilding the road map. Transit and signals are still
generated for each scenario; this cache covers the road network and city map.

Cache keys include SHA-256 of the source network and generator code, source CRS, seed,
MESO/MICRO settings, split size, and whether signal approach data is needed. Changed inputs or
settings produce a new cache entry. Cached files are verified by checksum before reuse; missing
or damaged entries are rebuilt. Copies keep each scenario independent, and the cache is saved
before signal wiring or SQLite conversion modifies the scenario files. Existing scenarios from
before this feature do not contain the required network state; the first run after updating
builds the cache. The metadata counter `network_map_cache_hit` records whether it was reused.

```bash
# Another population percentage, same road network/map:
SCENARIO=tl_2pct_meso SAMPLE=0.02 ./generate_tl_1pct_meso.sh
# Force a fresh build without reading or writing the cache:
REUSE_MAP=0 SCENARIO=tl_fresh ./generate_tl_1pct_meso.sh
# Reuse the same cache across output locations:
MAP_CACHE_DIR=/data/toulouse-map-cache SCENARIO=tl_shared ./generate_tl_1pct_meso.sh
```

The direct CLI equivalents are `--no-reuse-map` and `--map-cache-dir PATH`. The usual
requirement that each scenario output directory be new or empty still applies.

When the São Paulo output format changes, port the change here and extend
`tests/test_converter.py`, which pins the shared contract.

---

## Prerequisites

```
python3 >= 3.10
pyproj
scipy
numpy
ijson          # only for --output-format sqlite (tools/scenario-db-converter)
```

## Input files (`--input-dir`, default `.`; the folder `input/` holds exactly these five)

| File | Used for |
|---|---|
| `toulouse_network.xml.gz` | Road network (`network_v2`): nodes, directed links with `length`/`freespeed`/`capacity`/`permlanes`/`modes` |
| `toulouse_population.xml.gz` | Selected plan of every person (`population_v6`), streamed one `<person>` at a time |
| `toulouse_transit_schedule.xml.gz` | Stops, lines, routes, departures (`--transit`, `--rail`) |
| `toulouse_transit_vehicles.xml.gz` | Vehicle types → bus capacity/length and train capacity (`Rail` 400, `Subway` 300, `Tram` 180) |
| `toulouse_meta.json` | eqasim generation metadata, folded into `scenario_metadata.json` |

The rest of the eqasim export (CSV/GeoPackage tables, facilities, households, `toulouse_config.xml`,
`toulouse_run.jar`) is **not read** and lives in `../toulouse_raw_unused/` (with the old
`convert_pt_to_walk.py`, a MATSim-side utility). The trips tables carry no `mode` column
(`person_id;trip_index;preceding_activity_index;…`); the chosen mode only survives in the plans, so
the converter reads the plans directly.

## Coordinate system

`toulouse_config.xml` sets `coordinateSystem = "Atlantis"`, which is eqasim's anonymisation label,
not a real CRS. The coordinates match **EPSG:2154 (Lambert-93)**: a node near central Toulouse is at
about `x=575421, y=6278866`. That is an assumption verified by magnitude, not by metadata. It is
recorded in `scenario_metadata.json` (`source.populationCrs`) and can be changed with
`--population-crs`. Network, plans and transit schedule share one projected space, so:

* `NodeState.latitude/longitude` are the WGS-84 conversion (needed by the engine's A* heuristic);
* snapping (KD-tree) runs directly on the projected x/y, with no round trip.

If the assumed CRS were wrong, the node lat/lon would land outside Toulouse. Check them before
trusting any geography in the output.

---

## What is converted, and how

### Network
One `Node` per MATSim node and one `Link` per directed MATSim link (no reverse-link synthesis, the
file already has both directions). Speeds are km/h, `permlanes` rounds to lanes (a `bus_lane` /
`bike_lane` lane type is set when the link allows `pt` / `bike`).

**Rail-only links are dropped.** The network also carries railway links (`rail`, `pt,rail,train`).
A link is a road link only if its `modes` contain `car` or `bus`; the rest are counted in
`links_skipped_non_road_mode`. Emitting them would let cars and walkers route over tracks.

The main strongly-connected component is computed over the road links and the snapping KD-tree only
contains its nodes. Links usable by buses are written to `data/bus_routing_links.json`.

### Population
Per person, the selected plan is collapsed into real activities and trips:

* `"car interaction"` / `"pt interaction"` / `"car_passenger interaction"` stub activities are
  dropped. The legs between two real activities form one trip; its mode is the single non-walk mode
  of the group. Two different non-walk modes in one group abort the run.
* Activity end: `end_time` → `AtTick`; else `dur`/`max_dur` → `Duration`; else the horizon
  (`matsim_activity_end_fallback_horizon`). The first leg's `dep_time` overrides the departing
  activity's end.
* Times are parsed strictly (`HH:MM:SS`, hours may exceed 24, or plain seconds). A missing or
  malformed time raises instead of becoming tick 0.
* Persons with one activity or no trips are skipped. A person whose activity is farther than
  `--max-snap-distance` (default 1000 m) from the network is skipped, not teleported
  (`persons_skipped_snap_distance`).

One vehicle actor per private mode used is emitted, parked at the first departure node, with an
`owner` / `part_of` dependency on the person (`--no-declare-ownership` turns it off). `car` → `Car`,
`bike` → `Bicycle`, `motorcycle` → `Motorcycle`.

### Unresolved modes (`--unsupported-mode`)
Only `car`, `bike` and `walk` have a native leg. In this dataset that leaves **`pt`** and
**`car_passenger`** unresolved (and `pt` trips are common). Nothing is silently walked:

| Policy | Effect |
|---|---|
| `error` (default) | abort at the first such trip, naming the person and modes |
| `walk` | `WalkLeg` for the whole trip; counted in `trips_mode_fallback_walk` |
| `skip-person` | drop the person (`persons_skipped_unsupported_mode`) |
| `replan` | `PendingDecision` resolved at runtime by `--mc-strategy` (needs `--transit`) |

A run without an explicit policy therefore stops at the first `pt` or `car_passenger` trip. That is
intended; choose the policy you mean. `walk` changes the travel pattern of those persons, so say so
in any result that uses it.

### Dynamic mode choice
`--dynamic-mode-choice` makes **every** trip a `PendingDecision` (flags `--mc-*` are identical to
São Paulo's). Vehicles for the modes listed in `--mc-modes` are created up front so the engine can
pick them. `subway` is rejected: this converter emits no rail actors.

### Bus transit (`--transit`)
Only routes whose own `<transportMode>` is `bus` are converted (1732 of ~2100 routes in the real
file); `rail`, `subway`, `tram` are tallied in the report (`transit_routes_mode_*`) but not
simulated. The `<transportMode>` element decides, never stop-id substrings (`OCECar` stops are SNCF
coach substitutes and are `bus`).

Same model as São Paulo's GTFS converter:

* `BusStop` actors, one per physical `stopFacility`, shared by every route that serves it.
* `Bus` actors, one per scheduled `<departure>`, `--max-buses-per-route` (`0` = all, the default) per
  route, activated at the departure time. Each carries its stop map and a precomputed closed road
  path `stop₀ → … → stopₙ → stop₀` (`storedBestRoute`) from A* over `bus_routing_links.json`. Buses
  then circulate until the horizon; arrival adherence to the schedule and trip-end retirement are
  **not** modelled.
* Stops snap by their own x/y (the `linkRefId` often names pseudo pt-only links). A route with an
  unsnappable stop, a revisited stop (loop), fewer than two distinct stops, no departure, or no
  directed road path between consecutive stops is skipped and counted
  (`transit_routes_skipped_*`).
* `transit_map.json` (stop index, with the lines serving each stop) and `transit_routes.json`
  (RAPTOR routes; per-hop travel time from the `routeProfile` arrival offsets, headway = median gap
  between departures, fallback `--transit-headway`).

The old `BusStation` actors are no longer emitted.

### Rail, subway and tram (`--rail`)
`--transit` enables buses and rail/subway/tram together. Use `--transit --no-rail`
for buses only, or `--rail` without `--transit` for rail services only. The wrapper
also explicitly passes `--rail`.

Read from the **same schedule** in the same pass (no GTFS is needed or downloaded: the schedule
already holds the region's TER trains, the metro and the tram, derived upstream from the national
feeds). Routes with `<transportMode>` `rail`, `subway`, `tram` become the São Paulo rail model:
`SubwayStation` actors (garage stations carry the train definitions) and one `RailLink` per segment
and direction, plus their entries in `transit_map.json` / `transit_routes.json` (`stopType:
"subway"`) and `dynamicActorTypes: ["hybrid.actor.Subway"]` in `simulation.json`.

The schedule has one `<transitRoute>` per direction **and per stopping pattern** (the 39 train
lines have up to 16 each), whereas HTC models a line as one station sequence served both ways. So
per `transitLine`:

* the pattern with the most scheduled departures is the line; the others are dropped and counted
  (`rail_rail_patterns_unused`);
* the return direction is that sequence reversed, so a one-way service runs both ways here;
* stations snap to the nearest road node within `--max-snap-distance`; a line with an unsnappable
  station is dropped whole (`rail_rail_routes_skipped_unsnapped_stop`) rather than shortcut;
* segment length is the straight-line station separation (floor 100 m), not the track;
* train speed = that length over the schedule's own run time (clipped 3–40 m/s; fallback
  `--train-velocity`), headway = median gap between the pattern's departures, capacity from the
  vehicle type, at most `--max-trains-per-line` trains per direction.

These are scenario assumptions, not an operating timetable. The counters appear with a
`rail_rail_` prefix in `scenario_metadata.json` (stage prefix + converter counter name).
Per-mode counts are available as `rail_lines_subway`, `rail_lines_tram`, `rail_lines_rail`
and corresponding `rail_trains_*` counters, and are printed at completion. All three modes
use the engine's `Subway` actor: trains are embedded in station garages and created at runtime,
so there are no separate `trams_*.json` or `trains_*.json` actor files. Rail links distinguish
`SUBWAY`, `LIGHT_RAIL`, and `HEAVY_RAIL`.

### Traffic lights from OSM

Provide a local OSM XML or XML.gz extract containing `highway=traffic_signals` nodes:

```bash
python3 generate_hybrid_input.py --input-dir input --scenario tl_signals \
  --sample 0.001 --unsupported-mode walk \
  --signals --signal-osm-file toulouse.osm.gz --signal-max-snap-distance 50

# With the full-scenario wrapper (also generates buses, metro, tram and TER):
SIGNAL_OSM_FILE=toulouse.osm.gz ./generate_tl_1pct_meso.sh
```

`--signal-osm-file` is relative to `--input-dir` unless absolute. PBF must first be
converted to XML with `osmium cat input.osm.pbf -o toulouse.osm`. The OSM file is
read locally; the generator does not download it. OSM coordinates are transformed
to `--population-crs` before matching to a road node with incoming links. The
separate 50 m limit prevents use of the 1,000 m activity/stop snap radius.

Several OSM stop lines at one MATSim node share one controller. Unmatched signals
are counted, and `data/osm_signal_matches.json` records matched OSM IDs by MATSim
node. `traffic_signals_*.json` (or `.db`) are registered in `simulation.json`.
Positions come from OSM; cycle and phases are synthetic (`--signal-cycle 120`),
including a red interval for a single incoming approach. Turn-specific phases and
observed signal timing are not inferred. MICRO links receive their own approach phase;
HTC's MESO gate uses the outgoing link, associated here with the straightest incoming
approach. This does not preserve turn-specific control. Preserve OpenStreetMap attribution when
redistributing its derived data.

---

## Output layout (`output/<scenario>/`)

```
<scenario>/
├── data/
│   ├── nodes_*.json  links_*.json  persons_*.json  cars_*.json
│   ├── bikes_*.json  motorcycles_*.json       only if such vehicles exist
│   ├── bus_stops_*.json  buses_*.json         --transit
│   ├── subway_stations_*.json  rail_links_*.json   --rail
│   ├── traffic_signals_*.json                  --signals
│   ├── transit_map.json  transit_routes.json  --transit and/or --rail
│   ├── city_map.json                          A* graph index
│   ├── bus_routing_links.json  id_registry.json   internal, not actor files
├── simulation.json            HTC manifest
├── scenario_metadata.json     counters, CRS, input snapshots (path/size/mtime)
├── effective_config.json      every resolved option
└── GENERATION_REPORT.md       assumptions + all counters
```

Ids are unique `long`s across all actor types, deterministic for a given seed and inputs.
`--output-format sqlite` converts every `data/*.json` actor file and `city_map.json` through
[`tools/scenario-db-converter`](../scenario-db-converter/convert.py) and deletes the JSON.

**The output directory must be new or empty.** A rerun into a used directory is refused, because
stale `persons_N.json` / `buses_N.json` from a larger run would be loaded next to the new files.

---

## More examples

```bash
# Smoke test: 0.1 % of the population, pt/car_passenger trips walked (explicit, counted)
python3 generate_hybrid_input.py --scenario tl_test --sample 0.001 --unsupported-mode walk

# 1 %, routed buses, pt/car_passenger replanned at runtime among bus and walk
python3 generate_hybrid_input.py --scenario tl_1pct --sample 0.01 --transit \
    --unsupported-mode replan --mc-modes bus walk

# Every trip decided at runtime by the travel-time engine
python3 generate_hybrid_input.py --scenario tl_mc --sample 0.01 --transit \
    --dynamic-mode-choice --mc-strategy travel-time --mc-modes car bicycle bus walk
```

## Known results

A 1 % run (seed 42, `replan`, buses + rail) produced: 488,523 nodes, 1,076,102 links (all MESO),
20,481 people, 12,216 cars, 748 bicycles, 3,881 bus stops, 8,822 buses from 1,706 routes, 142 rail
stations, 434 rail links, 348 trains, and 30 of the 42 rail/subway/tram lines (the rest had an
unsnappable station).

**In historical random-sampling runs, about 30 % of sampled people were dropped** (`persons_skipped_snap_distance`): their activities
lie in PACA, more than 100 km from the road network, which covers only Occitanie. Dropping them is
correct, but people who travel out of the region are under-represented. A further ~20 % have no
trips at all (`persons_skipped_no_trips`).

Not verified: loading this output in the HTC engine (there is no engine smoke test for Toulouse yet,
unlike São Paulo's `tests/run_smoke.py`), and whether the rail stations and speeds are realistic
beyond being within 6–30 m/s.

## Parameters

| Argument | Default | Description |
|---|---|---|
| `--scenario NAME` | *(required)* | Output subdirectory |
| `--input-dir DIR` | `.` | Directory with `toulouse_*.xml.gz` |
| `--output DIR` | `output` | Parent output directory |
| `--output-format` | `json` | `json` or `sqlite` |
| `--sample F` | `0.01` | Fraction of persons, in (0, 1] |
| `--sampling-strategy` | `prefix` | File-order generated target, or `random` for full-file Bernoulli selection |
| `--population-size N` | cached/count pass | Known source person count, avoiding the first count pass |
| `--reuse-map` / `--no-reuse-map` | on | Reuse verified road network and city-map cache |
| `--map-cache-dir PATH` | `OUTPUT/.toulouse-map-cache` | Shared map cache location |
| `--seed N` | `42` | Random seed |
| `--population-crs CRS` | `EPSG:2154` | Projected CRS of network, plans and schedule |
| `--unsupported-mode` | `replan` | `error` / `walk` / `skip-person` / `replan` |
| `--max-snap-distance M` | `1000` | Max activity/stop snap distance (metres) |
| `--duration S` | `86400` | Inclusive horizon in seconds (1 s ticks) |
| `--start-real-time ISO` | `2025-01-01T00:00:00.000` | Real-time origin |
| `--items-per-file N` | `50000` | Entries per split JSON file |
| `--docker-base PATH` | `/app/hyperbolic-time-chamber/simulations/input` | Path prefix inside `simulation.json` |
| `--declare-ownership` / `--no-declare-ownership` | on | `owner`/`part_of` on private vehicles |
| `--transit` / `--no-transit` | on | Buses plus rail/subway/tram by default; transit indexes |
| `--rail` / `--no-rail` | follows `--transit` | Override rail/subway/tram generation |
| `--signals` / `--no-signals` | on when OSM supplied | Traffic lights extracted from local OSM |
| `--signal-osm-file PATH` | required with signals | OSM XML/XML.gz path relative to input-dir |
| `--signal-max-snap-distance M` | `50` | Maximum OSM signal to road-node matching distance |
| `--signal-cycle S` | `120` | Synthetic signal cycle in seconds |
| `--max-trains-per-line N` | `0` | Trains per line and direction; `0` = all within the horizon |
| `--train-capacity` / `--train-interval` / `--train-velocity` / `--train-stop-time` | `1200` / `300` / `22.22` / `30` | Fallbacks (vehicle type, headway s, speed m/s) and dwell time s |
| `--max-transit-routes N` | `0` | Bus routes converted, file order; `0` = all |
| `--max-buses-per-route N` | `0` | Earliest departures per route; `0` = all |
| `--transit-headway S` | `900` | Fallback headway for routes with < 2 departures |
| `--micro-ratio F` | `0.0` | Fraction of links as MICRO (0 = full MESO) |
| `--micro-strategy` | `highway` | `highway` / `arterial` / `random`; the road class is derived from free-flow speed |
| `--driver-*-min/max` | see `--help` | Uniform ranges for aggressiveness, reaction time, speed factor, gap factor |
| `--dynamic-mode-choice` | off | `PendingDecision` for every trip |
| `--mc-strategy` | `nearest-stop-utility` | `raptor` / `nearest-stop-utility` / `travel-time` |
| `--mc-modes M…` | `bus subway walk` | Modes the strategy may pick |
| `--mc-*` | see `--help` | Weights and average speeds, identical to São Paulo |
| `--network-file`, `--population-file`, `--transit-schedule-file`, `--transit-vehicles-file` | auto | Override discovered input paths |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They run on a tiny synthetic MATSim scenario and cover the actor contract, interaction-stub
collapse, strict times, the four unresolved-mode policies, rail-link exclusion, snap distance,
routed buses and the transit indexes, `simulation.json` pointing at files that exist, id
uniqueness, and the empty-output guard. They do not run the simulator: unlike São Paulo there is no
engine smoke test yet.

## Not supported

* **Rail variants beyond the dominant pattern** of each line, and one-way services (see `--rail`).
* **`pt` and `car_passenger` as real legs**: need a MATSim-facility → HTC-stop correspondence.
  They are walked, skipped, or replanned by policy.
* **Observed signal timing and turn-specific control**: OSM provides locations; phase plans are synthetic.
* `--bbox`, `--skip-network`, `--input-format`: not ported from São Paulo.
* Calibration: a successful conversion says nothing about whether the simulated traffic is
  realistic. Bus counts are not scaled with `--sample`.
