# OSM → HTC bounded scenario import

The reusable [preview/publish workflow](IMPORT_WORKFLOW.md) accepts XML/gzip extracts,
selects a geographic area, reports topology/rule limitations and atomically publishes a
validated HTC revision. Start there for imports beyond the original two-road fixture.
The CLI/library also backs the [HTC View import page and managed HTTP jobs](../../htc-ui/docs/OSM_IMPORT_WORKFLOW.md).
An [11-way neighbourhood import and real HTC/replay audit](IMPORT_WORKFLOW.md#verification)
passed; its retained source is `examples/sao-paulo-neighbourhood.osm`.

## Original compatibility fixture

This converter implements the first small OSM-backed integration proof. It is not a
general city importer. HTC executes the vehicles; HTC View renders the retained road
shapes and exposes their source metadata. SimEDaPE acceleration is not used.

The supplied extract contains the real São Paulo ways **4774379 (Rua Itapimirum)** and
**4774398 (Rua Janga)**. It was selected from the existing local
`tools/sao-paulo/input/sao_paulo.osm` file. Original way properties and source node coordinates
are retained in `examples/sao-paulo-two-roads.osm`; the output stores that source snapshot
and its SHA-256 fingerprint. It is not a claim about current street conditions.

Source data: © OpenStreetMap contributors, [ODbL](https://www.openstreetmap.org/copyright).
Keep this attribution and the saved source metadata with redistributed derived packages.

From the platform root, using the installed platform Python environment:

```bash
python tools/osm_scenario/convert.py \
  tools/osm_scenario/examples/sao-paulo-two-roads.osm \
  /tmp/my-htc-scenarios/osm-roads
```

The destination must be new. The converter reuses the existing HTC actor builders and the
canonical `scenario_output_writer` package writer. It produces Node/Link/Car JSON, city map,
simulation manifest, metadata/report, `data/road_network.json` and `data/road_source.osm`.
Intersections are created at shared OSM nodes; ordinary interior shape vertices are retained
as geometry rather than converted into artificial junctions. Two-way roads produce separate
directed links; split segments preserve the source way identity.

The proof profile supports 1–100 ordinary urban road ways, links at least 10 m long,
one-way/reverse direction and numeric km/h or mph speeds. It explicitly uses one MICRO lane,
synthetic fixed-route cars and a 30 km/h fallback when maxspeed is absent. Source tags are
preserved even when the bounded simulation profile uses different lane assumptions. Restriction
relations, unsupported road/speed rules and closed-loop links are rejected. Signal timing,
observed demand, full-city routing and general turn-restriction conversion are not implemented.

Tests:

```bash
python -m unittest discover -s tools/osm_scenario/tests -v
```

The [integration/geometry validation](../../htc-ui/docs/OSM_COMPATIBILITY_VALIDATION.md)
records the real HTC run and the independent-network regression.
