# Implementation guide for AI agents: São Paulo → HTC

## Objective and boundary

Maintain a configurable **offline Python converter** from EQASim's São Paulo synthetic population or MATSim selected plans into HTC's native scenario format. Read the local HTC `AGENTS.md` and current Scala state classes before changing output schemas. A Python conversion success is not simulator validation and does not establish scientific validity.

Work in `tools/sao-paulo`. The sibling HTC repository is `../../hyperbolic-time-chamber`. The tools directory may not be a Git checkout; do not assume its edits appear in the simulator repository's `git diff`.

The user controls demand sampling, source CRS, spatial extent, data snapshots, service date, fleet caps, horizon, unresolved-mode policy, MICRO assignment, signals, output paths and format. Never infer research parameters from a filename or silently tune a population to pass a test.

## Read the contract first

Use these simulator files as the authoritative field/type contract:

| Concern | HTC path |
|---|---|
| Manifest, actor sources, policies | `src/main/scala/core/entity/configuration/Simulation.scala` and `ActorDataSource.scala` |
| Person plan and ownership | `src/main/scala/model/hybrid/entity/state/PersonState.scala` |
| Discriminated plan JSON | `src/main/scala/model/hybrid/entity/state/plan/PlanElement.scala`, `EndTimeSpec.scala`, `StopRef.scala` |
| Owned vehicles | `CarState.scala`, `BicycleState.scala`, `MotorcycleState.scala`, `MovableState.scala` in the same state package |
| Bus schema and actual route lifecycle | `BusState.scala`, `model/hybrid/actor/Bus.scala`, `support/bus/BusLinkHandler.scala` |
| Rail dispatch | `SubwayStationState.scala`, `SubwayState.scala`, `support/subwaystation/SubwayStationCreator.scala` |
| IDs and JSON decoding | `src/main/scala/core/util/JsonUtil.scala` |
| Intended import semantics | `docs/MATSIM_CONVERSION_GUIDE.md`, `docs/SCENARIO_MODELING.md` |

The native actor envelope is:

```json
{
  "id": 101,
  "typeActor": "hybrid.actor.Person",
  "data": {
    "dataType": "model.hybrid.entity.state.PersonState",
    "content": {
      "startTick": 28800,
      "scheduleOnTimeManager": true,
      "originalPlan": [
        {"kind": "Activity", "activityType": "home", "nodeId": 1,
         "endTime": {"kind": "AtTick", "tick": 28800}},
        {"kind": "WalkLeg", "originNodeId": 1, "destinationNodeId": 2,
         "precomputedRoute": null},
        {"kind": "Activity", "activityType": "work", "nodeId": 2,
         "endTime": {"kind": "AtTick", "tick": 86400}}
      ]
    }
  },
  "dependencies": {}
}
```

Do not emit obsolete `dailySchedule` or a prepopulated execution cursor. IDs are global positive `Long` values allocated through `ScenarioIdRegistry`; never independently number entity families. Use native fields such as `movableStatus`, not an inert field named `status`, when initializing inherited mutable vehicle state. An owned car must start `Parked` with `scheduleOnTimeManager=false`. Ownership keys are `car`, `bicycle`, `motorcycle`; the importer accepts source `bike`, but that is not an ownership key.

## Existing implementation responsibilities

- `generate_hybrid_input.py`: `Config` and CLI; streaming network/population parsing; spatial snapping; GTFS service selection; bus road routing; OSM rail extraction; native actor and manifest writing; metadata/reporting.
- `fetch_inputs.py`: staged, cached GTFS download/local ZIP import; online Overpass rail query; local XML/XML.gz rail extraction; source hashes.
- `explore_rail.py`: configurable rail relation audit, with no import-time work.
- `scenario*.example.json`: runnable configuration shapes that users edit for their sources.
- `tests/test_converter.py`: small behavioral regression fixtures, including both population paths.
- `tests/run_smoke.py`: retained small inputs → converted actors → actual HTC process → outcome checks.

Keep download operations outside the conversion path so conversion is reproducible offline. Do not overwrite an original input snapshot to make an example work. Use provider names/paths to distinguish independently downloaded snapshots and record them in metadata.

## Conversion stages and invariants

1. **Validate configuration and inputs.** JSON option names mirror CLI names; CLI overrides file defaults. Reject unknown keys, invalid modes/ranges/dates, ambiguous scenarios and expired requested GTFS service. Config-relative and input-relative paths have different meanings; preserve the README's rules. Publish `simulation.json` only after required outputs succeed.
2. **Build the OSM road graph.** Stream ways/nodes, retain directed endpoints, and use one registry for actors and city-map edges. `oneway=-1` must create one reverse edge, not an extra forward edge. Keep the selected directed connectivity cohort for snapping. The present high-degree-seed forward∩reverse cohort is strongly connected, but is not guaranteed to be the largest SCC.
3. **Normalize coordinates.** WGS84 OSM coordinates become EPSG:32723 metres. Read GeoPackage SRS definition/authority, including custom SRS rows, instead of assuming WGS84 UTM. MATSim coordinates require an explicit source CRS. Transform before bbox checks and nearest-neighbor queries. Reject nonfinite coordinates and bound snap distance.
4. **Convert population.** EQASim uses semicolon CSV and GeoPackage Point geometry; trip indexing uses disk SQLite and bounded chunks. Preserve activity/trip sequence indexes. MATSim parses one selected plan per person, releases XML subtrees, collapses stage activities, and ignores the source road route unless network equivalence is independently established. Do not splice a plan across missing locations.
5. **Preserve times.** The supported clock is one second per tick. Accept seconds and `HH:MM:SS`, including times beyond 24 hours. Round finite values, reject invalid/missing departure values. Absolute activity end takes priority; duration is relative to actual arrival. Missing final end uses the configured horizon and is counted. Keep source events beyond the horizon identifiable rather than inventing completed travel.
6. **Handle modes explicitly.** Static private/walk modes map to native legs. Unresolved transit/custom modes default to a conversion error. Optional `walk`, `skip-person` and `replan` are explicit scenario changes with counters. Replanning emits `PendingDecision`, not a guessed `TransitLeg`; do not claim it preserves MATSim transit itineraries.
7. **Select GTFS service.** Apply agency-local date, weekday flags and calendar exceptions. Restrict to bus route types. Group by source route, direction and complete ordered stop sequence; namespace all provider IDs and line labels. Expand frequency windows, preserve all-day hours, and clip activation times to the configured inclusive horizon. Count approximations for frequency-based service. Do not concatenate opposing patterns.
8. **Produce executable transit.** The current Bus actor repeats a path, so compute a closed motor-road path through the source stop sequence and back to its origin. BusState requires `size`, ordered stop mapping and `storedBestRoute`. Do not also enqueue the same IDs in a BusStation. The existing road router minimizes distance; it does not implement map matching, GTFS shape adherence or turn restrictions.
9. **Extract rail.** Use OSM rail route stop members in relation order. Preserve stop positions, not just `railway=station` nodes. Select line variants explicitly when more than two distinct sequences exist; `relation_ids` in the line override supports reviewable selection. Generate SubwayStation, RailLink and garage definitions, including return paths. Distinct same-name terminal positions require counted synthetic links within the configured terminal-gap limit; reject incompatible seams. Register `hybrid.actor.Subway` as a dynamic actor type in the manifest. Forward and `_ret` labels must match the route and stop indexes.
10. **Write native outputs.** Use bounded split actor arrays, consistent integer references and actual runtime-visible paths. Export bus and rail route indexes for RAPTOR. Motorcycles cannot share Bicycle data sources. Preserve registry/config metadata for JSON network reuse; remove obsolete transit indexes when disabled. Use the existing SQLite converter rather than inventing a second database schema.
11. **Validate scientific meaning separately.** Report exclusions, replacements, sampling, fleet caps and rail-distance approximations. A small successful run establishes compatibility only for that fixture, policy, clock and executable.

## Data acquisition and provenance

Use the [EQASim São Paulo repository](https://github.com/eqasim-org/eqasim-sao-paulo), [SPTrans developer portal](https://www.sptrans.com.br/desenvolvedores/), [EMTU portal](https://www.emtu.sp.gov.br/emtu/dados-abertos/dados-abertos-principal/gtfs.fss), [GTFS specification](https://gtfs.org/documentation/schedule/reference/) and [OSM Overpass documentation](https://wiki.openstreetmap.org/wiki/Overpass_API) as primary references. Follow portal access requirements; do not invent download credentials or bypass authentication.

GTFS ZIP validation must reject missing core tables and ambiguous duplicated filenames, constrain extracted size, and write only allowed table basenames. Stage downloads before replacing targets. Hash downloaded archives and installed tables. Rail records its extract hash and origin/query. Population metadata currently records file size/mtime and source CRS, **not** a cryptographic seal of the whole population. For a publication campaign, archive and hash the full population/road inputs independently.

## Required validation before delivering changes

Run Python compile checks and the focused unit/integration fixture suite. When actor shapes, route lifecycle or plan semantics change, run the actual simulator smoke test with an existing assembly. Retain its inputs, command, expected outcomes, reports and executable hash. Compare model outcomes, not only process exit or `[CONSERVATIVE-COMPLETE]`.

The smoke fixture has two people, two car trips, one PT trip, four bus activations and two trains. The validator checks three completed person trips, two completed car journeys, a bus boarding, subway arrivals, both person schedules, and no aborted trips. Circulating buses/trains are intentionally censored at the horizon; a `sumo_tripinfo` row with `arrival=-1` is not a completed vehicle journey.

For converter changes, change the fixture to expose the behavior under test. Do not change HTC actor protocols or scientific thresholds to make a converter test pass. If an engine constraint prevents faithful input representation, document the constraint and implement an explicit mapping/policy or fail clearly.

## Remaining extensions: implement only with an explicit contract

- Exact MATSim `pt` conversion needs a transit schedule plus facility/line/route correspondence to generated HTC stop actors; parse source PT routing and produce typed `TransitLeg` plus access/egress legs. Unknown references must fail. Existing replanning is an alternative scenario, not this mapping.
- Exact scheduled transit needs an HTC lifecycle adapter for stop-time adherence, terminal retirement, block/fleet reuse and boarding at the source stop. A Python input change alone cannot turn a circulating Bus into a finite GTFS trip.
- Rail geometry should follow ordered track paths and measured service assumptions. Current straight-line spacing and a 100 m floor are approximations. General platform/stop-area correspondence, branched lines and interchange modeling remain incomplete.
- OSM road access, mode-specific snapping, turn restrictions, map matching, capacity scaling and fractionally timed Person/PT support need separate design and acceptance criteria.
- Population semantics do not currently transfer full demographics, household vehicle sharing, MATSim utility scores or every custom transport mode.

Preserve these limitations in both README and generated reports until corresponding functionality has been implemented and validated.
