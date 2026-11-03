import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("scenario_converter", Path(__file__).resolve().parents[1] / "convert.py")
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


class SidewalkRoundTripTest(unittest.TestCase):
    def test_city_map_preserves_explicit_sidewalk_and_absent_metadata(self):
        for sidewalk in (None, {"enabled": False, "width": 2.5, "gap": .4}):
            with self.subTest(sidewalk=sidewalk), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                label = {"id": "10", "resourceId": "road", "classType": "Link", "length": 100}
                if sidewalk is not None:
                    label["sidewalk"] = sidewalk
                graph = {"directed": True, "nodes": [
                    {"id": str(n), "resourceId": "node", "classType": "Node", "latitude": 48, "longitude": 2 + n * .001} for n in (1, 2)
                ], "edges": [{"source_id": "1", "target_id": "2", "weight": 100, "label": label}]}
                source = folder / "source.json"; database = folder / "city.db"; result = folder / "result.json"
                source.write_text(json.dumps(graph))
                converter.city_map_json_to_db(str(source), str(database))
                converter.city_map_db_to_json(str(database), str(result))
                self.assertEqual(json.loads(result.read_text()), graph)
                if sidewalk is None:
                    with sqlite3.connect(database) as conn:
                        conn.execute("ALTER TABLE city_map_edge DROP COLUMN sidewalk")
                    converter.city_map_db_to_json(str(database), str(result))
                    self.assertEqual(json.loads(result.read_text()), graph)


if __name__ == "__main__":
    unittest.main()
