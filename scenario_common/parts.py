"""Shared selection and transactional extension for the city generators."""

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

PARTS = {
    "network": ("nodes", "links"),
    "population": ("persons", "cars", "bikes", "motorcycles"),
    "transit": ("bus_stops", "bus_stations", "buses"),
    "rail": ("subway_stations", "rail_links"),
    "signals": ("traffic_signals",),
}


def add_part_arguments(parser):
    parser.add_argument("--parts", nargs="+", choices=PARTS,
                        help="Generate only these parts; fresh scenarios require network")
    parser.add_argument("--extend", action="store_true",
                        help="Add selected --parts to the existing output/scenario")
    parser.add_argument("--replace-parts", action="store_true",
                        help="With --extend, replace selected parts that already exist")


def selected(cfg, part):
    if cfg.parts is not None:
        return part in cfg.parts
    return part in ("network", "population") or getattr(cfg, "include_" + part)


def prepare(cfg):
    if not cfg.scenario_name or Path(cfg.scenario_name).name != cfg.scenario_name or cfg.scenario_name in (".", ".."):
        raise ValueError("scenario must be a single directory name")
    if cfg.parts is not None and getattr(cfg, "skip_network", False):
        raise ValueError("Use --extend with --parts instead of --skip-network")
    if cfg.extend and not cfg.parts:
        raise ValueError("--extend requires explicit --parts")
    if cfg.replace_parts and not cfg.extend:
        raise ValueError("--replace-parts requires --extend")
    if cfg.parts is not None:
        if not cfg.parts or set(cfg.parts) - PARTS.keys():
            raise ValueError("Invalid --parts selection")
        if cfg.extend and "network" in cfg.parts:
            raise ValueError("An existing network cannot be replaced; use a fresh scenario")
        if not cfg.extend and "network" not in cfg.parts:
            raise ValueError("A fresh scenario requires --parts network (or use --extend)")
        for part in ("transit", "rail", "signals"):
            setattr(cfg, "include_" + part, part in cfg.parts)


def extend_scenario(generator):
    """Build in a sibling copy, then publish with rollback on rename failure.

    Copies, never hardlinks: signal wiring mutates network actor files. The original
    directory is retained until the entire conversion and manifest merge succeed.
    """
    cfg = generator.cfg
    root = cfg.output_dir
    if root.is_symlink():
        raise ValueError("--extend requires a real scenario directory, not a symlink")
    old_manifest = json.loads((root / "simulation.json").read_text())
    old_config = json.loads((root / "effective_config.json").read_text())
    old_meta = json.loads((root / "scenario_metadata.json").read_text())
    if not (root / "data" / "id_registry.json").is_file():
        raise ValueError("--extend requires the original id_registry.json")
    prefixes = tuple(prefix for part in cfg.parts for prefix in PARTS[part])
    for prefix in prefixes:
        if not cfg.replace_parts and any((root / "data").glob(prefix + "_*.*")):
            raise FileExistsError(f"Part {prefix} already exists; use --replace-parts to regenerate it")

    # Global/network settings belong to the existing scenario. Part-specific options
    # (sample, fleet sizes, input paths, etc.) come from this invocation.
    inherited = {key: old_config[key] for key in (
        "scenario_name", "output_format", "docker_base", "duration", "start_real_time",
        "random_seed", "micro_ratio", "micro_strategy", "population_crs", "bbox",
        "micro_time_step", "micro_ticks_per_tick",
    ) if key in old_config and hasattr(cfg, key)}
    # São Paulo's population CRS is independent of its WGS84 OSM road source.
    if "population" in cfg.parts and hasattr(cfg, "bbox") and cfg.population_crs:
        inherited["population_crs"] = cfg.population_crs
    for part in ("transit", "rail", "signals"):
        inherited["include_" + part] = selected(cfg, part) or old_config.get("include_" + part, False)
    if "population" not in cfg.parts:
        for key, value in old_config.items():
            if key.startswith(("mc_", "driver_", "sample", "sampling_")) or key in (
                "population_size", "unsupported_mode", "declare_ownership",
                "enable_dynamic_mode_choice", "mode_choice_strategy", "input_format",
            ):
                inherited[key] = value
    if "transit" not in cfg.parts:
        for key in ("bus_source", "service_date", "transit_providers", "max_buses_per_route", "max_transit_routes",
                    "transit_default_headway"):
            if key in old_config:
                inherited[key] = old_config[key]
    for part, starts in (("rail", ("rail_", "train_", "max_trains_")),
                         ("signals", ("signal_",))):
        if part not in cfg.parts:
            inherited.update({key: value for key, value in old_config.items()
                              if key.startswith(starts)})
    # Rehydrate path-valued dataclass fields serialized by effective_config.json.
    if inherited.get("signal_osm_file"):
        inherited["signal_osm_file"] = Path(inherited["signal_osm_file"])

    with tempfile.TemporaryDirectory(prefix=f".{root.name}-extend-", dir=root.parent) as tmp:
        staged = Path(tmp) / root.name
        shutil.copytree(root, staged)
        data = staged / "data"
        # Restore SQLite to JSON only in the private working copy; the ordinary
        # generator converts the completed scenario back to its original format.
        if inherited["output_format"] == "sqlite":
            converter_dir = Path(__file__).resolve().parents[1] / "scenario-db-converter"
            sys.path.insert(0, str(converter_dir))
            from convert import actor_simulation_db_to_json, city_map_db_to_json
            for prefix in (p for values in PARTS.values() for p in values):
                for path in data.glob(prefix + "_*.db"):
                    json_path = path.with_suffix(".json")
                    actor_simulation_db_to_json(str(path), str(json_path))
                    # SQLite uses TEXT primary keys; the city generators use Long IDs.
                    actors = json.loads(json_path.read_text())
                    for actor in actors:
                        actor["id"] = int(actor["id"])
                    json_path.write_text(json.dumps(actors, separators=(",", ":")))
                    path.unlink()
            city_map_db_to_json(str(data / "city_map.db"), str(data / "city_map.json"))
            (data / "city_map.db").unlink()
        if "signals" in cfg.parts:
            from .traffic_signals import clear_signal_nodes
            controlled = set()
            for path in data.glob("traffic_signals_*.json"):
                for actor in json.loads(path.read_text()):
                    controlled.update(actor["data"]["content"].get("nodes", []))
            clear_signal_nodes(data, controlled)
        for prefix in prefixes:
            for path in data.glob(prefix + "_*.json"):
                path.unlink()
        old_indexes = {name: json.loads((data / name).read_text())
                       for name in ("transit_map.json", "transit_routes.json") if (data / name).exists()}
        generator.cfg = replace(cfg, output_dir=staged, **inherited)
        generator._extending = True
        generator.stats = dict(old_meta["statistics"])
        for key in list(generator.stats):
            if any(key == prefix or key.startswith(prefix + "_") or key == "files_" + prefix
                   for prefix in prefixes) or any(key.startswith(part + "_") for part in cfg.parts) or (
                       "population" in cfg.parts and key.startswith(("trips_", "matsim_", "activity_nodes_"))
                   ) or ("signals" in cfg.parts and key.startswith("signal_")):
                del generator.stats[key]
        try:
            generator.run()
            if inherited["output_format"] == "sqlite":
                # Keep all untouched databases byte-for-byte, including their optional
                # columns. Signal generation is allowed to modify road node contents.
                for path in (root / "data").glob("*.db"):
                    changed = any(path.name.startswith(p + "_") for p in prefixes)
                    changed |= "signals" in cfg.parts and path.name.startswith("nodes_")
                    if not changed:
                        shutil.copyfile(path, data / path.name)
            for name, entries in old_indexes.items():
                retained = [entry for entry in entries if
                            ("rail" not in cfg.parts if entry.get("stopType") == "subway"
                             else "transit" not in cfg.parts)]
                if "rail" in cfg.parts or "transit" in cfg.parts:
                    new = json.loads((data / name).read_text()) if (data / name).exists() else []
                    (data / name).write_text(json.dumps(retained + new, separators=(",", ":")))
            new_manifest = json.loads((staged / "simulation.json").read_text())
            manifest = dict(old_manifest)
            if "population" in cfg.parts:
                manifest["enableDynamicModeChoice"] = new_manifest["enableDynamicModeChoice"]
            manifest["actorsDataSources"] = [source for source in old_manifest["actorsDataSources"]
                if not any(source["id"].startswith(p + "_") for p in prefixes)]
            manifest["actorsDataSources"] += [source for source in new_manifest["actorsDataSources"]
                if any(source["id"].startswith(p + "_") for p in prefixes)]
            for key in ("dynamicActorTypes", "postLoadRegistrationClasses"):
                manifest[key] = list(dict.fromkeys(old_manifest.get(key, []) + new_manifest.get(key, [])))
            for key in ("transitMapFile", "transitRoutesFile"):
                if key in new_manifest:
                    manifest[key] = new_manifest[key]
            (staged / "simulation.json").write_text(json.dumps(manifest, indent=2))
            effective = json.loads((staged / "effective_config.json").read_text())
            effective["output_dir"] = str(root)
            (staged / "effective_config.json").write_text(json.dumps(effective, indent=2))
            meta_path = staged / "scenario_metadata.json"
            meta = json.loads(meta_path.read_text())
            meta["target"]["directory"] = str(root)
            for name, key in (("transit_map.json", "transit_map_entries"),
                              ("transit_routes.json", "transit_route_entries")):
                if (data / name).exists():
                    meta["statistics"][key] = len(json.loads((data / name).read_text()))
            generator.stats.update(meta["statistics"])
            generator._write_report()
            meta["extensions"] = old_meta.get("extensions", []) + [{
                "parts": cfg.parts, "replaced": cfg.replace_parts,
                "previousConfiguration": old_config, "previousSource": old_meta.get("source"),
                "previousInputSnapshots": old_meta.get("inputSnapshots", []),
                "previousDownloadProvenance": old_meta.get("downloadProvenance", {}),
            }]
            meta_path.write_text(json.dumps(meta, indent=2))
            backup = Path(tmp) / "previous"
            root.rename(backup)
            try:
                staged.rename(root)
            except BaseException:
                backup.rename(root)
                raise
        finally:
            generator.cfg = cfg
            generator._extending = False
