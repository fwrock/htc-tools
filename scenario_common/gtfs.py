"""Reuse São Paulo's GTFS bus converter on a native MATSim road network."""

import importlib.util
import sys
from pathlib import Path

from pyproj import Transformer


def convert_gtfs(cfg, net, data_dir, ids):
    name = "htc_sao_paulo_gtfs_backend"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "sao-paulo" / "generate_hybrid_input.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    backend = sys.modules[name]

    class NetworkAdapter:
        # The legacy converter projects GTFS stops to UTM23S. Transform that
        # space into the native MATSim network's declared metre-based CRS.
        def __init__(self):
            self.project = Transformer.from_crs("EPSG:32723", cfg.population_crs, always_xy=True)

        def nearest_node(self, easting, northing):
            return net.nearest_node(*self.project.transform(easting, northing))

    converter = backend.GtfsTransitConverter(cfg, NetworkAdapter(), data_dir, ids)
    converter.convert()
    if not converter.stats.get("buses"):
        raise ValueError("GTFS selection produced no routed buses; check date, feeds and snapping")
    return converter
