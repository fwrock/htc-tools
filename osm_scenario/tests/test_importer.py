import gzip
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from tools.osm_scenario.extract import intersects
from tools.osm_scenario.importer import Options, preview, publish


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, *, nodes=None, ways=None, relations=()):
        nodes = nodes or {"1": (2, 48), "2": (2.002, 48)}
        ways = ways or [("9", ["1", "2"], {"highway": "residential", "name": "Test street", "maxspeed": "30", "lanes": "1"})]
        root = ET.Element("osm", version="0.6")
        for identity, (lon, lat) in nodes.items():
            ET.SubElement(root, "node", id=identity, lon=str(lon), lat=str(lat))
        for identity, refs, tags in ways:
            way = ET.SubElement(root, "way", id=identity)
            for ref in refs:
                ET.SubElement(way, "nd", ref=ref)
            for key, value in tags.items():
                ET.SubElement(way, "tag", k=key, v=value)
        for identity, refs, kind in relations:
            relation = ET.SubElement(root, "relation", id=identity)
            ET.SubElement(relation, "tag", k="type", v=kind)
            for ref in refs:
                ET.SubElement(relation, "member", type="way", ref=ref, role="from")
        path = self.root / "input.osm"
        ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def codes(self, report):
        return {item["code"] for item in report["diagnostics"]}

    def test_bbox_includes_crossing_road_without_clipping_or_inside_nodes(self):
        source = self.source(nodes={"1": (2, 48), "2": (2.002, 48), "3": (3, 49), "4": (3.002, 49)},
            ways=[("9", ["1", "2"], {"highway": "residential"}), ("10", ["3", "4"], {"highway": "residential"})])
        report = preview(source, Options(bbox=(2.0005, 47.999, 2.0015, 48.001)))
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["selection"]["ways"], ["9"])
        self.assertEqual(report["selection"]["nodes"], 2)
        self.assertEqual(report["selection"]["extent"]["west"], 2)
        self.assertEqual(report["selection"]["extent"]["east"], 2.002)
        self.assertEqual(report["geometry_preview"]["features"][0]["geometry"]["coordinates"], [[2, 48], [2.002, 48]])
        self.assertIn("DEFAULT_SPEED", self.codes(report))

    def test_rectangle_intersection_handles_parallel_outside_touching_and_empty(self):
        box = (0, 0, 1, 1)
        self.assertTrue(intersects([(-1, .5), (2, .5)], box))
        self.assertTrue(intersects([(0, 0), (-1, -1)], box))
        self.assertFalse(intersects([(-1, 2), (2, 2)], box))
        self.assertFalse(intersects([], box))

    def test_gzip_preserves_selection_and_repeated_preview_is_deterministic(self):
        source = self.source()
        compressed = self.root / "input.osm.gz"
        compressed.write_bytes(gzip.compress(source.read_bytes()))
        a, b, c = preview(source), preview(source), preview(compressed)
        self.assertEqual(a["preview_sha256"], b["preview_sha256"])
        self.assertEqual(a["selection"]["selected_sha256"], c["selection"]["selected_sha256"])
        self.assertNotEqual(a["source"]["sha256"], c["source"]["sha256"])

    def test_only_restrictions_touching_selected_ways_block_publication(self):
        source = self.source(relations=[("20", ["700", "800"], "restriction"), ("21", ["9"], "route")])
        self.assertEqual(preview(source)["status"], "ready")
        source = self.source(relations=[("20", ["9", "700"], "restriction")])
        report = publish(source, self.root / "blocked")
        self.assertEqual(report["status"], "invalid")
        self.assertIn("TURN_RESTRICTION_UNSUPPORTED", self.codes(report))
        self.assertFalse((self.root / "blocked").exists())

    def test_conditional_rule_is_reported_on_its_way(self):
        source = self.source(ways=[("9", ["1", "2"], {"highway": "residential", "maxspeed:conditional": "20 @ (Mo-Fr)"})])
        report = preview(source)
        errors = [item for item in report["diagnostics"] if item["level"] == "error"]
        self.assertEqual(errors[0]["entity"], "way/9")
        self.assertEqual(errors[0]["code"], "CONDITIONAL_RULE_UNSUPPORTED")

    def test_private_road_is_excluded_and_selection_limit_never_silently_truncates(self):
        source = self.source(ways=[("9", ["1", "2"], {"highway": "residential"}),
            ("10", ["1", "2"], {"highway": "service", "access": "private"})])
        report = preview(source)
        self.assertEqual(report["selection"]["ways"], ["9"])
        self.assertIn("ACCESS_EXCLUDED", self.codes(report))
        source = self.source(ways=[("9", ["1", "2"], {"highway": "residential"}), ("10", ["1", "2"], {"highway": "service"})])
        report = preview(source, Options(max_ways=1))
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(report["selection"]["truncated"])
        self.assertIn("SELECTION_TOO_LARGE", self.codes(report))

    def test_unsupported_speed_has_structured_entity_and_code(self):
        source = self.source(ways=[("9", ["1", "2"], {"highway": "residential", "maxspeed": "signals"})])
        report = preview(source)
        self.assertEqual(report["status"], "invalid")
        error = next(item for item in report["diagnostics"] if item["code"] == "UNSUPPORTED_SPEED")
        self.assertEqual(error["entity"], "way/9")

    def test_destination_or_customer_permissions_are_not_assumed(self):
        for access in ("destination", "customers"):
            source = self.source(ways=[("9", ["1", "2"], {"highway": "residential", "access": access})])
            report = preview(source)
            self.assertEqual(report["status"], "invalid")
            self.assertIn("ACCESS_RULE_UNSUPPORTED", self.codes(report))

    def test_grade_separated_crossing_does_not_invent_a_junction(self):
        source = self.source(nodes={"1": (2, 48), "2": (2.002, 48), "3": (2.001, 48),
            "4": (2.001, 47.999), "5": (2.001, 48.001), "6": (2.001, 48)},
            ways=[("9", ["1", "3", "2"], {"highway": "residential"}),
                  ("10", ["4", "6", "5"], {"highway": "residential", "bridge": "yes", "layer": "1"})])
        blocked = preview(source)
        self.assertIn("DISCONNECTED_NETWORK", self.codes(blocked))
        report = preview(source, Options(allow_disconnected=True))
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["network"]["weak_components"], 2)
        self.assertEqual(report["network"]["simulation_nodes"], 4)

    def test_publish_is_canonical_rebased_and_has_no_staging_references(self):
        from pipeline.experiments.road_network import load_for_scenario
        source = self.source()
        inspected = preview(source)
        target = self.root / "selection"
        report = publish(source, target, expected_preview=inspected["preview_sha256"])
        self.assertEqual(report["status"], "published")
        network = load_for_scenario(target / "simulation.json")
        self.assertEqual(network["source"]["parent_extract"]["sha256"], inspected["source"]["sha256"])
        simulation = json.loads((target / "simulation.json").read_text())
        self.assertEqual(simulation["id"], "selection")
        for entry in simulation["actorsDataSources"]:
            path = Path(entry["dataSource"]["info"]["path"])
            self.assertTrue(path.is_relative_to(target))
            self.assertTrue(path.is_file())
        self.assertEqual(list(self.root.glob(".htc-osm-import-*")), [])
        self.assertTrue((target / "import_report.json").is_file())

    def test_changed_preview_and_existing_destination_are_not_overwritten(self):
        source = self.source()
        inspected = preview(source)
        report = publish(source, self.root / "changed", Options(cars=17), expected_preview=inspected["preview_sha256"])
        self.assertIn("PREVIEW_CHANGED", self.codes(report))
        self.assertFalse((self.root / "changed").exists())
        target = self.root / "existing"
        target.mkdir()
        (target / "keep.txt").write_text("original")
        with self.assertRaises(FileExistsError):
            publish(source, target)
        self.assertEqual((target / "keep.txt").read_text(), "original")

    def test_racing_publisher_and_failed_compilation_leave_no_partial_revision(self):
        source = self.source()
        target = self.root / "race"
        def competing(event):
            if event["phase"] == "publish":
                target.mkdir()
                (target / "keep.txt").write_text("other publisher")
        with self.assertRaises(FileExistsError):
            publish(source, target, progress=competing)
        self.assertEqual(list(target.iterdir()), [target / "keep.txt"])
        self.assertEqual(list(self.root.glob(".htc-osm-import-*")), [])
        def failure(event):
            if event["phase"] == "compile_package":
                raise RuntimeError("test compilation failure")
        with self.assertRaises(RuntimeError):
            publish(source, self.root / "failed", progress=failure)
        self.assertFalse((self.root / "failed").exists())

    def test_source_changes_during_selection_are_detected(self):
        source = self.source()
        def changed(event):
            if event["phase"] == "verify_source":
                source.write_text(source.read_text().replace("Test street", "Changed street"))
        report = preview(source, progress=changed)
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(any("Source changed" in item["message"] for item in report["diagnostics"]))

    def test_invalid_bbox_and_empty_selection_are_actionable(self):
        source = self.source()
        for box in ((2, 48, 1, 49), (0, 0, float("nan"), 1), (170, 0, -170, 1)):
            with self.assertRaises(ValueError):
                preview(source, Options(bbox=box))
        report = preview(source, Options(bbox=(3, 49, 3.1, 49.1)))
        self.assertEqual(report["status"], "invalid")
        self.assertIn("EMPTY_SELECTION", self.codes(report))

    def test_library_options_reject_wrong_types_before_creating_outputs(self):
        source = self.source()
        for options in (Options(max_ways=True), Options(cars=1.5), Options(duration=False),
                        Options(allow_disconnected="false"), Options(bbox="2,48,3,49")):
            with self.assertRaises(ValueError):
                publish(source, self.root / "invalid", options)
            self.assertFalse((self.root / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
