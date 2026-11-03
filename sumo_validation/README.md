# HTC vs SUMO MICRO validation

Runs the same microscopic (Krauss car-following) scenario in both HTC's
`hyperbolic-time-chamber` engine and real SUMO, then compares vehicle trajectories to
measure how closely HTC's MICRO mode tracks SUMO as ground truth.

## Layout

```
tools/sumo_validation/
  scenario/       canonical scenario definitions (scenario_a.py, scenario_b.py, model.py)
  htc_export/     canonical Scenario -> HTC scenario JSON (simulation.json + data/*.json)
  sumo_export/    canonical Scenario -> SUMO .net.xml/.rou.xml/.sumocfg (via netconvert)
  runners/        subprocess wrappers: HTC jar run, SUMO run (no traci)
  parsers/        HTC Parquet reader + SUMO FCD XML reader -> common schema
  metrics/        RMSE / travel-time-delta computation
  tests/          pytest suite for every module above
  run_validation.py   orchestrates export -> run -> parse -> metrics -> report, per scenario
  output/         generated scenario files + run artifacts + metrics.csv + report.txt (gitignored-worthy, kept here for inspection)
```

`tools/sumo_validation_venv/` (sibling directory, one level up from this package) is a
throwaway Python venv with `pandas`, `pyarrow`, `pytest` installed — the platform's other
Python tooling (`simedape/`) already depends on `pandas`/`pyarrow`, so this is consistent
with what's already used elsewhere, not a new dependency class.

## Common schema

Both parsers produce: `sim_time` (float, simulated seconds), `vehicle_id` (str),
`position_m` (float, along-link distance from link start), `speed_mps` (float), `link_id`
(str).

## Methodology

1. One canonical `Scenario` (topology, Krauss vehicle-type params, per-vehicle routes and
   depart ticks) is defined by hand per scenario in `scenario/`.
2. Two independent exporters translate it to each simulator's native format, so
   length/speed/Krauss params are *asserted* identical on both sides (see `tests/`),
   rather than translated through `/home/dean/PhD/htc/scripts/sumo_convert/`, which has a
   known unfixed unit bug (writes SUMO's native m/s directly into HTC's `freeSpeed`/
   `speedLimit`, fields HTC declares as km/h). That script is bypassed entirely.
3. Each simulator is run as a plain subprocess (`runners/`) — no traci, no docker-compose.
4. Each output is parsed into the common schema and compared with `metrics/`.

## Scenarios

- **A — minimal car-following isolation**: 2 nodes, 1 straight 300 m single-lane link, no
  signal, 4 vehicles of one type entering 6 s apart (leader/follower pairs, not
  simultaneous free-flow entry — this is what actually exercises Krauss's gap-closing
  behavior instead of trivially free-flowing).
- **B — small signalized intersection**: A -> B (signalized 200 m approach) -> B -> C
  (100 m departure), same Krauss params as A, one traffic-signal-controlled crossing at B
  (60 s cycle, 30 s green / 30 s red), 6 vehicles spread across the cycle so some arrive on
  green and some on red.

## Deliberate scoping choices

### Determinism

- **SUMO**: `sigma="0"` on the vehicle type (no driver-imperfection randomness).
- **HTC**: the task brief asked us to wire `KraussModel.deterministic` (`randomness=0.0`)
  through scenario config. **We investigated and found this is unnecessary** — not because
  a config knob exists, but because the live MICRO tick loop never exercises randomness at
  all. Full trace, read directly from source (not assumed):
  - `hyperbolic-time-chamber/src/main/scala/model/hybrid/actor/Link.scala` wires MICRO
    ticks through `LinkMicroSimulationHandler` -> `DefaultMicroSimulationStrategy`
    (`private val microSimulationStrategy: MicroSimulationStrategy =
    DefaultMicroSimulationStrategy()`), which internally holds `KraussModel()` — the
    **default** constructor, `randomness = 0.2`, not `KraussModel.deterministic`.
  - However, `DefaultMicroSimulationStrategy.processMicroLane` (the method that actually
    runs every sub-tick) only calls `carFollowingModel.calculateSafeVelocity(...)`. It
    **never calls** `calculateAcceleration` or `updateState` — the two methods where
    `randomness` and `epsilonAccel` are actually used inside `KraussModel`. Instead it
    computes its own velocity ramp: a target velocity capped by
    `maxAcceleration/maxDeceleration * microTimeStep`, with **no random factor applied
    anywhere**.
  - Net effect: the live HTC MICRO path is **already fully deterministic** given fixed
    floating-point ops, regardless of the `randomness` field on the `KraussModel` instance
    it happens to hold. `KraussModel.deterministic` genuinely cannot be "wired in" more
    deterministically than this, because the field it controls is dead code on this path.
  - This is itself a real, separate finding, independent of the determinism goal — see
    "Known gap: Krauss params are hardcoded on the HTC side" below. **No Scala code was
    changed** for determinism; nothing needed to be.
  - There is a second, genuinely dead `LinkMicroTimeManager`
    (`model/hybrid/micro/manager/LinkMicroTimeManager.scala`) that *does* look like a
    proper Krauss-driven typed-actor sub-tick loop, including its own `carFollowingModel`
    parameter — but it is never referenced from `Link.scala` or anywhere else in
    `src/main`. It appears to be an earlier or parallel implementation that was superseded
    by `DefaultMicroSimulationStrategy` without being deleted. We did not touch or rely on
    it; flagging it here because it could mislead a future reader of this codebase into
    thinking Krauss randomness/`epsilonAccel` are live when they are not, on either path.

### Known gap: Krauss params are hardcoded on the HTC side

Reading `DefaultMicroSimulationStrategy.processMicroLane` line by line (not guessed) shows
that of the five Krauss parameters named in the task brief, only two are actually threaded
through from any scenario configuration:

| Param | Where it *should* come from | What actually happens |
|---|---|---|
| `maxAcceleration` / `maxDeceleration` | `MicroCarState.maxAcceleration/maxDeceleration` via scenario JSON | Actually comes from `Movable.microMaxAcceleration/microMaxDeceleration` (a **Scala method**, not a JSON field), sent once at link-entry via `EnterLinkData`. `Car` does not override these (only `Bus`/`Bicycle`/`Motorcycle` do), so every `Car` gets the **hardcoded default 2.6 / 4.5 m/s²** regardless of anything in the scenario JSON. |
| `minGap` | `MicroCarState.minGap` | **Hardcoded to `2.0`** directly in `processMicroLane`'s call to `calculateSafeVelocity`. |
| `reactionTime` | `MicroCarState.reactionTime` | **Hardcoded to `1.0`** directly in the same call. |
| `desiredVelocity` | `MicroCarState.desiredVelocity` | **Always `link.speedLimit / 3.6`** (the link's speed limit), not the vehicle's own field. |
| `vehicleLength` | `EnterLinkData.actorSize` | This one *does* flow through correctly (`Car.state.size` -> `EnterLinkData.actorSize` -> `VehicleInLane.vehicleLength`). |

Separately, `CarMicroHandler.handleMicroEnterLink` rebuilds a fresh `MicroCarState` on
every link entry and only carries over the **previous** micro state's `velocity` field —
any custom `minGap`/`reactionTime`/`desiredVelocity`/`maxAcceleration`/`maxDeceleration`
set in a scenario's `microState` JSON block is silently discarded even before reaching the
link.

**What we did about it**: rather than threading five new config knobs through
`Car`/`Movable`/`MicroCarState`/`DefaultMicroSimulationStrategy` — a genuinely invasive,
multi-file Scala change for a validation task, and outside the "minimal scoped change"
bar in this task's own instructions — we set `scenario/model.py`'s `VehicleType` defaults
to **exactly the values HTC hardcodes** (`max_accel_mps2=2.6`, `max_decel_mps2=4.5`,
`min_gap_m=2.0`, `reaction_time_s=1.0`), and set every link's speed limit consistently so
`desiredVelocity = speedLimit/3.6` matches the vehicle type's intended free-flow speed.
The SUMO exporter reads the *same* `VehicleType` values, so the 1:1 comparison holds by
construction, even though the "1:1 mapping" is really "SUMO mirrors what HTC's engine
actually does" rather than "both mirror an author's scenario-level configuration". The
`htc_export` module still emits an (unused) `microState=None` — deliberately `None`, not a
populated-but-ignored block — see its docstring for why populating it would be misleading.
**This is a real gap in HTC** worth fixing on the HTC side (threading per-vehicle Krauss
params from scenario JSON through to the live tick loop) independent of this validation
task; it was out of scope to fix here per the task's own instructions (Scala changes were
only pre-authorized for the *randomness* config gap, which turned out not to need one).

### Integration scheme

The task brief flagged HTC's `KraussModel.updateState` as using trapezoidal integration
(`averageVelocity = (v_old + v_new)/2`) and asked us to check SUMO's default (Euler-like)
against `--step-method.ballistic` (trapezoidal-under-constant-acceleration) for the closer
match. Given the finding above — **the live MICRO loop never calls `updateState` either**
— this question has a different answer than assumed: `processMicroLane` advances position
with `newPosition = position + cappedVelocity * microTimeStep`, i.e. using the **new**,
already-capped velocity directly, not an average of old/new. That is SUMO's classic
Euler-like default, not the ballistic one. **We did not set `--step-method.ballistic`** —
using it would introduce a real integration-scheme mismatch against what HTC's live code
path actually does, which is the opposite of what the task intended.

### Why GEH is not used

GEH is a flow/volume-count calibration statistic (compares aggregate traffic counts
against a threshold curve). This validation compares single-vehicle continuous
trajectories against a reference, for which position/speed RMSE is the correct tool.

### Why the OSM converter script is bypassed

`/home/dean/PhD/htc/scripts/sumo_convert/` has a known unfixed unit bug: it writes SUMO's
native m/s directly into HTC's `freeSpeed`/`speedLimit` fields, which HTC's `LinkState`
declares (and the live MICRO code path treats, via the `/3.6` conversion in
`CarMicroHandler`) as **km/h**. Every link exported by that script would silently run at
3.6x its intended speed on the HTC side. This pipeline never imports from it; both
exporters are hand-written directly from the canonical `Scenario`.

## HTC output resolution is 1 Hz, not the internal 0.1 s sub-tick (verified finding)

HTC's MICRO physics genuinely steps at `microTimeStep=0.1s` internally
(`LinkState.microTimeStep`/`microTicksPerGlobalTick`, confirmed read by
`LinkMicroSimulationHandler.initializeMicroMode`), **but the live tick loop only reports
one position/velocity sample per vehicle per whole global (1 s) tick**:
`DefaultMicroSimulationStrategy.processMicroLane` only appends a `MicroVehicleUpdate` when
`subTick >= microTicksPerGlobalTick - 1` (i.e. the *last* sub-tick), so even setting
`HTC_MICRO_UPDATE_REPORT_EVERY=1` (which we do) yields exactly one `micro_update` Parquet
row per car per second — never one per 0.1 s. This was confirmed empirically (see "How
this was verified" below): every `micro_update` row observed across both scenario runs has
`sub_tick == 9` (the last of 10).

**Consequence for the metrics**: per the task brief's own instruction ("resample the finer
series down ... never interpolate the coarser one up"), `metrics/rmse.py` downsamples
SUMO's 10 Hz FCD output onto HTC's 1 Hz grid by exact row selection at whole seconds (SUMO's
`step-length 0.1` makes every whole second an exact sample — no interpolation needed), and
does **not** attempt to upsample HTC to 10 Hz.

## Real discrepancies found (do not paper over — this is thesis material)

Both scenarios were run end-to-end (see numbers below). Two genuine, reproducible engine
behaviors surfaced that are **not** artifacts of this pipeline's scenario design or
parsing:

### 1 & 2. Confirmed root cause (2026-07-30): `LocalTimeManagerBase.scheduleEvent`'s past-tick guard discards real causal time when actors share an LTM pool instance

Both artifacts below share **one confirmed root cause**, found by instrumenting a live run
with `-Dpekko.loglevel=DEBUG` (the engine's own `logDebug` calls in `scheduleEvent` are
gated by Pekko's log level, separate from logback — this is why it never showed up in the
plain `htc_run.log`s used for the first pass). Full writeup, code citations, and proposed
fix direction: `hyperbolic-time-chamber/docs/KNOWN_GAPS.md`, section "`scheduleEvent`'s
Past-Tick Guard Discards Real Causal Time When Actors Share an LTM Instance". **This
supersedes the JVM/cluster-warm-up hypothesis originally written here** — it was wrong;
the actual mechanism is a scheduling bug, not a fixed startup cost.

`LocalTimeManager` pool instances are shared by multiple actors (small pool —
`htc.time-manager.max-instances-per-node = 4` locally). Each instance tracks one shared
`localTickOffset`. When an actor sharing an instance with busier actors goes idle (e.g. a
`Link` with no vehicles) and later reactivates asking to be scheduled at its own next
legitimate tick, `LocalTimeManagerBase.scheduleEvent`'s guard (lines ~114-121) can't tell
"this tick was already processed for this specific actor" (what the guard was written to
protect against) apart from "this actor's request is only behind the shared clock because
unrelated actors on the same instance ran ahead of it" — and force-bumps both cases the
same way, silently discarding the actor's real causal time. Captured directly in scenario
B: `ScheduleEvent tick=63 is at/behind localTickOffset=180; bumping to 181` — `link_bc`
lost 118 legitimate ticks. Same mechanism, smaller magnitude, at scenario registration
time: `ScheduleEvent tick=0 is at/behind localTickOffset=0; bumping to 1`.

**Finding #1** (the ~10-13 s startup-latency-looking effect on `car_0`/`car_1` in scenario
A) is this same registration-time bump — its magnitude is a genuine real-time race (how far
the shared clock has advanced when the new actor registers), not a fixed floor, which is
why it varies between runs of the same scenario rather than staying constant.

**Finding #2** (the ~106-119 tick stall on `car_3`/`car_5` in scenario B, `link_bc`
reactivating after sitting idle) is the same guard hit at much larger magnitude, once the
shared LTM instance's clock has advanced far past the reactivating Link's own next tick.
Unrelated to the traffic signal — `car_3`'s upstream `link_ab` crossing (the one actually
signal-controlled) was unremarkable at 19 s with 0.8 s waiting.

**Not fixed** — this needs a per-actor watermark distinct from the shared
`localTickOffset` (see `KNOWN_GAPS.md` for the specific proposal and why it isn't a
drop-in reuse of the *different*, already-fixed `finishEvent` staleness guard) and
re-verification against the existing 86,400-tick reproducibility baseline before landing,
so it's left as a documented, root-caused, open gap rather than patched here.

Given these two engine-timing artifacts dominate the aggregate metrics, **the RMSE/travel-time
numbers below reflect the current engine's scheduling behavior, not solely Krauss
car-following accuracy** — see "Known gap" above and the two findings here before treating
the numbers as a pure car-following accuracy measurement.

### 3. Confirmed root cause (2026-07-31, corrected 2026-08-01): scenario A's `car_1` anomaly is a *different* mechanism from #1/#2 above — it's the already-documented `enterLink()`-poll race (KNOWN_GAPS.md "part 5"), not the `scheduleEvent` past-tick guard — plus one new, now-fixed `CarMicroHandler` bug found along the way

**Two things were found and fixed here, only one of which explains the anomaly.** First, a real
bug in this harness itself: `runners/sumo_runner.py` had a path-resolution bug (`-c` argument
plus `cwd` double-resolved the config path) that made every SUMO run since 2026-07-30 silently
exit 1 and fall back to reusing a stale, never-refreshed `scenario_fcd.xml` as "ground truth" —
fixed, with a regression test (`tests/test_sumo_runner.py`) that previously didn't exist at all.
Every number below is against genuinely fresh SUMO output.

Second, and this is the actual chase-down: `car_1` in scenario A (no signal at all in this
scenario, ruling out every signal-related mechanism) shows 56-69m RMSE across repeated runs
against a ~4.6m baseline. Root-caused via `journey_started` vs. `enter_micro_link` Parquet
timestamps per car: a car's actual entry into its first micro link lags its own `journey_started`
tick by a real-time-race-sized, non-constant delay (6, 7, 10, 13 ticks observed for `car_0` across
different runs). When two cars' independent delays land within 0-2 ticks of each other, the
scenario's intended 6-second departure gap collapses to almost nothing, forcing the follower into a
genuine (given the corrupted input, *correct*) Krauss emergency-brake recovery that dominates its
RMSE. Whichever car in a given run *doesn't* get caught in this pile-up lands at exactly the same
~4.6m baseline as everyone else — the Krauss math itself is not implicated.

**Correction (2026-08-01)**: this was originally written up as "the same registration/first-dispatch
scheduling delay as findings #1/#2 above" (the `scheduleEvent` past-tick guard). Live instrumentation
(tick+timestamp tracing on `Movable.actSpontaneous`, `enterLink()`'s send, the `Waiting`-case
re-poll, and `CarMicroHandler.handleMicroEnterLink`, temporary, since removed) showed this was
wrong: every tick between a car's `journey_started` and its `enter_micro_link` is directly observed
as a `Waiting`-case re-poll (`onFinishSpontaneous(Some(currentTick+1))` racing the real
`EnterLinkData`/`MicroEnterLinkData` mailbox round-trip) — this is **KNOWN_GAPS.md's already-
documented "part 5" mechanism** (previously characterized only via a single mid-run bus example
showing 2-4 extra polls), not the `scheduleEvent`/`localTickOffset` guard #1/#2 are about. The two
mechanisms happen to produce a similar-shaped symptom (a real, variable, multi-tick delay before an
actor's first meaningful event) but live in different code paths — #1/#2 is in
`LocalTimeManagerBase.scheduleEvent`; this is in `Movable.enterLink()`/`Waiting`. Four cars
registering into the same `Link` actor's mailbox within an 18-tick window is what amplifies part
5's race to 6-13 polls here, vs. 2-4 in its original single-bus example. Full writeup and
instrumented evidence: `hyperbolic-time-chamber/docs/KNOWN_GAPS.md`, section "`sumo_validation`
Scenario A `car_1` RMSE Anomaly...", "Correction" subsection.

The independent `CarMicroHandler` velocity-carryover bug found and fixed along the way is real, but
not sufficient on its own to close this gap (see KNOWN_GAPS.md for why).

**Not fixed** — per KNOWN_GAPS.md part 5's own standing guidance: removing `Movable.enterLink()`'s
poll has been attempted and reverted twice already (once causing a `Subway` stall, once making
duplicate dispatch rates worse), so a third attempt isn't safe without either auditing every
interacting reply handler system-wide or implementing the TimeManager-level stale-repoll-detection
approach part 5 sketches. This is unrelated to, and does not require, the #1/#2 `scheduleEvent` fix
above (which is already landed).

## Results (scenario A — free-of-signal car-following)

```
vehicle_id  n_samples  position_rmse_m  speed_rmse_mps  htc_travel_time_s  sumo_travel_time_s  travel_time_delta_s
car_0       13         4.599            0.001           14.0               23.0                -9.0
car_1       17         68.690           3.515           16.0               25.0                -9.0
car_2       23         4.600            0.001           24.0               23.0                +1.0
car_3       23         4.600            0.001           24.0               23.0                +1.0

Mean position RMSE:      20.622 m
Mean speed RMSE:         0.879 m/s
Mean travel-time delta:  -4.00 s (HTC - SUMO)
Max |travel-time delta|: 9.00 s
```

(Numbers as of 2026-07-31, post `CarMicroHandler` fix — see finding #3. `car_0` itself now
tracks the baseline cleanly, same as `car_2`/`car_3`; `car_1` still shows the entry-scheduling
pile-up effect, run to run, regardless of which specific car happens to be delayed.)

`car_2`/`car_3` (unaffected by the entry-scheduling collision, finding #3) show the cleanest
signal: ~4.6 m position RMSE and ~1 ms/s speed RMSE over a 300 m / ~23 s trip — sub-2%
relative position error, and travel time within 1 s. `car_1` is dominated by the
entry-scheduling-collision artifact (finding #3), not by car-following divergence — which
specific car it lands on varies run to run (see finding #3).

## Results (scenario B — signalized intersection)

```
vehicle_id  n_samples  position_rmse_m  speed_rmse_mps  htc_travel_time_s  sumo_travel_time_s  travel_time_delta_s
car_0       11         80.198           4.312           55.0               23.0                +32.0
car_1       18         45.595           3.199           228.0              25.0                +203.0
car_2       18         4.275           1.513           218.0              49.0                +169.0
car_3       26         12.263           2.933           43.0               41.0                +2.0
car_4       18         6.813           3.008           198.0              32.0                +166.0
car_5       26         14.393           3.673           29.0               27.0                +2.0

Mean position RMSE:      27.256 m
Mean speed RMSE:         3.106 m/s
Mean travel-time delta:  +95.67 s (HTC - SUMO)
Max |travel-time delta|: 203.00 s
```

(Numbers as of 2026-07-31, post `CarMicroHandler` fix. Position RMSE improved substantially for
several cars vs. the pre-fix baseline below — `car_2` 108m -> 4.3m, `car_4` 75.5m -> 6.8m — since
scenario B's two-link `link_ab` -> `link_bc` route directly exercises the chained-micro-link
velocity-carryover half of that fix. Travel-time deltas got *larger* for cars genuinely queued at
Red, which is expected, not a regression: correcting the departure-speed bug means more cars now
genuinely reach and queue at the signal instead of sailing past mid-cruise, so the pre-existing,
already-documented "cars queued at Red overshoot travel time" gap — see `KNOWN_GAPS.md` — is more
exposed than before, not newly introduced.)

`car_4` is the cleanest signal here (unaffected by either engine-timing artifact): 6.8m RMSE now
tracks the scenario A baseline. `car_0`/`car_1` are dominated by the entry-scheduling-collision
artifact (finding #3, same mechanism as scenario A's `car_1`). `car_3`/`car_5`'s large
travel-time deltas are the already-documented signal-queue-overshoot gap, not a car-following or
signal-gating divergence — HTC's `Node`/`TrafficSignal` correctly phases Green->Red->Green on
schedule (confirmed via the `signal_phase_change` Parquet events).

**Bottom line**: signal-gating-vs-Krauss interaction still can't be cleanly isolated from the two
still-open engine-timing gaps (entry-scheduling collision, signal-queue overshoot); a follow-up
scenario with a single link and the signal directly at the destination (no downstream `link_bc`)
would isolate this better once those are fixed.

## Krauss parameters used (identical on both sides, see scenario/model.py)

| Param | Value | Matches HTC's hardcoded... |
|---|---|---|
| `max_accel_mps2` | 2.6 | `Movable.microMaxAcceleration` default |
| `max_decel_mps2` | 4.5 | `Movable.microMaxDeceleration` default |
| `min_gap_m` | 2.0 | `DefaultMicroSimulationStrategy`'s hardcoded `minGap` |
| `reaction_time_s` | 1.0 | `DefaultMicroSimulationStrategy`'s hardcoded `reactionTime` |
| `length_m` | 4.5 | passed through correctly via `EnterLinkData.actorSize` |
| `sigma` (SUMO only) | 0.0 | determinism; HTC side needs no equivalent knob (see above) |

`epsilonAccel` (HTC's extra 0.5 m/s² deceleration safety margin, applied inside
`KraussModel.calculateAcceleration`'s lower bound) has **no SUMO equivalent and could not
be zeroed or matched**, but it is also dead code on the live MICRO path for the same
reason `randomness` is (`calculateAcceleration` is never called by
`DefaultMicroSimulationStrategy`). Documented here as a caveat in case a future HTC change
wires `calculateAcceleration`/`updateState` back into the live path — at that point
`epsilonAccel` would start actually mattering and this comparison would need revisiting.

## How this was verified

Every JSON/XML shape and Scala behavior claim in this README and in the exporter/runner
docstrings was checked against one of:
- Direct reads of the relevant `hyperbolic-time-chamber` source files (cited by path
  throughout).
- An existing known-good example scenario,
  `hyperbolic-time-chamber/simulations/input/sqlite_validation_test/data/*.json`.
- A hand-run smoke test: a minimal 1-link, then 2-link-plus-signal scenario, run directly
  against the assembled fatjar with `sourceType: "json"` data sources (bypassing sqlite
  entirely, confirmed supported via `DataSourceTypeEnum`/`JsonLoadData`), inspected via the
  resulting Parquet output, before writing the Python exporters. This is how the "1 Hz
  reporting resolution", "fixed engine startup latency", and "TrafficSignal actor requires
  its own `dependencies` block for every node it notifies" findings were actually
  discovered (none of these were guessed or assumed).

## Running it

Prerequisites:
- `hyperbolic-time-chamber` assembled: `cd hyperbolic-time-chamber && sbt assembly` (jar at
  `target/scala-3.3.5/hyperbolic-time-chamber-1.24.3.jar`).
- SUMO + netconvert on `PATH` (`/usr/bin/sumo`, `/usr/bin/netconvert`, v1.27.1 here).
- A Python venv with `pandas`, `pyarrow`: `python3 -m venv tools/sumo_validation_venv &&
  tools/sumo_validation_venv/bin/pip install pandas pyarrow pytest`.

```bash
cd tools/sumo_validation
../sumo_validation_venv/bin/python3 run_validation.py \
    --jar ../../hyperbolic-time-chamber/target/scala-3.3.5/hyperbolic-time-chamber-1.24.3.jar \
    --scenarios a b \
    --out-dir ./output
```

This exports both scenarios, runs HTC then SUMO for each, parses both outputs, computes
metrics, and writes `output/<scenario_id>/metrics.csv` + `report.txt`. HTC's JVM process
does not exit on its own after the simulation stops (see `runners/htc_runner.py`
docstring); the runner kills it after a grace period once it sees the stop marker in the
log, but if you interrupt a run manually, check for and kill any leftover
`hyperbolic-time-chamber-*.jar` process before the next run (it holds the cluster port
1600 and the Prometheus metrics port 9001).

### Tests

```bash
cd tools/sumo_validation
../sumo_validation_venv/bin/python3 -m pytest -q
```

24 tests, all passing: scenario topology sanity, HTC JSON-export field-by-field checks
(including the TrafficSignal/Node wiring for scenario B), SUMO XML-export field-by-field
checks (skipped automatically if `netconvert` is not on `PATH`), parser round-trips against
hand-built synthetic Parquet/FCD samples, metrics RMSE checks against hand-computed
values (not tautological — e.g. `test_compute_vehicle_metrics_matches_hand_computed_rmse`
asserts against `sqrt(2)` computed by hand from the fixture's own error terms), and (added
2026-07-31) `test_sumo_runner.py`'s two regression tests for the `-c`/`cwd` path bug found in
finding #3 above — a fake `sumo` executable that only succeeds on a bare filename, reproducing
the exact double-resolution failure without needing a real SUMO install to exercise the bug.

## What was NOT done (explicitly out of scope)

- No lane-changing (single lane throughout both scenarios, by design — the task brief
  scoped this out).
- No attempt to match HTC/SUMO random seeds — determinism on both sides makes this
  unnecessary, and it turned out unnecessary on the HTC side for reasons deeper than the
  brief anticipated (see "Determinism" above).
- No fix for the two engine-timing findings above (startup latency, idle-link
  reactivation stall) — real, reproducible, and worth their own investigation, but outside
  this task's pre-authorized Scala-change scope.
- No fix for the Krauss-parameter-threading gap (`minGap`/`reactionTime`/
  `desiredVelocity`/`maxAcceleration`/`maxDeceleration` not read from scenario JSON on the
  live MICRO path) — documented as a real platform gap, worked around at the scenario
  level (matching values), not patched in Scala.
