# InterSCSimulator → HTC Scenario Converter

Converts an [InterSCSimulator](https://interscity.org/software/interscsimulator/)
(MATSim-style XML) scenario into the two input datasets the **Hyperbolic Time Chamber
(HTC)** hybrid traffic simulator loads: `city_map` (routing graph) and `actor_simulation`
(agent definitions), in JSON and/or SQLite — the same two schemas defined by
[`tools/scenario-db-converter/convert.py`](../scenario-db-converter/convert.py).

**Priority**: the road-network map (`network.xml` → `city_map` + `Node`/`Link` actors) and
traffic-signal actors (`signals.xml` → `TrafficSignal` actors) are the robust, well-tested
part of this converter. Person/car OD-trip conversion (`trips.xml`) emits the *current*
`PersonState`/`CarState`/`BicycleState`/`MotorcycleState` shapes (verified field-by-field
against `hyperbolic-time-chamber/src/main/scala/model/hybrid/entity/state/`, not copied from
any pre-existing generator — see "Person/Car actor shape" below). Bus/metro
(`--bus-lines`/`--metro-lines`) transitSchedule.xml parsing remains best-effort/unverified
(no such input ships with this scenario family) — see the caveats below.

Beyond straight 1:1 conversion, this converter also **synthesizes** entity types
InterSCSimulator's `base_scenario` never provides: bus lines, bus stops/stations, subway
lines/stations, and bike trips — derived from the converted map's own topology (connectivity,
geography) rather than invented from nothing. See "Synthetic transit network" and "Synthetic
bike trips" below for exactly what's real (from XML) vs. synthesized and which heuristics
were used.

---

## Prerequisites

```
python3 >= 3.10
pyproj
```

Everything else is Python standard library. `pytest` is only needed to run the test suite.

```bash
pip install -r requirements.txt
```

---

## Input files

| File | Required | Description |
|---|---|---|
| `network.xml` | yes | `<nodes>`/`<links>` MATSim-style road network |
| `trips.xml` | no | `<scsimulator_matrix>` OD trip matrix |
| `signals.xml` | no | `<traffic-signals>` intersection signal timing |
| `--bus-lines FILE` | no | best-effort MATSim `transitSchedule.xml`-like bus feed |
| `--metro-lines FILE` | no | best-effort MATSim `transitSchedule.xml`-like metro feed |

Confirmed against `tools/interscsimulator_scenario/input/base_scenario/*.xml` (read in full
before writing this converter, not assumed):

```xml
<!-- network.xml -->
<nodes>
  <node id="1001568643" x="-7347433.28816257" y="-2852981.6323715686"/>
</nodes>
<links capperiod="01:00:00" effectivecellsize="7.5" effectivelanewidth="3.75">
  <link id="1" from="394923340" to="2033271141" length="13.54" freespeed="4.17"
        capacity="600.0" permlanes="1.0" oneway="1" modes="car"/>
</links>

<!-- trips.xml -->
<scsimulator_matrix>
  <trip name="paraiso_regular_0" origin="165466550" destination="60609874"
        link_origin="2105" count="1" start="6" mode="car" digital_rails_capable="false"/>
</scsimulator_matrix>

<!-- signals.xml -->
<traffic-signals>
  <signal cycle_duration="90" offset="0">
    <nodes><node id="2098758071"/><node id="60609643"/></nodes>
    <phases><phase origin="4236712736" green_duration="45" green_start="0"/></phases>
  </signal>
</traffic-signals>
```

No bus-line/bus-stop/metro example ships with this scenario family — only
`network.xml`, `trips.xml`, `signals.xml`, `config.xml`, `mini-map.xml`, `mini-trips.xml`,
`demo-bikes-map.xml`, `demo-bikes-trips.xml`, `empty-signals.xml`,
`empty-digital-rails.xml`, `arrivals.xml` exist under `input/base_scenario/` (verified by
listing the directory). `--bus-lines`/`--metro-lines` are implemented best-effort against the
*documented* MATSim `transitSchedule.xml` structure and exercised only against a hand-built
test fixture — see `parse_transit_schedule`'s docstring.

---

## Coordinate system — not assumed, empirically confirmed

`network.xml` gives no CRS. The instinctive guess for a São Paulo scenario with coordinate
magnitudes around `x≈-7.3M, y≈-2.85M` is Web Mercator (EPSG:3857), but that guess is wrong
here: `base_scenario/network.xml` node ids turn out to be literal OpenStreetMap node ids
(e.g. node `60609643`, live-checked via `api.openstreetmap.org/api/0.6/node/60609643.json`,
is tagged `highway=traffic_signals` — consistent with its use in `signals.xml` as part of
the "Paulista x Bela Cintra" signal). Fetching live OSM lat/lon for a spread of network nodes
and comparing against `network.xml`'s `(x, y)`:

- Plain EPSG:3857 (Web Mercator): residual **>2 km** — ruled out.
- EPSG:32723 (WGS84 / UTM zone 23S) + a fitted affine correction (translation + ~1.08°
  rotation + ~0.4-0.5% scale, least-squares fit against 11 nodes): out-of-sample residual
  **1.5-11 m** across 6 held-out nodes spanning the network's ~30 km extent.

The fitted affine coefficients live in `convert_interscsimulator.py` as
`matsim_sp_xy_to_lonlat`, with the full derivation in the module docstring. This is
**not** a documented InterSCSimulator convention — it's reverse-engineered for this specific
São Paulo (Avenida Paulista area) scenario family. A `network.xml` generated elsewhere may
use a different projection entirely.

`--projection` controls this:

| Value | Behavior |
|---|---|
| `auto` (default) | `matsim-sp-utm23s` when `\|x\|` or `\|y\|` exceeds `--large-coord-threshold` (default 10 000 m), else `none` |
| `matsim-sp-utm23s` | the fitted affine + UTM 23S inverse described above |
| `none` | treats `(x, y)` as a flat meter offset (east, north) from `--anchor-lat`/`--anchor-lon`, via an equirectangular approximation — only adequate for small synthetic grids (e.g. `demo-bikes-map.xml`, whose `x`/`y` are in the 0-50 range and aren't geographic at all) |

---

## Output layout

```
<output>/
├── city_map.json          Routing graph (nodes/edges) for CityMapUtil
├── actor_simulation.json  Node/Link/TrafficSignal/Person/Car/Bicycle/Motorcycle actors
│                          (+ synthetic BusStop/BusStation/Bus/SubwayStation/RailLink
│                          actors unless --no-synth-transit is given)
├── city_map.db            (only with --format sqlite)
└── actor_simulation.db    (only with --format sqlite)
```

`--format sqlite` reuses (imports, does not duplicate)
[`tools/scenario-db-converter/convert.py`](../scenario-db-converter/convert.py)'s
`city_map_json_to_db`/`actor_simulation_json_to_db` functions via a relative `sys.path`
import (`../scenario-db-converter/convert.py` next to this script's `tools/` directory).
This keeps the two schemas defined in exactly one place instead of duplicating the DDL/row
mapping here — the tradeoff is that `--format sqlite` needs the monorepo layout intact (or
`convert.py` copied alongside this script); `--format json` has no such dependency.

---

## Quick start

```bash
# Full base scenario (map + traffic signals + best-effort persons/cars), JSON output
python3 convert_interscsimulator.py \
  --network input/base_scenario/network.xml \
  --trips input/base_scenario/trips.xml \
  --signals input/base_scenario/signals.xml \
  --format json --output output/base_scenario_json

# Map + signals only (skip person/car generation)
python3 convert_interscsimulator.py \
  --network input/base_scenario/network.xml \
  --signals input/base_scenario/signals.xml \
  --format json --output output/map_only

# Tiny fixture, SQLite output, local (non-projected) coordinates
python3 convert_interscsimulator.py \
  --network input/base_scenario/demo-bikes-map.xml \
  --trips input/base_scenario/demo-bikes-trips.xml \
  --projection none --format sqlite --output output/demo_bikes
```

Inspect a SQLite result's schema directly:

```bash
sqlite3 output/demo_bikes/city_map.db ".schema"
sqlite3 output/demo_bikes/actor_simulation.db "select type_actor, count(*) from actor_simulation group by type_actor;"
```

---

## All parameters

| Argument | Default | Description |
|---|---|---|
| `--network FILE` | *(required)* | `network.xml` path |
| `--trips FILE` | none | `trips.xml` path (optional — skips Person/Car generation if omitted) |
| `--signals FILE` | none | traffic-signals `.xml` path (optional — skips TrafficSignal generation if omitted) |
| `--bus-lines FILE` | none | best-effort transitSchedule-like bus feed |
| `--metro-lines FILE` | none | best-effort transitSchedule-like metro feed |
| `--projection` | `auto` | `auto` \| `matsim-sp-utm23s` \| `none` — see coordinate section above |
| `--anchor-lat` | `-23.55` | anchor latitude for `--projection none` |
| `--anchor-lon` | `-46.63` | anchor longitude for `--projection none` |
| `--large-coord-threshold` | `10000` | meters; `auto` projection cutoff |
| `--start-unit` | `minutes` | unit of `trips.xml`'s `start` attribute — see caveat below |
| `--synth-bike-fraction` | `0.0` | fraction of car trips randomly relabeled as bike trips (see "Synthetic bike trips") |
| `--synth-seed` | `42` | RNG seed for bike-trip / extra-OD-trip synthesis |
| `--synth-extra-od-trips` | `0` | additional random car/bike OD trips beyond `trips.xml` (see "Extra synthetic OD trips") |
| `--synth-extra-od-car-fraction` | `0.7` | fraction of `--synth-extra-od-trips` assigned `mode=car` (rest `mode=bike`) |
| `--no-synth-transit` | off | disable synthetic bus/subway network generation (see "Synthetic transit network") |
| `--synth-bus-lines` | `4` | number of synthetic bus lines (`0` disables) |
| `--synth-bus-stops-per-line` | `6` | stops per synthetic bus line |
| `--synth-bus-headway` | `600` | seconds between synthetic bus dispatches |
| `--synth-bus-capacity` | `80` | passenger capacity per synthetic bus |
| `--synth-bus-ports` | `2` | boarding doors per synthetic bus |
| `--synth-buses-per-line` | `4` | number of `Bus` vehicle actors dispatched per line |
| `--synth-subway-lines` | `1` | number of synthetic subway lines (`0` disables) |
| `--synth-subway-stations-per-line` | `6` | stations per synthetic subway line |
| `--synth-subway-headway` | `300` | seconds between synthetic train dispatches |
| `--synth-subway-capacity` | `300` | passenger capacity per synthetic train |
| `--synth-subway-ports` | `4` | boarding doors per synthetic train |
| `--synth-subway-speed-ms` | `22.22` | synthetic train cruise speed, m/s (~80 km/h) |
| `--synth-subway-stop-time` | `30` | seconds a synthetic train dwells per station |
| `--synth-subway-trains-per-line` | `3` | trains dispatched per synthetic subway line |
| `--synth-hub-min-spacing-m` | `300.0` | minimum spacing between synthetic bus-stop hubs (subway hubs use 2x this) |
| `--format` | `json` | `json` or `sqlite` |
| `--output DIR` | *(required)* | output directory |

---

## Person/Car actor shape

`build_person_and_vehicle_actors` emits the **current** engine shapes, verified field-by-field
against the Scala case classes (not copied from any pre-existing generator — as of this
writing, none of `tools/sao-paulo/generate_hybrid_input.py`,
`tools/toulouse/generate_hybrid_input.py`, or
`hyperbolic-time-chamber/scripts/scenario/generate_tiny_validation_scenario.py` have been
updated to the current shape either; this converter is the first to target it):

- **`PersonState`** (`model/hybrid/entity/state/PersonState.scala`): only `startTick`,
  `scheduleOnTimeManager`, `originalPlan`, `ownedVehicles`, `vehicleCurrentNode`,
  `totalDistanceTraveled`, `completedTrips`, `ptWaitTimeoutTicks` are emitted. `cursor` and
  `tripExecution` are **deliberately omitted** — `Person.internStateStrings` seeds `cursor`
  from `originalPlan` on first load (see that method's doc and
  `PersonStateJsonSpec`'s "deserialize a scenario-shaped payload" test, which is exactly this
  shape). There is no top-level `origin`/`destination`/`dailySchedule`/`currentActivityIndex`
  anymore — those fields don't exist on the current `PersonState` at all; the trip's
  origin/destination live inside `originalPlan`'s `Activity` nodes instead.
- **`originalPlan`** is `[Activity(origin) -> PrivateVehicleLeg(mode) -> Activity(destination)]`
  — a single contiguous trip, since `trips.xml` has no return-trip semantics (each row is one
  directed OD leg).
- **Vehicle actors** (`CarState`/`BicycleState`/`MotorcycleState`) emit only each state's real
  constructor fields — confirmed `BicycleState`/`MotorcycleState` have no `precomputedRoute`
  field (unlike `CarState`), so it's omitted for those two, not emitted as `null`.

---

## Synthetic transit network (bus + subway)

No InterSCSimulator bus-line/bus-stop/metro example ships with this scenario family (only
`network.xml`/`trips.xml`/`signals.xml`/`config.xml`/`mini-map.xml`/`mini-trips.xml`/
`demo-bikes-map.xml`/`demo-bikes-trips.xml`/`empty-signals.xml`/`empty-digital-rails.xml`/
`arrivals.xml` exist under `input/base_scenario/`). Rather than leaving bus/subway entirely
absent, this converter **synthesizes** a plausible transit network directly from the
converted road network's topology, on by default whenever `--network` is given (disable with
`--no-synth-transit`):

1. **Hub selection** (`select_hub_nodes`): rank raw network nodes by undirected degree
   (in-links + out-links) — busier intersections are more plausible transit hubs than
   degree-1 dead ends — then greedily pick the highest-degree nodes subject to a minimum
   geographic spacing (`--synth-hub-min-spacing-m`, subway uses 2x that) so hubs don't
   cluster on the same intersection. Falls back to ignoring spacing if too few nodes satisfy
   it (relevant for small/synthetic fixtures like `mini-map.xml`).
2. **Line partitioning** (`partition_into_lines`): subway hubs are picked first (fewer, more
   spread out, so they claim the highest-connectivity corridors); bus hubs are picked from
   the remaining nodes so the two networks don't collide. Each hub set is sorted
   geographically (by longitude) and chunked into lines so each line reads as a rough
   corridor, not a random jumble of stops.
3. **Actor generation** — field shapes confirmed against
   `model.hybrid.entity.state.{BusStopState,BusStationState,BusState,SubwayStationState,
   RailLinkState}.scala`, mirroring the real GTFS-driven reference implementation in
   `tools/sao-paulo/generate_hybrid_input.py` (`_emit_route` for buses,
   `_create_stations`/`_create_rail_links`/`_configure_garages` for subway) with two
   deliberate scope cuts, documented as such (not oversights):
   - **Forward direction only** — no separate return route/line, unlike the reference
     generator's `_ret` return-direction handling. A reasonable simplification for a
     synthetic scenario with no real timetable to mirror both ways.
   - One correction *over* the reference generator: this converter's `RailLinkState` JSON
     always includes `startTick` (required, no default in the case class) and omits
     `laneConfigurations`/`vehiclesByLane` (fields that exist on `LinkState`, not
     `RailLinkState` at all) — the reference generator gets both of these wrong.

Real base_scenario run (`--synth-bus-lines 4 --synth-subway-lines 1`, all other synth flags
at their defaults): 4 bus lines / 24 bus stops / 4 bus stations / 16 buses, 1 subway line / 6
subway stations / 5 rail links / 3 trains.

---

## Synthetic bike trips

`base_scenario/trips.xml` has **no real bike-mode demand** — all 6439 trips are `mode="car"`
(confirmed: `grep -o 'mode="[^"]*"' trips.xml | sort -u` yields only `car`).
`--synth-bike-fraction F` (default `0.0`, so the previous car-only behavior is reproduced
exactly unless explicitly raised) randomly relabels that fraction of car trips to `mode=bike`
(`synthesize_bike_trips`, seeded by `--synth-seed` for reproducibility) — reusing real
origin/destination/start values rather than inventing new OD pairs, since a car OD pair is at
minimum a physically connected, demand-plausible pair of network nodes. A run with
`--synth-bike-fraction 0.1` against `base_scenario` converts roughly 644 of the 6439 trips to
`hybrid.actor.Bicycle`.

Note: synthetic bike trips are ordinary single-leg private-vehicle trips (like car trips) —
this does **not** attempt to route people through the synthetic bus/subway network above;
combining topology-driven PT stop-snapping with multi-leg `TransitLeg` plans is a
substantially larger scope than infrastructure synthesis and is left for future work (tracked
as a known gap, not a silent omission).

---

## Known best-effort gaps

1. **`trips.xml`'s `start` unit is not documented anywhere found.** Cross-checking
   `base_scenario/trips.xml` against `base_scenario/arrivals.xml` (real InterSCSimulator
   output) didn't cleanly confirm minutes vs. seconds — InterSCSimulator appears to jitter
   departures within a `start` bucket rather than departing at exactly that instant, so no
   simple `start * k` recovers the logged arrival times exactly. This converter defaults to
   treating `start` as **minutes since midnight**; pass `--start-unit seconds` if a given
   scenario's provenance says otherwise.

2. **Bus/metro support via `--bus-lines`/`--metro-lines` (MATSim `transitSchedule.xml`-like
   input) is unverified.** No InterSCSimulator transit example exists in this scenario
   family; `parse_transit_schedule` follows the documented MATSim `transitSchedule.xml` shape
   and is unit-tested only against a hand-built fixture. `BusStop` actors are emitted with
   `nodeId: null` since a transit schedule alone gives no road-network node — snap to the
   nearest `city_map` node before use. (This is independent of the topology-driven synthetic
   transit network above, which needs no such input file.)

3. **Traffic-signal phase resolution falls back to a node-id placeholder** when a
   phase's `origin` (an upstream approach node in `signals.xml`) has no matching incoming
   link into the signal's controlled node(s) in `network.xml` (can happen with truncated
   mini-fixtures cutting off approach links). This fallback is visible in
   `TrafficSignalState.phases[].origin` as `htcaid:node;...` instead of
   `htcaid:link;...` and won't match a real Link actor id — check for this if a generated
   signal looks inert in the engine.

4. **Synthetic bike trips and the synthetic transit network are independently generated** —
   no person is ever routed onto the synthetic bus/subway network (see "Synthetic bike
   trips" above).

---

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/ -v
```

Covers: coordinate reprojection (both against a live-OSM-confirmed known point and
out-of-sample bounds checks), `network.xml`/`trips.xml`/`signals.xml` parsing (inline
fixtures + the repo's own `mini-map.xml`/`mini-trips.xml`/`signals.xml`/`empty-signals.xml`),
Node/Link/TrafficSignal/Person/Car/Bicycle/Motorcycle actor generation against the current
`PersonState`/`CarState`/`BicycleState`/`MotorcycleState` shapes, `city_map` shape,
bike-trip synthesis, topology-driven hub selection/line partitioning and synthetic bus/subway
actor generation, best-effort transitSchedule.xml parsing, and an end-to-end JSON + SQLite
pass against both `base_scenario` (full, ~4.5k nodes/~7k links/13 signals/~6.4k trips) and
the mini fixtures.
