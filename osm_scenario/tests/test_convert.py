import json
from pathlib import Path
import runpy
import tempfile
import unittest

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "convert.py"))
SOURCE = Path(__file__).resolve().parents[1] / "examples/sao-paulo-two-roads.osm"


class ConverterTests(unittest.TestCase):
    def test_real_fixture_retains_bends_and_directional_way_segments(self):
        _, roads, endpoints = MODULE["read_network"](SOURCE)
        self.assertEqual(len(roads), 6)
        self.assertEqual(len(endpoints), 4)
        itapimirum = [road for road in roads if road["osmWayId"] == "4774379"]
        self.assertEqual(len(itapimirum), 4)
        self.assertTrue(any(len(road["coordinates"]) > 30 for road in itapimirum))
        forward = next(road for road in roads if road["direction"] == "forward")
        reverse = next(road for road in roads if road["osmNodeIds"] == list(reversed(forward["osmNodeIds"])))
        self.assertEqual(reverse["coordinates"], list(reversed(forward["coordinates"])))
        self.assertAlmostEqual(reverse["length"], forward["length"])

    def test_oneway_reverse_mph_and_missing_nodes_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.osm"
            text = '<osm><node id="1" lon="2" lat="48"/><node id="2" lon="2.001" lat="48"/><way id="9"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/><tag k="oneway" v="-1"/><tag k="maxspeed" v="20 mph"/></way></osm>'
            path.write_text(text)
            _, roads, _ = MODULE["read_network"](path)
            self.assertEqual(len(roads), 1)
            self.assertEqual(roads[0]["direction"], "backward")
            self.assertEqual(roads[0]["from"], "2")
            self.assertAlmostEqual(roads[0]["speed"], 32.18688)
            path.write_text(text.replace('<node id="2" lon="2.001" lat="48"/>', ''))
            with self.assertRaisesRegex(ValueError, "referenced node"):
                MODULE["read_network"](path)

    def test_export_uses_canonical_package_and_validates_against_source(self):
        from pipeline.experiments.road_network import load_for_scenario
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "scenario"
            simulation = MODULE["convert"](SOURCE, output, cars=4, duration=900)
            network = load_for_scenario(simulation)
            self.assertEqual(len(network["links"]), 6)
            for name in ("simulation.json", "scenario_metadata.json", "GENERATION_REPORT.md", "data/road_network.json", "data/road_source.osm"):
                self.assertTrue((output / name).is_file(), name)
            cars = json.loads((output / "data/cars_0.json").read_text())
            self.assertEqual(len(cars), 4)
            self.assertTrue(all(car["data"]["content"]["bestRoute"] for car in cars))
            with self.assertRaisesRegex(ValueError, "immutable"):
                MODULE["convert"](SOURCE, output)


if __name__ == "__main__":
    unittest.main()
