# Reusable bounded OSM import workflow

The importer prepares HTC scenarios from an OSM XML or `.osm.gz` extract, optionally selecting
a geographic area. It is available as a CLI/library and through the
[HTC View import workflow](../../htc-ui/docs/OSM_IMPORT_WORKFLOW.md). HTC executes the
resulting scenario. SimEDaPE acceleration is not used.

## Preview

Run from the platform root with the platform Python environment:

```bash
python tools/osm_scenario/importer.py preview path/to/source.osm \
  --bbox=-46.7342,-23.6334,-46.7317,-23.6295 \
  --max-ways 100 --cars 16 --duration 900 \
  --report /tmp/osm-preview.json --progress
```

Coordinates are `west,south,east,north` in geographic degrees. Use the `--bbox=...` form
for negative values. Omit bbox to inspect a source that is already a bounded extract.
PBF input is not supported by this implementation; provide XML or gzipped XML.

Selection retains **whole eligible ways intersecting the box**, including roads that cross
it without an interior source node. All their node coordinates and complete shapes are
retained. No synthetic cut points or clipped simulation links are introduced. Accordingly,
the reported output extent may extend outside the requested box; inspect it before publishing.

The regional file is streamed and coordinates indexed in a temporary on-disk SQLite database.
It is not loaded into one large in-memory DOM. The index is removed when the operation ends.
Large inputs require temporary disk space and two XML scans plus source fingerprinting;
preview and publish currently repeat this preparation rather than share a persistent index.

## Report contract

The JSON schema is `htc.osm-import.v1`. A valid preview has `status: ready`; a blocked one
has `status: invalid`. The report includes:

- Source file fingerprint and the selected source snapshot fingerprint.
- Options, selected way IDs, source-node count, requested box and actual extent.
- Directed link/node counts, weak connectivity components and source-way segmentation.
- `geometry_preview`: GeoJSON LineString features with stable logical link IDs, names,
  directions, lengths, speeds and original OSM tags for the preview map.
- Diagnostics with `level`, `code`, `entity` and `message`.
- `preview_sha256`, binding the input, selection, configuration and validation outcome.

Common diagnostics:

| Code | Meaning |
|---|---|
| `DEFAULT_SPEED` | Missing source speed; the bounded profile uses 30 km/h. Warning. |
| `LANE_PROFILE` | Source lane tags are retained but the executable proof profile uses one lane. Warning. |
| `ACCESS_EXCLUDED` | A private/no-access road was excluded from the car network. Warning. |
| `ACCESS_RULE_UNSUPPORTED` | Destination/customer or other permissions cannot be inferred for synthetic cars. Blocks publication. |
| `CONDITIONAL_RULE_UNSUPPORTED` | Selected road has a conditional rule that this profile cannot execute. Blocks publication. |
| `DIRECTIONAL_RULE_UNSUPPORTED` | Selected road needs unsupported per-direction/vehicle-specific conversion. Blocks publication. |
| `TURN_RESTRICTION_UNSUPPORTED` | A restriction relation touches a selected road. Blocks publication; unrelated restrictions do not. |
| `INCOMPLETE_WAY` | A supported road has missing source-node coordinates. Blocks publication. |
| `SELECTION_TOO_LARGE` | More eligible roads intersect the area than the requested limit. The partial selection cannot be published. |
| `DISCONNECTED_NETWORK` | Selection has disconnected components. Refine the area or explicitly use `--allow-disconnected`. |
| `UNSUPPORTED_SPEED`, `UNSUPPORTED_ONEWAY`, `SHORT_OR_LOOP_LINK` | Existing bounded-profile limits, attributed to the affected way. |
| `PREVIEW_CHANGED` | The publish preparation differs from the preview fingerprint supplied by the caller. |

Unsupported highway classes are counted separately under `excluded` with a `source_highway:`
prefix. These counts concern the source file, not only the requested area. The selected network
contains the supported urban car-road profile, not every road or mode visible in the source.

## Publish

Choose a **new revision directory** beneath a configured scenario location:

```bash
python tools/osm_scenario/importer.py publish path/to/source.osm \
  /path/to/scenarios/osm-neighbourhood-v1 \
  --bbox=-46.7342,-23.6334,-46.7317,-23.6295 \
  --max-ways 100 --cars 16 --duration 900 \
  --expect-preview THE_PREVIEW_SHA256 \
  --report /tmp/osm-publish.json --progress
```

`--expect-preview` is an optional concurrency/reproducibility check. Use the same options as
the preview. The source is hashed before and after selection so a changing extract cannot
silently produce a mixed snapshot. Invalid imports return status `invalid`, exit code 2 and
no scenario revision. Report files requested with `--report` are still written.

A valid package is compiled with the existing canonical writer in staging. Its road/source
mapping is validated, absolute runtime paths are rebased to the final revision, and the
directory is promoted atomically with Linux `renameat2(RENAME_NOREPLACE)`. An existing revision,
including one created by a competing publisher, is never replaced. Temporary data is cleaned
on failure. Preview is portable Python/SQLite; atomic publication currently requires Linux
with `renameat2` support.

Published output includes `simulation.json`, actor JSON, city map, retained road/source files,
`scenario_metadata.json`, `GENERATION_REPORT.md` and `import_report.json`. It can be discovered
by the existing scenario catalogue without adding a new endpoint or rewriting a location.
Published source metadata retains both the original regional fingerprint and selected snapshot.

## Supported profile and limits

Currently: 1–100 supported urban road ways; fixed-route synthetic cars; conservative MICRO
execution; one-second ticks; numeric km/h or mph speeds; ordinary/reverse one-way rules;
non-loop links at least 10 m long. The old profile name's “standalone car” means independently
scheduled cars, not the standalone map mode: these packages are OSM-backed.

Shared OSM node IDs define topology. Coincident geometry with different node IDs does not
create a junction, which preserves the separation of a bridge crossing in the regression test.
This is not complete grade-separated traffic-model acceptance. Conditional access, turn
restrictions, general roundabout/closed-link handling, arbitrary lane profiles, observed demand,
signals inferred from OSM, PBF ingestion and full-city execution remain outside this profile.
The importer reports limitations rather than claiming to implement those behaviors.

## Library seam for HTC View

```python
from tools.osm_scenario.importer import Options, preview, publish

options = Options(bbox=(-46.7342, -23.6334, -46.7317, -23.6295))
report = preview(source_file, options)
if report["status"] == "ready":
    published = publish(source_file, new_revision_dir, options,
                        expected_preview=report["preview_sha256"])
```

The UI/service owns authorized source locations, revision names and scheduling. This library
does not expose arbitrary filesystem paths over HTTP. The implemented screen displays diagnostics
and GeoJSON, then publishes a revision and navigates to the existing full-HTC run workflow.
Its API resolves paths through the locations registry and runs long operations as managed jobs.

Coordination with Claude is recorded in [AGENT_COORDINATION.md](../../AGENT_COORDINATION.md).

## Verification

```bash
python -m unittest discover -s tools/osm_scenario/tests -v
```

The suite covers crossing/whole-way selection, gzip, deterministic previews, access/rule
diagnostics, connected components, separate crossing identities, source changes, canonical
publication, stale previews, no-clobber races and cleanup after failed compilation.

The first regional preview examined 18,177,123 nodes, 2,722,395 ways and 32,206 relations
from the local São Paulo XML extract. The requested neighbourhood selected 11 ways,
34 directed links, 17 simulation nodes and one connected component. This establishes bounded
area preparation from a larger file; it does not establish city-scale simulator performance.

The same preview was atomically published as `default:osm-neighbourhood-v1` and executed
through the existing HTC API. Run `089d73f5d13d456485c939bb14093dee` completed with 16
departed cars. The HTTP audit checked 36,720 measured values and preserved 24,036 missing
entries. Run-bound replay contained 34 links, 17 nodes and 30 traversal segments; its source
and output fingerprints passed. The map audit matched 630 normal/worker position samples,
checked curved `PathLayer` geometry and source-way picking metadata. Browser/WebGL
interaction was not tested by these scripts.

See [compact evidence](validation/neighbourhood-2026-11-01.json). The retained
[11-way source snapshot](examples/sao-paulo-neighbourhood.osm) can reproduce a bounded import
without scanning the regional file. Its new preview fingerprint will differ because the
input is now the selected snapshot; the original regional provenance is in the evidence.

```bash
python tools/osm_scenario/importer.py preview \
  tools/osm_scenario/examples/sao-paulo-neighbourhood.osm
python tools/osm_scenario/importer.py publish \
  tools/osm_scenario/examples/sao-paulo-neighbourhood.osm \
  /path/to/scenarios/osm-neighbourhood-v2
```

The HTC View import workflow is implemented; see its [API contract and validation](../../htc-ui/docs/OSM_IMPORT_WORKFLOW.md).
Its actual client completed preview/publication and started a real HTC run, with results/replay
and a standalone regression verified. Fresh browser acceptance and coordinated rollout to the
shared API remain next. Accuracy optimization remains after functional integration.
