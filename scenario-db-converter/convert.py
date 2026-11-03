#!/usr/bin/env python3
"""Standalone converter between HTC scenario JSON inputs and equivalent SQLite databases.

Two independent conversions, matching the two schemas designed for the HTC simulator
(hyperbolic-time-chamber):

  actor-simulation   ActorSimulation records (person/car/bus/node/link/... input), consumed by
                      JsonLoadData/ProgressiveJsonLoadData. Typical files have ~50k agent records.
                      `startTick` is promoted out of the opaque `data.content` payload into an
                      indexed `start_tick` column so a progressive/windowed reader can query a
                      tick range directly instead of re-scanning the whole file, as the current
                      JSON path must.

  city-map           city_map.json, the road-network graph consumed by CityMapUtil. For a whole
                      city (e.g. São Paulo) this can be hundreds of thousands of nodes/edges.
                      Nodes/edges normalize into two relational tables; the optional nested
                      sidewalk configuration is preserved as JSON in an extra TEXT column. Pure load-time optimization — CityMapUtil
                      still loads the whole graph into a permanent in-memory singleton either way.

Both directions here are streamed record-by-record (via `ijson` for JSON reads, manual
incremental JSON writing for JSON writes) — at city-map scale, loading the whole file into a
Python list/dict first (e.g. via stdlib `json.load`) would just move the memory problem this
whole effort exists to avoid into the conversion tool itself. Memory use here stays bounded by
one row/batch at a time regardless of input size.

This script intentionally lives outside hyperbolic-time-chamber's build — it's a data-prep
utility, not part of the simulator runtime, and has no dependency on the Scala/Pekko toolchain.

Requires: ijson (pip install ijson). Everything else is Python stdlib (json, sqlite3).

Usage:
  python convert.py actor-simulation json-to-db  <input.json> <output.db>
  python convert.py actor-simulation db-to-json  <input.db>   <output.json>
  python convert.py city-map         json-to-db  <input.json> <output.db>
  python convert.py city-map         db-to-json  <input.db>   <output.json>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import ijson

BATCH_SIZE = 1000


# --------------------------------------------------------------------------------------
# actor-simulation
# --------------------------------------------------------------------------------------

ACTOR_SIMULATION_DDL = """
CREATE TABLE actor_simulation (
  seq                          INTEGER PRIMARY KEY AUTOINCREMENT,
  id                           TEXT NOT NULL,
  name                         TEXT,
  type_actor                   TEXT NOT NULL,
  creation_type                TEXT NOT NULL,
  start_tick                   INTEGER NOT NULL DEFAULT 0,
  pool_round_robin_pool        INTEGER,
  pool_total_instances         INTEGER,
  pool_max_instances_per_node  INTEGER,
  pool_allow_local_routes      INTEGER,
  pool_use_roles               TEXT,
  data_type                    TEXT NOT NULL,
  data_content                 TEXT NOT NULL,
  relationships                TEXT
);
"""

ACTOR_SIMULATION_INDEX_DDL = "CREATE INDEX idx_actor_simulation_start_tick ON actor_simulation(start_tick);"

DEFAULT_CREATION_TYPE = "LoadBalancedDistributed"
DEFAULT_POOL_CONFIGURATION = {
    "roundRobinPool": 0,
    "totalInstances": 100,
    "maxInstancesPerNode": 10,
    "allowLocalRoutes": True,
    "useRoles": None,
}


def _extract_start_tick(content) -> int:
    """Mirrors TickIndexUtil.extractStartTick's semantics: default 0 for actors without a
    startTick (infrastructure actors), read out of the opaque content payload."""
    if isinstance(content, dict):
        value = content.get("startTick")
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _actor_to_row(actor: dict) -> tuple:
    data = actor.get("data") or {}
    content = data.get("content")
    pool = actor.get("poolConfiguration") or DEFAULT_POOL_CONFIGURATION
    use_roles = pool.get("useRoles")
    # ActorSimulation.relationships is @JsonAlias("dependencies") — real scenario data (as produced
    # by scripts/scenario/generate_*.py) uses the legacy "dependencies" key, not "relationships".
    relationships = actor.get("relationships", actor.get("dependencies"))

    return (
        actor["id"],
        actor.get("name"),
        actor["typeActor"],
        actor.get("creationType", DEFAULT_CREATION_TYPE),
        _extract_start_tick(content),
        pool.get("roundRobinPool", 0),
        pool.get("totalInstances", 100),
        pool.get("maxInstancesPerNode", 10),
        1 if pool.get("allowLocalRoutes", True) else 0,
        json.dumps(use_roles) if use_roles is not None else None,
        data.get("dataType"),
        json.dumps(content),
        json.dumps(relationships) if relationships is not None else None,
    )


def actor_simulation_json_to_db(json_path: str, db_path: str) -> None:
    out = Path(db_path)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(ACTOR_SIMULATION_DDL)

        insert_sql = """
            INSERT INTO actor_simulation
              (id, name, type_actor, creation_type, start_tick,
               pool_round_robin_pool, pool_total_instances, pool_max_instances_per_node,
               pool_allow_local_routes, pool_use_roles, data_type, data_content, relationships)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        with open(json_path, "rb") as f:
            batch = []
            for actor in ijson.items(f, "item", use_float=True):
                batch.append(_actor_to_row(actor))
                if len(batch) >= BATCH_SIZE:
                    conn.executemany(insert_sql, batch)
                    conn.commit()
                    batch = []
            if batch:
                conn.executemany(insert_sql, batch)
                conn.commit()

        conn.execute(ACTOR_SIMULATION_INDEX_DDL)
        conn.commit()
    finally:
        conn.close()


def _row_to_actor_json(row: sqlite3.Row) -> str:
    use_roles = json.loads(row["pool_use_roles"]) if row["pool_use_roles"] else None
    relationships = json.loads(row["relationships"]) if row["relationships"] else None

    actor = {
        "id": row["id"],
        "name": row["name"],
        "typeActor": row["type_actor"],
        "creationType": row["creation_type"],
        "poolConfiguration": {
            "roundRobinPool": row["pool_round_robin_pool"],
            "totalInstances": row["pool_total_instances"],
            "maxInstancesPerNode": row["pool_max_instances_per_node"],
            "allowLocalRoutes": bool(row["pool_allow_local_routes"]),
            "useRoles": use_roles,
        },
        "data": {
            "dataType": row["data_type"],
            "content": json.loads(row["data_content"]),
        },
        "relationships": relationships,
    }
    return json.dumps(actor)


def actor_simulation_db_to_json(db_path: str, json_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute("SELECT * FROM actor_simulation ORDER BY seq")
        cursor.arraysize = BATCH_SIZE
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("[")
            first = True
            while True:
                rows = cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    if not first:
                        f.write(",")
                    first = False
                    f.write(_row_to_actor_json(row))
            f.write("]")
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# city-map
# --------------------------------------------------------------------------------------

CITY_MAP_DDL = """
CREATE TABLE city_map_meta (
  directed INTEGER NOT NULL
);
CREATE TABLE city_map_node (
  id          TEXT PRIMARY KEY,
  resource_id TEXT,
  class_type  TEXT,
  latitude    REAL NOT NULL,
  longitude   REAL NOT NULL
);
CREATE TABLE city_map_edge (
  id          TEXT PRIMARY KEY,
  resource_id TEXT,
  class_type  TEXT,
  length      REAL,
  source_id   TEXT NOT NULL REFERENCES city_map_node(id),
  target_id   TEXT NOT NULL REFERENCES city_map_node(id),
  weight      REAL,
  sidewalk    TEXT
);
"""

CITY_MAP_INDEX_DDL = [
    "CREATE INDEX idx_city_map_edge_source ON city_map_edge(source_id);",
    "CREATE INDEX idx_city_map_edge_target ON city_map_edge(target_id);",
]


def _read_directed_flag(json_path: str) -> bool:
    """Lightweight streaming scan for the top-level `directed` key — does not materialize the
    (potentially huge) `nodes`/`edges` arrays it skips over along the way."""
    with open(json_path, "rb") as f:
        for value in ijson.items(f, "directed", use_float=True):
            return bool(value)
    return False


def city_map_json_to_db(json_path: str, db_path: str) -> None:
    out = Path(db_path)
    if out.exists():
        out.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(CITY_MAP_DDL)

        conn.execute(
            "INSERT INTO city_map_meta (directed) VALUES (?)",
            (1 if _read_directed_flag(json_path) else 0,),
        )
        conn.commit()

        # Separate streamed pass per array — keeps memory bounded to one batch at a time
        # regardless of how large `nodes`/`edges` are, at the cost of reading the file twice.
        with open(json_path, "rb") as f:
            batch = []
            for node in ijson.items(f, "nodes.item", use_float=True):
                batch.append(
                    (
                        node["id"],
                        node.get("resourceId"),
                        node.get("classType"),
                        node["latitude"],
                        node["longitude"],
                    )
                )
                if len(batch) >= BATCH_SIZE:
                    conn.executemany(
                        "INSERT INTO city_map_node (id, resource_id, class_type, latitude, longitude) "
                        "VALUES (?, ?, ?, ?, ?)",
                        batch,
                    )
                    conn.commit()
                    batch = []
            if batch:
                conn.executemany(
                    "INSERT INTO city_map_node (id, resource_id, class_type, latitude, longitude) "
                    "VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()

        with open(json_path, "rb") as f:
            batch = []
            for edge in ijson.items(f, "edges.item", use_float=True):
                label = edge["label"]
                batch.append(
                    (
                        label["id"],
                        label.get("resourceId"),
                        label.get("classType"),
                        label.get("length"),
                        edge["source_id"],
                        edge["target_id"],
                        edge.get("weight"),
                        json.dumps(label["sidewalk"]) if "sidewalk" in label else None,
                    )
                )
                if len(batch) >= BATCH_SIZE:
                    conn.executemany(
                        "INSERT INTO city_map_edge "
                        "(id, resource_id, class_type, length, source_id, target_id, weight, sidewalk) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        batch,
                    )
                    conn.commit()
                    batch = []
            if batch:
                conn.executemany(
                    "INSERT INTO city_map_edge "
                    "(id, resource_id, class_type, length, source_id, target_id, weight, sidewalk) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()

        for stmt in CITY_MAP_INDEX_DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def city_map_db_to_json(db_path: str, json_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        directed_row = conn.execute("SELECT directed FROM city_map_meta").fetchone()
        directed = bool(directed_row["directed"]) if directed_row else False

        with open(json_path, "w", encoding="utf-8") as f:
            f.write('{"nodes":[')
            node_cursor = conn.execute(
                "SELECT id, resource_id, class_type, latitude, longitude FROM city_map_node"
            )
            node_cursor.arraysize = BATCH_SIZE
            first = True
            while True:
                rows = node_cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    if not first:
                        f.write(",")
                    first = False
                    f.write(
                        json.dumps(
                            {
                                "id": row["id"],
                                "resourceId": row["resource_id"],
                                "classType": row["class_type"],
                                "latitude": row["latitude"],
                                "longitude": row["longitude"],
                            }
                        )
                    )

            f.write('],"edges":[')
            edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(city_map_edge)")}
            sidewalk_column = "sidewalk" if "sidewalk" in edge_columns else "NULL"
            edge_cursor = conn.execute(
                f"SELECT id, resource_id, class_type, length, source_id, target_id, weight, {sidewalk_column} AS sidewalk_config "
                "FROM city_map_edge"
            )
            edge_cursor.arraysize = BATCH_SIZE
            first = True
            while True:
                rows = edge_cursor.fetchmany(BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    if not first:
                        f.write(",")
                    first = False
                    f.write(
                        json.dumps(
                            {
                                "source_id": row["source_id"],
                                "target_id": row["target_id"],
                                "weight": row["weight"],
                                "label": {
                                    "id": row["id"],
                                    "resourceId": row["resource_id"],
                                    "classType": row["class_type"],
                                    "length": row["length"],
                                    **({"sidewalk": json.loads(row["sidewalk_config"])} if row["sidewalk_config"] is not None else {}),
                                },
                            }
                        )
                    )
            directed_literal = "true" if directed else "false"
            f.write(f'],"directed":{directed_literal}}}')
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

CONVERTERS = {
    ("actor-simulation", "json-to-db"): actor_simulation_json_to_db,
    ("actor-simulation", "db-to-json"): actor_simulation_db_to_json,
    ("city-map", "json-to-db"): city_map_json_to_db,
    ("city-map", "db-to-json"): city_map_db_to_json,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("schema", choices=["actor-simulation", "city-map"])
    parser.add_argument("direction", choices=["json-to-db", "db-to-json"])
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()

    convert = CONVERTERS[(args.schema, args.direction)]
    convert(args.input, args.output)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    sys.exit(main())
