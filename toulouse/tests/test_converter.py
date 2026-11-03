"""Focused tests for the Toulouse MATSim -> HTC converter, on a tiny synthetic MATSim scenario.

Run:  python3 -m unittest discover -s tests -v      (from tools/toulouse)

They pin the output *contract* shared with ../sao-paulo: actor field names the engine's state
classes deserialise, the explicit unresolved-mode policy, rail exclusion from the road graph,
routed Bus actors, and the simulation.json / transit index files.
"""
import contextlib
import gzip
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_hybrid_input as g

# Lambert-93 coordinates near central Toulouse; nodes are 500 m apart on a line.
X0, Y0 = 574000.0, 6279000.0
NODES = {"1": (X0, Y0), "2": (X0 + 500, Y0), "3": (X0 + 1000, Y0), "4": (X0 + 1500, Y0)}

NETWORK = """<?xml version="1.0"?><network>
<nodes>{nodes}</nodes>
<links>
<link id="a" from="1" to="2" length="500" freespeed="13.9" capacity="1000" permlanes="1" modes="car,car_passenger"/>
<link id="b" from="2" to="1" length="500" freespeed="13.9" capacity="1000" permlanes="1" modes="car,car_passenger"/>
<link id="c" from="2" to="3" length="500" freespeed="13.9" capacity="1000" permlanes="1" modes="car,car_passenger,bike"/>
<link id="d" from="3" to="2" length="500" freespeed="13.9" capacity="1000" permlanes="1" modes="car,car_passenger,bike"/>
<link id="rail1" from="3" to="4" length="500" freespeed="30" capacity="1000" permlanes="1" modes="pt,rail,train"/>
<link id="rail2" from="4" to="3" length="500" freespeed="30" capacity="1000" permlanes="1" modes="pt,rail,train"/>
</links></network>""".format(nodes="".join(f'<node id="{i}" x="{x}" y="{y}"/>' for i, (x, y) in NODES.items()))


def plan(home, work, mode, extra_leg=""):
    (hx, hy), (wx, wy) = home, work
    stub = f"{mode} interaction"
    return f"""<plan selected="yes">
<activity type="home" x="{hx}" y="{hy}" end_time="08:00:00"/>
<leg mode="walk" dep_time="08:00:00"/>
<activity type="{stub}" x="{hx}" y="{hy}" max_dur="00:00:00"/>
<leg mode="{mode}" dep_time="08:01:00"/>
<activity type="{stub}" x="{wx}" y="{wy}" max_dur="00:00:00"/>
<leg mode="walk" dep_time="08:20:00"/>
<activity type="work" x="{wx}" y="{wy}" end_time="17:00:00"/>
<leg mode="{mode}" dep_time="17:00:00"/>
<activity type="home" x="{hx}" y="{hy}"/></plan>"""


def population(*persons):
    return "<?xml version='1.0'?><population>" + "".join(
        f'<person id="{pid}">{body}</person>' for pid, body in persons) + "</population>"


SCHEDULE = f"""<?xml version="1.0"?><transitSchedule>
<transitStops>
<stopFacility id="s1" x="{X0}" y="{Y0}" linkRefId="a"/>
<stopFacility id="s2" x="{X0 + 500}" y="{Y0}" linkRefId="c" name="Mid"/>
<stopFacility id="s3" x="{X0 + 1000}" y="{Y0}" linkRefId="c" name="End"/>
</transitStops>
<transitLine id="L1"><transitRoute id="R1:x">
<transportMode>bus</transportMode>
<routeProfile><stop refId="s1" arrivalOffset="00:00:00" departureOffset="00:00:00"/>
<stop refId="s3" arrivalOffset="00:05:00" departureOffset="00:00:00"/></routeProfile>
<route/><departures>
<departure id="d1" departureTime="07:00:00" vehicleRefId="v1"/>
<departure id="d2" departureTime="07:30:00" vehicleRefId="v2"/>
<departure id="d3" departureTime="08:00:00" vehicleRefId="v3"/></departures></transitRoute>
<transitRoute id="R2"><transportMode>rail</transportMode>
<routeProfile><stop refId="s1" arrivalOffset="00:00:00"/><stop refId="s2" arrivalOffset="00:01:00"/>
<stop refId="s3" arrivalOffset="00:02:00"/></routeProfile><route/>
<departures><departure id="d9" departureTime="07:00:00" vehicleRefId="v9"/>
<departure id="d10" departureTime="08:00:00" vehicleRefId="v9"/></departures></transitRoute>
<transitRoute id="R3"><transportMode>rail</transportMode>
<routeProfile><stop refId="s3" arrivalOffset="00:00:00"/><stop refId="s1" arrivalOffset="00:02:00"/></routeProfile><route/>
<departures><departure id="d11" departureTime="09:00:00" vehicleRefId="v9"/></departures></transitRoute>
</transitLine></transitSchedule>"""

VEHICLES = """<?xml version="1.0"?><vehicleDefinitions xmlns="http://www.matsim.org/files/dtd">
<vehicleType id="Bus"><capacity seats="70" standingRoomInPersons="10"/><length meter="18.0"/></vehicleType>
<vehicleType id="Rail"><capacity seats="400" standingRoomInPersons="0"/><length meter="200.0"/></vehicleType>
</vehicleDefinitions>"""


class ToulouseConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inp = self.root / "input"
        self.inp.mkdir()
        with gzip.open(self.inp / "toulouse_network.xml.gz", "wt") as f:
            f.write(NETWORK)
        (self.inp / "toulouse_transit_schedule.xml").write_text(SCHEDULE)
        (self.inp / "toulouse_transit_vehicles.xml").write_text(VEHICLES)
        self.default_pop = population(
            ("driver", plan(NODES["1"], NODES["3"], "car")),
            ("cyclist", plan(NODES["1"], NODES["3"], "bike")),
        )

    def tearDown(self):
        self.temp.cleanup()

    def cfg(self, pop_xml=None, name="fixture", **kw):
        pop = self.inp / f"{name}_population.xml.gz"
        with gzip.open(pop, "wt") as f:
            f.write(pop_xml or self.default_pop)
        transit = kw.get("include_transit", False) or kw.get("include_rail", False)
        return g.Config(
            input_dir=self.inp, output_dir=self.root / "output" / name, scenario_name=name,
            sample_rate=1.0, docker_base=str(self.root / "output"), duration=86_400,
            network_file=self.inp / "toulouse_network.xml.gz", population_file=pop,
            transit_schedule_file=self.inp / "toulouse_transit_schedule.xml" if transit else None,
            transit_vehicles_file=self.inp / "toulouse_transit_vehicles.xml" if transit else None,
            **kw)

    def run_generator(self, cfg):
        with contextlib.redirect_stdout(io.StringIO()):
            g.HybridInputGenerator(cfg).run()
        return cfg.output_dir

    @staticmethod
    def load(out, pattern):
        items = []
        for fp in sorted((out / "data").glob(pattern)):
            items += json.loads(fp.read_text())
        return items

    # ------------------------------------------------------------------ time parsing
    def test_map_cache_skips_network_and_city_map_build_and_preserves_ids(self):
        first = self.run_generator(self.cfg(name="first", include_transit=True, include_rail=True))
        cfg = self.cfg(name="second", include_transit=True, include_rail=True)
        with patch.object(g.MatsimNetworkParser, "parse", side_effect=AssertionError("network reparsed")), \
                patch.object(g.CityMapWriter, "__init__", side_effect=AssertionError("map rebuilt")):
            second = self.run_generator(cfg)
        for path in (first / "data").glob("*.json"):
            self.assertEqual(json.loads(path.read_text()),
                             json.loads((second / "data" / path.name).read_text()), path.name)
        stats = json.loads((second / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["network_map_cache_hit"], 1)
        self.assertEqual(stats["persons_emitted"], 2)

    def test_map_cache_survives_population_changes_and_output_edits(self):
        first = self.run_generator(self.cfg(name="first"))
        original = (first / "data" / "nodes_0.json").read_text()
        (first / "data" / "nodes_0.json").write_text("[]")
        cfg = self.cfg(population(("newperson", plan(NODES["1"], NODES["3"], "walk"))), name="changed")
        with patch.object(g.MatsimNetworkParser, "parse", side_effect=AssertionError("network reparsed")):
            second = self.run_generator(cfg)
        self.assertEqual((second / "data" / "nodes_0.json").read_text(), original)
        self.assertEqual(len(self.load(second, "persons_*.json")), 1)

    def test_map_cache_invalidates_network_and_map_options(self):
        self.run_generator(self.cfg(name="first"))
        for name, options in (("micro", {"micro_ratio": 1}), ("seed", {"random_seed": 9}),
                              ("split", {"items_per_file": 1}), ("signals", {"include_signals": True})):
            cfg = self.cfg(name=name, **options)
            self.assertFalse(g.NetworkMapCache(cfg).path.exists(), name)
        cfg = self.cfg(name="modified")
        with gzip.open(cfg.network_file, "wt") as f:
            f.write(NETWORK.replace('length="500"', 'length="501"'))
        out = self.run_generator(cfg)
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["network_map_cache_hit"], 0)
        self.assertEqual(self.load(out, "links_*.json")[0]["data"]["content"]["length"], 501)

    def test_missing_or_corrupt_map_cache_rebuilds(self):
        cfg = self.cfg(name="first")
        self.run_generator(cfg)
        cache = g.NetworkMapCache(cfg).path
        for name, damage in (("missing", lambda p: p.unlink()),
                             ("corrupt", lambda p: p.write_text("[]"))):
            damage(cache / "nodes_0.json")
            out = self.run_generator(self.cfg(name=name))
            stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
            self.assertEqual(stats["network_map_cache_hit"], 0)
            self.assertEqual(len(self.load(out, "nodes_*.json")), 4)

    def test_map_cache_can_be_disabled(self):
        cfg = self.cfg(reuse_map=False)
        out = self.run_generator(cfg)
        self.assertFalse((out.parent / ".toulouse-map-cache").exists())

    def test_map_cache_saved_before_sqlite_conversion(self):
        cfg = self.cfg(name="sqlite", output_format="sqlite")
        self.run_generator(cfg)
        self.assertFalse((cfg.output_dir / "data" / "city_map.json").exists())
        with patch.object(g.MatsimNetworkParser, "parse", side_effect=AssertionError("network reparsed")):
            out = self.run_generator(self.cfg(name="json"))
        self.assertTrue((out / "data" / "city_map.json").exists())
        self.assertEqual(len(self.load(out, "links_*.json")), 4)

    def test_prefix_stops_at_generated_target_after_skipped_person(self):
        cfg = self.cfg(population(
            ("empty", ""),
            ("first", plan(NODES["1"], NODES["3"], "car")),
            ("unread", '<plan selected="yes"/><plan selected="yes"/>'),
        ))
        cfg.sample_rate = 1 / 3
        cfg.population_size = 3
        out = self.run_generator(cfg)
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["persons_emitted"], 1)
        self.assertEqual(stats["persons_scanned"], 2)
        self.assertEqual(stats["persons_total_in_file"], 3)
        self.assertEqual(stats["population_stopped_early"], 1)
        self.assertEqual(len(self.load(out, "cars_*.json")), 1)
        self.assertFalse(g._population_count_cache(cfg.population_file).exists())

    def test_population_count_cache_reused_and_invalidated(self):
        cfg = self.cfg()
        self.assertEqual(g._population_count(cfg.population_file), 2)
        with patch.object(g.ET, "iterparse", side_effect=AssertionError("cache should avoid parsing")):
            self.assertEqual(g._population_count(cfg.population_file), 2)
        with gzip.open(cfg.population_file, "wt") as f:
            f.write(population(("only", "")))
        self.assertEqual(g._population_count(cfg.population_file), 1)

    def test_random_sampling_still_scans_entire_file(self):
        cfg = self.cfg()
        cfg.sample_rate = 0.5
        cfg.sampling_strategy = "random"
        out = self.run_generator(cfg)
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["persons_scanned"], 2)
        self.assertEqual(stats["population_stopped_early"], 0)
        self.assertNotIn("persons_target", stats)

    def test_prefix_reports_unattainable_target(self):
        cfg = self.cfg(population(("empty1", ""), ("empty2", "")))
        cfg.sample_rate = 0.5
        cfg.population_size = 2
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            g.HybridInputGenerator(cfg).run()
        self.assertIn("source exhausted before target", output.getvalue())

    def test_cli_defaults_include_uncapped_bus_and_rail(self):
        cfg = self.cfg()
        result = subprocess.run([
            sys.executable, str(Path(g.__file__)), "--scenario", "cli",
            "--input-dir", str(self.inp), "--output", str(self.root / "output"),
            "--population-file", str(cfg.population_file), "--sample", "1",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = self.root / "output" / "cli"
        config = json.loads((out / "effective_config.json").read_text())
        self.assertTrue(config["include_transit"])
        self.assertTrue(config["include_rail"])
        self.assertEqual(config["max_transit_routes"], 0)
        self.assertEqual(config["max_buses_per_route"], 0)
        self.assertEqual(config["max_trains_per_line"], 0)
        self.assertTrue(self.load(out, "buses_*.json"))
        self.assertTrue(self.load(out, "subway_stations_*.json"))

    def test_times_are_strict(self):
        self.assertEqual(g._to_tick("25:00:30"), 90030)       # MATSim allows > 24 h
        self.assertEqual(g._to_tick("3600"), 3600)
        for bad in (None, "", "undefined", "08:61:00", "08:00", "-5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                g._to_tick(bad)

    # ------------------------------------------------------------------ actor contract
    def test_car_person_and_vehicle_contract(self):
        out = self.run_generator(self.cfg())
        persons = {p["id"]: p for p in self.load(out, "persons_*.json")}
        cars, bikes = self.load(out, "cars_*.json"), self.load(out, "bikes_*.json")
        self.assertEqual((len(persons), len(cars), len(bikes)), (2, 1, 1))

        car = cars[0]
        c = car["data"]["content"]
        self.assertEqual(car["typeActor"], "hybrid.actor.Car")
        self.assertEqual(c["movableStatus"], "Parked")
        self.assertEqual(c["movableCurrentNode"], c["origin"])
        self.assertIn("precomputedRoute", c)
        for stale in ("status", "bestRoute", "currentNode", "driverAttributes", "eventCount"):
            self.assertNotIn(stale, c)
        self.assertEqual(car["dependencies"]["owner"]["relation"], "part_of")
        self.assertIn(car["dependencies"]["owner"]["id"], persons)
        self.assertTrue(all(isinstance(a["id"], int) for a in [car, bikes[0], *persons.values()]))
        self.assertNotIn("precomputedRoute", bikes[0]["data"]["content"])
        self.assertEqual(bikes[0]["data"]["content"]["actorType"], "Bicycle")

        driver = next(p for p in persons.values()
                      if "car" in p["data"]["content"]["ownedVehicles"])
        plan_ = driver["data"]["content"]["originalPlan"]
        # interaction stubs collapsed: home -leg- work -leg- home
        self.assertEqual([e["kind"] for e in plan_],
                         ["Activity", "PrivateVehicleLeg", "Activity", "PrivateVehicleLeg", "Activity"])
        self.assertEqual(plan_[0]["endTime"], {"kind": "AtTick", "tick": 8 * 3600})   # dep_time wins
        self.assertEqual(plan_[1]["mode"], "Car")
        self.assertEqual(plan_[1]["vehicle"]["id"], car["id"])
        self.assertEqual(plan_[-1]["endTime"], {"kind": "AtTick", "tick": 86_400})    # horizon fallback
        self.assertEqual(driver["data"]["content"]["startTick"], 8 * 3600)
        self.assertEqual(out.joinpath("data", "id_registry.json").exists(), True)

    def test_node_and_link_use_current_engine_fields(self):
        out = self.run_generator(self.cfg())
        node = self.load(out, "nodes_*.json")[0]["data"]["content"]
        self.assertIn("signalWaitingCounts", node)
        self.assertNotIn("hasHybridConnections", node)
        self.assertNotIn("conflictZones", node)
        link = self.load(out, "links_*.json")[0]["data"]["content"]
        for stale in ("linkType", "modes", "name"):
            self.assertNotIn(stale, link)
        self.assertIn("laneConfigurations", link)

    def test_rail_links_are_not_road_links(self):
        out = self.run_generator(self.cfg())
        self.assertEqual(len(self.load(out, "links_*.json")), 4)
        meta = json.loads((out / "scenario_metadata.json").read_text())
        self.assertEqual(meta["statistics"]["links_skipped_non_road_mode"], 2)
        city = json.loads((out / "data" / "city_map.json").read_text())
        self.assertEqual(len(city["edges"]), 4)

    def signal_input(self, compressed=False):
        # OSM ids deliberately differ from MATSim ids; two stop lines at node 2
        # must merge, and a distant signal must never be snapped to this city.
        rows = []
        for osm_id, x, y in (("101", X0, Y0), ("102", X0 + 500, Y0),
                             ("103", X0 + 501, Y0), ("104", X0 + 50_000, Y0)):
            lat, lon = g._projected_to_wgs84(x, y)
            rows.append(f'<node id="{osm_id}" lat="{lat}" lon="{lon}">'
                        '<tag k="highway" v="traffic_signals"/></node>')
        text = '<osm version="0.6">' + ''.join(rows) + '<way id="1"><nd ref="101"/></way></osm>'
        path = self.inp / ('signals.osm.gz' if compressed else 'signals.osm')
        if compressed:
            with gzip.open(path, 'wt') as f:
                f.write(text)
        else:
            path.write_text(text)
        return path

    def test_osm_signals_match_roads_merge_stop_lines_and_reject_distant_points(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                out = self.run_generator(self.cfg(name=f'signals_{compressed}', include_signals=True,
                                                   signal_osm_file=self.signal_input(compressed)))
                signals = self.load(out, 'traffic_signals_*.json')
                self.assertEqual(len(signals), 2)
                links = {a['id']: a['data']['content'] for a in self.load(out, 'links_*.json')}
                nodes = {a['id']: a['data']['content'] for a in self.load(out, 'nodes_*.json')}
                self.assertEqual(sorted(len(a['data']['content']['phases']) for a in signals), [1, 2])
                for a in signals:
                    c = a['data']['content']
                    self.assertIn(c['nodes'][0], nodes)
                    self.assertEqual(a['dependencies'][str(c['nodes'][0])]['id'], c['nodes'][0])
                    node = nodes[c['nodes'][0]]
                    self.assertEqual(set(node['approachConnections']), {str(p['origin']) for p in c['phases']})
                    self.assertEqual(set(node['connections']), {str(lid) for lid, lnk in links.items() if lnk['from'] == c['nodes'][0]})
                    for phase in c['phases']:
                        self.assertEqual(links[phase['origin']]['to'], c['nodes'][0])
                        self.assertGreater(phase['greenDuration'], 0)
                        self.assertLessEqual(phase['greenStart'] + phase['greenDuration'], c['cycleDuration'])
                    for outgoing, origin in node['connections'].items():
                        incoming = links[origin['id']]
                        # This straight fixture has an unambiguous continuation.
                        if len(c['phases']) > 1:
                            self.assertNotEqual(links[int(outgoing)]['to'], incoming['from'])
                stats = json.loads((out / 'scenario_metadata.json').read_text())['statistics']
                self.assertEqual(stats['signal_osm_signal_nodes'], 4)
                self.assertEqual(stats['signal_signals_unmatched'], 1)
                self.assertEqual(stats['signal_osm_signal_nodes_merged'], 1)
                sim = json.loads((out / 'simulation.json').read_text())
                src = next(s for s in sim['actorsDataSources'] if s['classType'] == 'hybrid.actor.TrafficSignal')
                self.assertEqual(src['entityLifecycle'], 'STATIC')
                self.assertTrue(Path(src['dataSource']['info']['path']).is_file())

    def test_signals_are_converted_and_registered_in_sqlite_output(self):
        out = self.run_generator(self.cfg(name='signal_sqlite', include_signals=True,
                                          signal_osm_file=self.signal_input(), output_format='sqlite'))
        self.assertEqual(self.load(out, 'traffic_signals_*.json'), [])
        with sqlite3.connect(out / 'data/traffic_signals_0.db') as db:
            rows = db.execute('SELECT type_actor, data_content, relationships FROM actor_simulation').fetchall()
        self.assertEqual(len(rows), 2)
        for actor_type, content, relationships in rows:
            self.assertEqual(actor_type, 'hybrid.actor.TrafficSignal')
            node = json.loads(content)['nodes'][0]
            self.assertIn(str(node), json.loads(relationships))
        sources = json.loads((out / 'simulation.json').read_text())['actorsDataSources']
        source = next(s for s in sources if s['classType'] == 'hybrid.actor.TrafficSignal')
        self.assertEqual(source['dataSource']['sourceType'], 'sqlite')
        self.assertTrue(Path(source['dataSource']['info']['path']).is_file())

    def test_signal_input_required_before_output_is_created(self):
        cfg = self.cfg(name='missing_signals', include_signals=True)
        with self.assertRaisesRegex(FileNotFoundError, 'signal-osm-file'):
            self.run_generator(cfg)
        self.assertFalse(cfg.output_dir.exists())

    def test_signal_configuration_validation(self):
        for kw in ({'signal_cycle_duration': 5}, {'signal_cycle_duration': 0},
                   {'signal_max_snap_distance': float('nan')}, {'signal_max_snap_distance': -1}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.cfg(**kw).validate()

    def test_rail_includes_subway_tram_and_regional_trains(self):
        path = self.inp / 'toulouse_transit_schedule.xml'
        text = path.read_text()
        # Each mode has its own line, as in the real schedule.
        rail_line = text[text.index('<transitLine'):text.index('</transitLine>') + len('</transitLine>')]
        extra = ''.join(rail_line.replace('id="L1"', f'id="{mode}"').replace(
            '<transportMode>rail</transportMode>', f'<transportMode>{mode}</transportMode>')
            for mode in ('subway', 'tram'))
        path.write_text(text.replace('</transitSchedule>', extra + '</transitSchedule>'))
        out = self.rail_out(max_trains_per_line=6)
        types = {a['data']['content']['railType'] for a in self.load(out, 'rail_links_*.json')}
        self.assertEqual(types, {'SUBWAY', 'LIGHT_RAIL', 'HEAVY_RAIL'})
        stats = json.loads((out / 'scenario_metadata.json').read_text())['statistics']
        for mode in ('subway', 'tram', 'rail'):
            self.assertEqual(stats[f'rail_lines_{mode}'], 1)
            self.assertEqual(stats[f'rail_trains_{mode}'], 12)

    def test_transit_cli_includes_rail_unless_explicitly_disabled(self):
        for disabled in (False, True):
            name = f'cli_{disabled}'
            cfg = self.cfg(name=name)
            argv = ['generate_hybrid_input.py', '--scenario', name, '--input-dir', str(self.inp),
                    '--output', str(self.root / 'output'), '--population-file', str(cfg.population_file),
                    '--sample', '1', '--transit', '--unsupported-mode', 'walk']
            if disabled:
                argv.append('--no-rail')
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
                g.main()
            self.assertEqual(bool(list(cfg.output_dir.glob('data/rail_links_*.json'))), not disabled)

    # ------------------------------------------------------------------ mode policy
    def pt_population(self):
        return population(("rider", plan(NODES["1"], NODES["3"], "pt")))

    def test_unresolved_modes_are_not_silently_walked(self):
        with self.assertRaisesRegex(ValueError, "unresolved modes"):
            self.run_generator(self.cfg(self.pt_population(), name="pt_error"))

    def test_walk_policy_is_counted(self):
        out = self.run_generator(self.cfg(self.pt_population(), name="pt_walk", unsupported_mode="walk"))
        person = self.load(out, "persons_*.json")[0]
        kinds = [e["kind"] for e in person["data"]["content"]["originalPlan"]]
        self.assertEqual(kinds, ["Activity", "WalkLeg", "Activity", "WalkLeg", "Activity"])
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["trips_mode_fallback_walk"], 2)
        self.assertEqual(stats["trips_unresolved_mode"], 2)

    def test_skip_person_policy(self):
        out = self.run_generator(self.cfg(self.pt_population(), name="pt_skip", unsupported_mode="skip-person"))
        self.assertEqual(self.load(out, "persons_*.json"), [])

    def test_replan_emits_pending_decisions(self):
        out = self.run_generator(self.cfg(
            self.pt_population(), name="pt_replan", unsupported_mode="replan",
            include_transit=True, mc_included_modes=["bus", "walk"]))
        person = self.load(out, "persons_*.json")[0]
        decisions = [e for e in person["data"]["content"]["originalPlan"] if e["kind"] == "PendingDecision"]
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["decision"]["allowedModes"], ["Bus", "Walk"])

    def test_config_rejects_impossible_choices(self):
        with self.assertRaisesRegex(ValueError, "replan requires"):
            self.cfg(name="v1", unsupported_mode="replan").validate()
        with self.assertRaisesRegex(ValueError, "subway"):
            self.cfg(name="v2", enable_dynamic_mode_choice=True, include_transit=True,
                     mc_included_modes=["subway", "walk"]).validate()
        with self.assertRaisesRegex(ValueError, "bus mode choice requires"):
            self.cfg(name="v3", enable_dynamic_mode_choice=True).validate()

    # ------------------------------------------------------------------ snapping
    def test_far_activities_are_skipped_not_teleported(self):
        far = population(("tourist", plan((X0 + 50_000, Y0), NODES["3"], "car")))
        out = self.run_generator(self.cfg(far, name="far"))
        self.assertEqual(self.load(out, "persons_*.json"), [])
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["persons_skipped_snap_distance"], 1)

    # ------------------------------------------------------------------ transit
    def test_routed_buses_and_transit_indexes(self):
        out = self.run_generator(self.cfg(name="bus", include_transit=True, max_buses_per_route=2))
        buses = self.load(out, "buses_*.json")
        stops = self.load(out, "bus_stops_*.json")
        self.assertEqual(sorted(b["data"]["content"]["startTick"] for b in buses), [7 * 3600, 7 * 3600 + 1800])
        self.assertEqual(len(stops), 2)
        b = buses[0]["data"]["content"]
        self.assertEqual(b["capacity"], 80)       # seats + standing from the vehicle type
        self.assertEqual(b["size"], 18.0)
        self.assertTrue(b["storedBestRoute"])
        self.assertEqual(b["storedBestRoute"][-1][1], b["origin"])   # closed loop back to stop 0
        self.assertEqual(set(b["busStops"].values()), {b["origin"], b["destination"]})
        self.assertEqual(stops[0]["data"]["content"]["people"], {})
        # no stale BusStation actors
        self.assertEqual(list((out / "data").glob("bus_stations_*.json")), [])

        tmap = json.loads((out / "data" / "transit_map.json").read_text())
        self.assertEqual({e["actorClassType"] for e in tmap}, {"hybrid.actor.BusStop"})
        self.assertTrue(all(e["lines"] == ["R1_x"] for e in tmap))
        self.assertTrue(all(-90 < e["latitude"] < 90 and 0 < e["longitude"] < 10 for e in tmap))
        routes = json.loads((out / "data" / "transit_routes.json").read_text())
        self.assertEqual(len(routes), 1)                              # the rail route is not converted
        self.assertEqual(routes[0]["headwaySeconds"], 1800)
        self.assertEqual([s["travelTimeFromPrevSeconds"] for s in routes[0]["stops"]], [0, 300])
        self.assertEqual({s["stopId"] for s in routes[0]["stops"]}, {e["id"] for e in tmap})

        sim = json.loads((out / "simulation.json").read_text())
        self.assertIn("transitMapFile", sim)
        self.assertIn("transitRoutesFile", sim)
        self.assertFalse(sim["enableDynamicModeChoice"])
        classes = [s["classType"] for s in sim["actorsDataSources"]]
        for expected in ("hybrid.actor.Node", "hybrid.actor.Link", "hybrid.actor.Person",
                         "hybrid.actor.Car", "hybrid.actor.Bicycle", "hybrid.actor.BusStop", "hybrid.actor.Bus"):
            self.assertIn(expected, classes)
        self.assertNotIn("hybrid.actor.BusStation", classes)
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["transit_routes_mode_rail"], 2)
        self.assertEqual(list((out / "data").glob("subway_stations_*.json")), [])   # --rail off

    # ------------------------------------------------------------------ rail
    def rail_out(self, **kw):
        return self.run_generator(self.cfg(name="rail", include_rail=True, **kw))

    def test_rail_line_uses_dominant_pattern_and_both_directions(self):
        out = self.rail_out(max_trains_per_line=6)
        stations = {s["data"]["content"]["name"]: s for s in self.load(out, "subway_stations_*.json")}
        self.assertEqual(len(stations), 3)
        links = self.load(out, "rail_links_*.json")
        self.assertEqual(len(links), 4)                               # 2 forward + 2 return segments
        c = links[0]["data"]["content"]
        for field in ("railType", "subwayLine", "fromStation", "toStation", "simulationMode", "lanes"):
            self.assertIn(field, c)
        self.assertEqual(c["railType"], "HEAVY_RAIL")
        self.assertEqual({l["data"]["content"]["subwayLine"] for l in links}, {"rail_L1"})

        first = next(s for s in stations.values() if s["data"]["content"]["garage"]
                     and "rail_L1" in s["data"]["content"]["lines"])
        fc = first["data"]["content"]
        trains = fc["subways"]["rail_L1"]
        self.assertEqual(len(trains), 6)                              # capped by --max-trains-per-line
        self.assertEqual(trains[0]["capacity"], 400)                  # Rail vehicle type
        self.assertAlmostEqual(trains[0]["velocity"], 1000 / (120 - 30), places=3)   # schedule run time
        self.assertEqual(fc["lines"]["rail_L1"]["interval"], 3600)    # median gap of 07:00 / 08:00
        self.assertEqual(len(fc["linesRoute"]["rail_L1"]), 4)         # fwd + ret cycle
        # the dominant (2-departure) pattern wins; the 1-departure reverse pattern is counted
        stats = json.loads((out / "scenario_metadata.json").read_text())["statistics"]
        self.assertEqual(stats["rail_rail_patterns_unused"], 1)
        self.assertEqual(stats["rail_subway_trains"], 12)

    def test_rail_indexes_and_manifest(self):
        out = self.rail_out(include_transit=True)
        tmap = json.loads((out / "data" / "transit_map.json").read_text())
        subway = [e for e in tmap if e["stopType"] == "subway"]
        self.assertEqual(len(subway), 3)
        self.assertEqual({e["actorClassType"] for e in subway}, {"hybrid.actor.SubwayStation"})
        self.assertTrue(all(e["lines"] == ["rail_L1", "rail_L1_ret"] for e in subway))
        routes = {r["lineId"]: r for r in json.loads((out / "data" / "transit_routes.json").read_text())}
        self.assertEqual(sorted(k for k in routes if k.startswith("rail")), ["rail_L1", "rail_L1_ret"])
        self.assertEqual(routes["rail_L1"]["stops"][0]["travelTimeFromPrevSeconds"], 0)
        self.assertEqual(len(routes["rail_L1"]["stops"]), 3)
        ids = {e["id"] for e in tmap}
        self.assertTrue(all(s["stopId"] in ids for r in routes.values() for s in r["stops"]))
        sim = json.loads((out / "simulation.json").read_text())
        self.assertEqual(sim["dynamicActorTypes"], ["hybrid.actor.Subway"])
        classes = {s["classType"] for s in sim["actorsDataSources"]}
        self.assertTrue({"hybrid.actor.SubwayStation", "hybrid.actor.RailLink"} <= classes)

    def test_rail_stations_must_snap(self):
        # every stop 50 km north of the network: nothing may snap, the line must not be shortcut
        (self.inp / "toulouse_transit_schedule.xml").write_text(SCHEDULE.replace(f'y="{Y0}"', f'y="{Y0 + 50_000}"'))
        with self.assertRaisesRegex(ValueError, "no rail links"):
            self.run_generator(self.cfg(name="rail_far", include_rail=True))

    def test_subway_mode_choice_needs_rail(self):
        self.cfg(name="mc_ok", include_rail=True, enable_dynamic_mode_choice=True,
                 mc_included_modes=["subway", "walk"]).validate()

    def test_every_data_source_file_exists(self):
        out = self.run_generator(self.cfg(name="files", include_transit=True))
        sim = json.loads((out / "simulation.json").read_text())
        base = f"{sim['cityMapFile'].rsplit('/data/', 1)[0]}/data/"
        for src in sim["actorsDataSources"]:
            path = Path(src["dataSource"]["info"]["path"].replace(base, str(out / "data") + "/"))
            self.assertTrue(path.exists(), path)

    # ------------------------------------------------------------------ hygiene
    def test_non_empty_output_is_refused(self):
        cfg = self.cfg(name="reuse")
        self.run_generator(cfg)
        with self.assertRaises(FileExistsError):
            self.run_generator(self.cfg(name="reuse"))

    def test_ids_are_unique_across_actor_types(self):
        out = self.run_generator(self.cfg(name="ids", include_transit=True))
        seen = []
        for pattern in ("nodes_*", "links_*", "persons_*", "cars_*", "bikes_*", "bus_stops_*", "buses_*"):
            seen += [a["id"] for a in self.load(out, pattern + ".json")]
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
