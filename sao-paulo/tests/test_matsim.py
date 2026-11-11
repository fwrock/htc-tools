"""Native São Paulo MATSim CLI contract, independent of the OSM/EQASim path."""

import csv
import contextlib
import gzip
import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pyproj import Transformer

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))
from scenario_common import matsim

SCRIPT = TOOLS / "sao-paulo" / "generate_matsim_input.py"


def write_xml(path, body):
    with gzip.open(path, "wt") as stream:
        stream.write(body)


def fixtures(root):
    inp = root / "input"
    inp.mkdir()
    project = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
    x, y = project.transform(-46.65, -23.55)
    nodes = {"a": (x, y), "b": (x + 100, y), "c": (x + 200, y)}
    write_xml(inp / "sao_paulo_network.xml.gz", '<network><nodes>' + ''.join(
        f'<node id="{key}" x="{px}" y="{py}"/>' for key, (px, py) in nodes.items()) +
        '</nodes><links>' + ''.join(
            f'<link id="{a}{b}" from="{a}" to="{b}" length="100" freespeed="10" '
            'capacity="1000" permlanes="1" modes="car,bus,bike"/>'
            for a, b in (("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"))) + '</links></network>')
    write_xml(inp / "sao_paulo_population.xml.gz", f'''<population>
<person id="driver"><plan selected="yes">
<activity type="home" x="{x}" y="{y}" end_time="00:00:05"/><leg mode="car"/>
<activity type="work" x="{x + 200}" y="{y}" end_time="00:01:00"/><leg mode="car"/>
<activity type="home" x="{x}" y="{y}"/></plan></person>
<person id="rider"><plan selected="yes">
<activity type="home" x="{x}" y="{y}" end_time="00:00:05"/><leg mode="pt"/>
<activity type="work" x="{x + 200}" y="{y}"/></plan></person></population>''')
    lines = []
    for line, mode, vehicle in (("bus_line", "bus", "bus1"), ("metro_line", "subway", "metro1")):
        lines.append(f'''<transitLine id="{line}"><transitRoute id="{line}_route">
<transportMode>{mode}</transportMode><routeProfile>
<stop refId="s1" arrivalOffset="00:00:00" departureOffset="00:00:00"/>
<stop refId="s2" arrivalOffset="00:00:20" departureOffset="00:00:20"/>
</routeProfile><route/><departures>
<departure id="first" departureTime="00:00:10" vehicleRefId="{vehicle}"/>
<departure id="second" departureTime="00:01:10" vehicleRefId="{vehicle}"/>
</departures></transitRoute></transitLine>''')
    write_xml(inp / "sao_paulo_transit_schedule.xml.gz", f'''<transitSchedule><transitStops>
<stopFacility id="s1" x="{x}" y="{y}" name="A"/>
<stopFacility id="s2" x="{x + 200}" y="{y}" name="B"/>
</transitStops>{''.join(lines)}</transitSchedule>''')
    write_xml(inp / "sao_paulo_transit_vehicles.xml.gz", '''<vehicleDefinitions xmlns="http://www.matsim.org/files/dtd">
<vehicleType id="custom_bus"><capacity><seats persons="30"/><standingRoomInPersons persons="45"/></capacity><length meter="15"/></vehicleType>
<vehicleType id="custom_metro"><capacity seats="200" standingRoomInPersons="300"/></vehicleType>
<vehicle id="bus1" type="custom_bus"/><vehicle id="metro1" type="custom_metro"/>
</vehicleDefinitions>''')
    (inp / "sao_paulo_config.xml").write_text('''<config><module name="global">
<param name="coordinateSystem" value="EPSG:31983"/></module></config>''')
    (inp / "meta.json").write_text(json.dumps({"fixture": "São Paulo native MATSim"}))
    return inp


def gtfs_fixture(inp):
    feed = inp / "bus"
    feed.mkdir()
    inverse = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
    project = Transformer.from_crs("EPSG:4326", "EPSG:31983", always_xy=True)
    x, y = project.transform(-46.65, -23.55)
    lon2, lat2 = inverse.transform(x + 200, y)
    tables = {
        "agency": (["agency_id", "agency_name", "agency_url", "agency_timezone"],
                   [["agency", "Fixture", "https://example.org", "America/Sao_Paulo"]]),
        "routes": (["route_id", "route_short_name", "route_type"], [["r", "GTFS", "3"]]),
        "stops": (["stop_id", "stop_name", "stop_lat", "stop_lon"],
                  [["a", "A", -23.55, -46.65], ["b", "B", lat2, lon2]]),
        "trips": (["route_id", "service_id", "trip_id", "direction_id"],
                  [["r", "daily", "trip", "0"], ["r", "daily", "trip2", "0"]]),
        "calendar": (["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "start_date", "end_date"],
                     [["daily", 1, 1, 1, 1, 1, 1, 1, 20260101, 20261231]]),
        "stop_times": (["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                       [["trip", "00:00:10", "00:00:10", "a", 1], ["trip", "00:00:30", "00:00:30", "b", 2],
                        ["trip2", "00:01:10", "00:01:10", "a", 1], ["trip2", "00:01:30", "00:01:30", "b", 2]]),
    }
    for name, (fields, rows) in tables.items():
        with (feed / f"{name}.txt").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)


class NativeMatsimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inp = fixtures(self.root)
        self.out = self.root / "output" / "native"

    def run_cli(self, *options, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--input-dir", str(self.inp),
            "--output", str(self.root / "output"), "--scenario", "native", "--sample", "1",
            "--duration", "240", "--docker-base", str(self.root / "output"), *options],
            text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def actors(self, prefix):
        return [actor for path in sorted((self.out / "data").glob(prefix + "_*.json"))
                for actor in json.loads(path.read_text())]

    def test_native_xml_all_parts_metadata_and_coordinates(self):
        self.run_cli()
        manifest = json.loads((self.out / "simulation.json").read_text())
        self.assertIn("São Paulo", manifest["name"])
        self.assertNotIn("Toulouse", manifest["description"])
        self.assertEqual(manifest["dynamicActorTypes"], ["hybrid.actor.Subway"])
        self.assertEqual(len(self.actors("persons")), 2)
        self.assertEqual(len(self.actors("buses")), 2)
        self.assertTrue(self.actors("rail_links"))
        self.assertEqual({a["data"]["content"]["capacity"] for a in self.actors("buses")}, {75})
        self.assertEqual({a["data"]["content"]["size"] for a in self.actors("buses")}, {15})
        node = self.actors("nodes")[0]["data"]["content"]
        self.assertAlmostEqual(node["latitude"], -23.55, places=5)
        self.assertAlmostEqual(node["longitude"], -46.65, places=5)
        trains = [train for actor in self.actors("subway_stations")
                  for queue in actor["data"]["content"].get("subways", {}).values() for train in queue]
        self.assertTrue(trains)
        self.assertEqual({t["capacity"] for t in trains}, {500})
        all_actors = [a for prefix in ("nodes", "links", "persons", "cars", "buses", "bus_stops", "rail_links", "subway_stations")
                      for a in self.actors(prefix)]
        self.assertEqual(len({a["id"] for a in all_actors}), len(all_actors))
        meta = json.loads((self.out / "scenario_metadata.json").read_text())
        self.assertEqual(meta["source"]["scenarioMeta"]["fixture"], "São Paulo native MATSim")
        self.assertIn("global.coordinateSystem", meta["source"]["coordinateCrsSource"])
        for source in manifest["actorsDataSources"]:
            self.assertTrue(Path(source["dataSource"]["info"]["path"]).is_file())

    def test_atlantis_requires_explicit_crs_before_creating_output(self):
        config = self.inp / "sao_paulo_config.xml"
        config.write_text(config.read_text().replace("EPSG:31983", "Atlantis"))
        result = self.run_cli(success=False)
        self.assertIn("--population-crs", result.stderr)
        self.assertFalse(self.out.exists())
        self.run_cli("--population-crs", "EPSG:31983")

    def test_missing_config_requires_explicit_crs(self):
        (self.inp / "sao_paulo_config.xml").unlink()
        self.run_cli(success=False)
        self.run_cli("--population-crs", "EPSG:31983", "--parts", "network")

    def test_geographic_crs_is_rejected(self):
        result = self.run_cli("--population-crs", "EPSG:4326", success=False)
        self.assertIn("projected CRS in metres", result.stderr)
        self.assertFalse(self.out.exists())

    def test_parts_then_extend_without_original_network_or_config(self):
        self.run_cli("--parts", "network")
        original = (self.out / "data" / "nodes_0.json").read_bytes()
        (self.inp / "sao_paulo_network.xml.gz").unlink()
        (self.inp / "sao_paulo_config.xml").unlink()
        self.run_cli("--extend", "--parts", "population", "--unsupported-mode", "walk")
        self.run_cli("--extend", "--parts", "transit", "rail")
        self.assertEqual((self.out / "data" / "nodes_0.json").read_bytes(), original)
        self.assertTrue(self.actors("persons"))
        self.assertTrue(self.actors("buses"))
        self.assertTrue(self.actors("rail_links"))

    def test_optional_gtfs_buses_with_matsim_rail(self):
        gtfs_fixture(self.inp)
        self.run_cli("--bus-source", "gtfs", "--service-date", "2026-11-04", "--transit-providers", "bus")
        buses = self.actors("buses")
        self.assertEqual(len(buses), 2)
        self.assertTrue(buses[0]["data"]["content"]["label"].startswith("bus_"))
        self.assertTrue(self.actors("rail_links"))
        entries = json.loads((self.out / "data" / "transit_routes.json").read_text())
        self.assertEqual({entry["stopType"] for entry in entries}, {"bus", "subway"})

    def test_gtfs_only_needs_no_matsim_schedule(self):
        gtfs_fixture(self.inp)
        (self.inp / "sao_paulo_transit_schedule.xml.gz").unlink()
        (self.inp / "sao_paulo_transit_vehicles.xml.gz").unlink()
        self.run_cli("--parts", "network", "transit", "--bus-source", "gtfs",
                     "--service-date", "2026-11-04", "--transit-providers", "bus")
        self.assertEqual(len(self.actors("buses")), 2)

    def test_gtfs_requires_service_date(self):
        result = self.run_cli("--bus-source", "gtfs", success=False)
        self.assertIn("--service-date", result.stderr)
        self.assertFalse(self.out.exists())

    def test_switch_bus_source_preserves_rail_and_people(self):
        self.run_cli()
        before = {p.name: p.read_bytes() for p in (self.out / "data").glob("*.json")
                  if p.name.startswith(("rail_links_", "subway_stations_", "persons_", "cars_"))}
        gtfs_fixture(self.inp)
        self.run_cli("--extend", "--parts", "transit", "--replace-parts", "--bus-source", "gtfs",
                     "--service-date", "2026-11-04", "--transit-providers", "bus")
        for name, content in before.items():
            self.assertEqual((self.out / "data" / name).read_bytes(), content)
        self.assertTrue(all(a["data"]["content"]["label"].startswith("bus_") for a in self.actors("buses")))
        self.run_cli("--extend", "--parts", "population", "--replace-parts")
        effective = json.loads((self.out / "effective_config.json").read_text())
        self.assertEqual(effective["bus_source"], "gtfs")
        for name in ("transit_map.json", "transit_routes.json"):
            entries = json.loads((self.out / "data" / name).read_text())
            self.assertEqual({e["stopType"] for e in entries}, {"bus", "subway"})

    def test_native_sqlite_parts_and_extension(self):
        self.run_cli("--parts", "network", "--output-format", "sqlite")
        original = (self.out / "data" / "nodes_0.db").read_bytes()
        self.run_cli("--extend", "--parts", "population", "transit", "rail")
        self.assertEqual((self.out / "data" / "nodes_0.db").read_bytes(), original)
        self.assertTrue((self.out / "data" / "buses_0.db").is_file())
        manifest = json.loads((self.out / "simulation.json").read_text())
        for source in manifest["actorsDataSources"]:
            self.assertEqual(source["dataSource"]["sourceType"], "sqlite")
            self.assertTrue(Path(source["dataSource"]["info"]["path"]).is_file())

    def test_native_signals_project_to_network_crs(self):
        self.run_cli("--parts", "network")
        (self.inp / "signals.osm").write_text('<osm><node id="s" lat="-23.55" lon="-46.65">'
                                             '<tag k="highway" v="traffic_signals"/></node></osm>')
        self.run_cli("--extend", "--parts", "signals", "--signal-osm-file", "signals.osm")
        self.assertEqual(len(self.actors("traffic_signals")), 1)
        self.assertTrue(any(a["data"]["content"].get("approachConnections") for a in self.actors("nodes")))

    def test_programmatic_crs_is_used(self):
        cfg = matsim.Config(input_dir=self.inp, output_dir=self.out, scenario_name="native",
            city_name="São Paulo", source_prefix="sao_paulo", population_crs="EPSG:31983",
            network_file=self.inp / "sao_paulo_network.xml.gz", parts=["network"])
        with contextlib.redirect_stdout(io.StringIO()):
            matsim.HybridInputGenerator(cfg).run()
        self.assertAlmostEqual(self.actors("nodes")[0]["data"]["content"]["latitude"], -23.55, places=5)


if __name__ == "__main__":
    unittest.main()
