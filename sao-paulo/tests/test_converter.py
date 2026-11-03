import contextlib
import csv
import gzip
import io
import json
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate_hybrid_input as g
import fetch_inputs as fetch
from pyproj import Transformer


def write_csv(path, fields, rows, delimiter=','):
    with path.open('w', newline='') as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(fields)
        writer.writerows(rows)


def fixtures(root):
    inp = root / 'input'
    inp.mkdir()
    inp.joinpath('sao_paulo.osm').write_text('''<osm version="0.6">
<node id="1" lat="-23.55" lon="-46.65"><tag k="railway" v="stop"/><tag k="name" v="A"/></node>
<node id="2" lat="-23.55" lon="-46.649"/>
<node id="3" lat="-23.55" lon="-46.648"><tag k="public_transport" v="stop_position"/><tag k="name" v="B"/></node>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="residential"/><tag k="maxspeed" v="36"/></way>
<way id="20"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="railway" v="subway"/></way>
<relation id="30"><member type="node" ref="1" role="stop"/><member type="node" ref="3" role="stop"/><member type="way" ref="20" role=""/>
<tag k="type" v="route"/><tag k="route" v="subway"/><tag k="ref" v="1"/><tag k="name" v="Test metro"/></relation></osm>''')
    e, n = g._wgs84_to_utm23s(-23.55, -46.65)
    e2, n2 = g._wgs84_to_utm23s(-23.55, -46.648)
    inp.joinpath('output_plans.xml').write_text(f'''<population>
<person id="driver"><plan selected="no"><activity type="ignored"/></plan><plan selected="yes">
<activity type="home" x="{e}" y="{n}" end_time="00:00:05"/><leg mode="car"/>
<activity type="work" x="{e2}" y="{n2}" end_time="00:01:00"/><leg mode="car"/>
<activity type="home" x="{e}" y="{n}"/></plan></person>
</population>''')
    gtfs = inp / 'bus'
    gtfs.mkdir()
    write_csv(gtfs/'agency.txt', ['agency_id','agency_name','agency_url','agency_timezone'], [['a','test','https://example.org','America/Sao_Paulo']])
    write_csv(gtfs/'routes.txt', ['route_id','route_short_name','route_type'], [['r','R','3'],['rail','M','1']])
    write_csv(gtfs/'stops.txt', ['stop_id','stop_name','stop_lat','stop_lon'], [['a','A',-23.55,-46.65],['b','B',-23.55,-46.648]])
    write_csv(gtfs/'calendar.txt', ['service_id','monday','tuesday','wednesday','thursday','friday','saturday','sunday','start_date','end_date'], [['daily',1,1,1,1,1,1,1,20260101,20261231], ['removed',1,1,1,1,1,1,1,20260101,20261231]])
    write_csv(gtfs/'calendar_dates.txt', ['service_id','date','exception_type'], [['removed',20261027,2], ['added',20261027,1]])
    write_csv(gtfs/'trips.txt', ['route_id','service_id','trip_id','direction_id'], [['r','daily','freq','0'],['r','added','reverse','1'],['r','removed','removed-trip','0'],['rail','daily','metro-trip','0']])
    write_csv(gtfs/'stop_times.txt', ['trip_id','arrival_time','departure_time','stop_id','stop_sequence'], [
        ['freq','00:00:10','00:00:10','a',1],['freq','00:00:40','00:00:40','b',2],
        ['reverse','00:01:30','00:01:30','b',1],['reverse','00:02:00','00:02:00','a',2],
        ['removed-trip','00:00:20','00:00:20','a',1],['removed-trip','00:00:50','00:00:50','b',2],
        ['metro-trip','00:00:20','00:00:20','a',1],['metro-trip','00:00:50','00:00:50','b',2]])
    write_csv(gtfs/'frequencies.txt', ['trip_id','start_time','end_time','headway_secs','exact_times'], [['freq','00:00:10','00:01:10',20,1]])
    return inp


def config(root, inp, **kw):
    return g.Config(input_dir=inp, output_dir=root/'output'/'fixture', scenario_name='fixture',
                    input_format='matsim', population_crs='EPSG:32723', sample_rate=1,
                    docker_base=str(root/'output'), duration=120, service_date='2026-10-27', **kw)


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inp = fixtures(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_generator(self, cfg):
        with contextlib.redirect_stdout(io.StringIO()):
            g.HybridInputGenerator(cfg).run()

    def add_signals(self):
        path = self.inp / 'sao_paulo.osm'
        text = path.read_text().replace('<node id="2" lat="-23.55" lon="-46.649"/>',
            '<node id="2" lat="-23.55" lon="-46.649"><tag k="highway" v="traffic_signals"/></node>')
        text = text.replace('<tag k="railway" v="stop"/>',
            '<tag k="railway" v="stop"/><tag k="highway" v="traffic_signals"/>')
        path.write_text(text)

    def test_osm_signals_include_single_approach_and_survive_network_reuse(self):
        self.add_signals()
        cfg = config(self.root, self.inp, include_signals=False)
        self.run_generator(cfg)
        data = cfg.output_dir / 'data'
        self.assertEqual(json.loads((data / 'osm_signal_nodes.json').read_text()), ['1', '2'])
        cfg.skip_network = cfg.include_signals = True
        self.run_generator(cfg)
        signals = json.loads((data / 'traffic_signals_0.json').read_text())
        self.assertEqual(len(signals), 2)
        links = {a['id']: a['data']['content'] for a in json.loads((data / 'links_0.json').read_text())}
        nodes = {a['id']: a['data']['content'] for a in json.loads((data / 'nodes_0.json').read_text())}
        for actor in signals:
            c = actor['data']['content']
            self.assertEqual(actor['dependencies'][str(c['nodes'][0])]['id'], c['nodes'][0])
            node = nodes[c['nodes'][0]]
            self.assertEqual(set(node['approachConnections']), {str(p['origin']) for p in c['phases']})
            self.assertEqual(set(node['connections']), {str(lid) for lid, lnk in links.items() if lnk['from'] == c['nodes'][0]})
            for phase in c['phases']:
                self.assertEqual(links[phase['origin']]['to'], c['nodes'][0])
                self.assertLess(phase['greenStart'] + phase['greenDuration'], c['cycleDuration'])
        original = (data / 'traffic_signals_0.json').read_bytes()
        self.run_generator(cfg)
        self.assertEqual((data / 'traffic_signals_0.json').read_bytes(), original)
        # Old outputs stored only actors, whose schema uses nodes, not nodeId.
        (data / 'osm_signal_nodes.json').unlink()
        self.run_generator(cfg)
        self.assertEqual((data / 'traffic_signals_0.json').read_bytes(), original)
        cfg.include_signals = False
        self.run_generator(cfg)
        for actor in json.loads((data / 'nodes_0.json').read_text()):
            self.assertFalse(actor['data']['content']['connections'])
            self.assertFalse(actor['data']['content'].get('approachConnections'))

    def test_gzip_osm_signals_on_reversed_oneway(self):
        self.add_signals()
        path = self.inp / 'sao_paulo.osm'
        text = path.read_text().replace('<tag k="highway" v="residential"/>',
            '<tag k="highway" v="residential"/><tag k="oneway" v="-1"/>')
        with gzip.open(path.with_suffix('.osm.gz'), 'wt') as f:
            f.write(text)
        path.unlink()
        cfg = config(self.root, self.inp, include_signals=True)
        self.run_generator(cfg)
        signals = json.loads((cfg.output_dir / 'data/traffic_signals_0.json').read_text())
        self.assertEqual(len(signals), 2)
        self.assertTrue(all(len(a['data']['content']['phases']) == 1 for a in signals))

    def test_signal_cycle_rejects_overlapping_phases(self):
        self.add_signals()
        cfg = config(self.root, self.inp, include_signals=True, signal_cycle_duration=10)
        with self.assertRaisesRegex(ValueError, 'too short'):
            self.run_generator(cfg)

    def test_complete_matsim_gtfs_rail_scenario(self):
        cfg = config(self.root, self.inp, include_transit=True, transit_providers=['bus'], include_rail=True)
        self.run_generator(cfg)
        data = cfg.output_dir/'data'
        buses = json.loads((data/'buses_0.json').read_text())
        self.assertEqual(sorted(a['data']['content']['startTick'] for a in buses), [10,30,50,90])
        self.assertFalse(list(data.glob('bus_stations_*.json')))
        self.assertTrue(all(a['data']['content']['size'] == 12 for a in buses))
        links = {a['id']:a['data']['content'] for a in json.loads((data/'links_0.json').read_text())}
        for bus in buses:
            c = bus['data']['content']
            node = c['origin']
            for link, target in c['storedBestRoute']:
                self.assertEqual(links[link]['from'], node)
                self.assertEqual(links[link]['to'], target)
                node = target
            self.assertEqual(node, c['origin'])
        person = json.loads((data/'persons_0.json').read_text())[0]['data']['content']
        self.assertEqual([a['endTime']['tick'] for a in person['originalPlan'] if a['kind']=='Activity'], [5,60,120])
        car=json.loads((data/'cars_0.json').read_text())[0]['data']['content']
        self.assertFalse(car['scheduleOnTimeManager'])
        self.assertEqual(car['movableStatus'],'Parked')
        manifest = json.loads((cfg.output_dir/'simulation.json').read_text())
        self.assertEqual(manifest['dynamicActorTypes'], ['hybrid.actor.Subway'])
        self.assertTrue(all(Path(a['dataSource']['info']['path']).exists() for a in manifest['actorsDataSources']))
        route_index = json.loads((data/'transit_routes.json').read_text())
        self.assertEqual(len([r for r in route_index if r['stopType']=='bus']), 2)
        self.assertEqual({r['lineId'] for r in route_index if r['stopType']=='subway'}, {'subway_1','subway_1_ret'})
        with self.assertRaises(FileExistsError):
            self.run_generator(cfg)

    def test_fleet_cap_selects_earliest_departures_across_patterns(self):
        cfg=config(self.root,self.inp,include_transit=True,transit_providers=['bus'],max_buses_per_route=2)
        self.run_generator(cfg)
        buses=json.loads((cfg.output_dir/'data/buses_0.json').read_text())
        self.assertEqual([b['data']['content']['startTick'] for b in buses],[10,30])
        metadata=json.loads((cfg.output_dir/'scenario_metadata.json').read_text())
        self.assertEqual(metadata['statistics']['transit_departures_removed_by_fleet_cap'],2)

    def test_skip_network_removes_old_transit_indexes(self):
        cfg = config(self.root,self.inp,include_transit=True,transit_providers=['bus'])
        self.run_generator(cfg)
        cfg.skip_network = True
        cfg.include_transit = False
        self.run_generator(cfg)
        data=cfg.output_dir/'data'
        self.assertFalse((data/'transit_map.json').exists())
        self.assertFalse(list(data.glob('buses_*.json')))
        manifest=json.loads((cfg.output_dir/'simulation.json').read_text())
        self.assertNotIn('transitMapFile',manifest)
        cfg.micro_ratio=0.5
        with self.assertRaisesRegex(ValueError,'matching network_config'):
            self.run_generator(cfg)

    def test_clock_and_snap_validation(self):
        self.assertEqual(g.PopulationConverter._to_tick('25:10:00'),90600)
        self.assertEqual(g.PopulationConverter._to_tick('12.6'),13)
        for value in ('invalid','00:65:00','nan','-1'):
            with self.assertRaises(ValueError):
                g.PopulationConverter._to_tick(value)
        cfg=config(self.root,self.inp)
        parser=g.OsmNetworkParser(cfg,self.root,g.ScenarioIdRegistry())
        from scipy.spatial import cKDTree
        parser.kdtree=cKDTree([[0,0]])
        parser._kd_ids=['1']
        self.assertIsNone(parser.nearest_node(1001,0))
        self.assertIsNone(parser.nearest_node(float('nan'),0))
        self.assertEqual(parser.nearest_node(10,0),'1')

    def test_gzip_and_single_unselected_plan(self):
        path=self.inp/'output_plans.xml'
        with gzip.open(self.inp/'output_plans.xml.gz','wt') as f:
            f.write(path.read_text())
        path.unlink()
        cfg=config(self.root,self.inp)
        self.run_generator(cfg)
        self.assertTrue((cfg.output_dir/'data/persons_0.json').exists())

    def test_local_rail_extract_retains_stop_positions(self):
        output=self.root/'rail.osm'
        fetch.local_rail_extract(self.inp/'sao_paulo.osm',output)
        tree=ET.parse(output)
        self.assertEqual(len(tree.findall('relation')),1)
        self.assertEqual(len(tree.findall('way')),1)
        self.assertEqual(len(tree.findall('node')),3)
        self.assertEqual(tree.find('way/tag').get('v'),'subway')

    def test_matsim_stages_and_duration(self):
        cfg = config(self.root, self.inp)
        converter = g.PopulationConverter(cfg, None, self.root, g.ScenarioIdRegistry())
        plan = ET.fromstring('''<plan><activity type="home" x="1" y="2" max_dur="00:00:30"/>
<leg mode="access_walk"/><activity type="pt interaction"/><leg mode="pt"/>
<activity type="pt interaction"/><leg mode="egress_walk"/><activity type="work" x="3" y="4"/></plan>''')
        acts, trips = converter._matsim_plan(plan, Transformer.from_crs(32723,32723,always_xy=True))
        self.assertEqual(len(acts),2)
        self.assertEqual(acts[0]['end_spec'], {'kind':'Duration','ticks':30})
        self.assertEqual(trips[0]['mode'],'pt')
        self.assertEqual(converter.stats['matsim_stage_activities_collapsed'],2)

    def test_unresolved_modes_are_not_silently_walked(self):
        cfg = config(self.root, self.inp)
        converter = g.PopulationConverter(cfg, None, self.root, g.ScenarioIdRegistry())
        with self.assertRaisesRegex(ValueError, 'unresolved modes'):
            converter._emit_person('p', [], [{'mode':'pt'}])

    def test_expired_service_is_rejected(self):
        cfg = config(self.root, self.inp, include_transit=True, transit_providers=['bus'])
        cfg.service_date = '2030-01-01'
        converter = g.GtfsTransitConverter(cfg, None, self.root, g.ScenarioIdRegistry())
        with self.assertRaisesRegex(ValueError, 'no service'):
            converter._active_services(self.inp/'bus')

    def test_reverse_oneway_matches_graph(self):
        cfg = config(self.root, self.inp)
        data = self.root/'data'
        data.mkdir()
        osm = g.OsmNetworkParser(cfg,data,g.ScenarioIdRegistry())
        osm._node_utm = {'1':(0,0),'2':(100,0)}
        osm._ways = [{'highway':'residential','nodes':['1','2'],'tags':{},'oneway':'-1'}]
        osm._link_writer = g.StreamingWriter(data,'links')
        osm._write_links()
        osm._link_writer.close()
        links = json.loads((data/'links_0.json').read_text())
        self.assertEqual(len(links),1)
        self.assertEqual(links[0]['data']['content']['from'],osm.ids.id_for('node','2'))
        self.assertEqual(links[0]['data']['content']['to'],osm.ids.id_for('node','1'))

    def test_eqasim_geopackage_crs(self):
        cfg = config(self.root,self.inp)
        cfg.input_format, cfg.population_crs = 'eqasim', None
        write_csv(self.inp/'persons.csv',['person_id'],[[1]],';')
        write_csv(self.inp/'trips.csv', ['person_id','trip_id','mode','departure_time','arrival_time','preceeding_activity_index','following_activity_index'], [[1,0,'bike',5,20,0,1]],';')
        to_sad69 = Transformer.from_crs(4326,29193,always_xy=True)
        with sqlite3.connect(self.inp/'activities.gpkg') as c:
            c.execute('create table gpkg_geometry_columns(table_name,column_name,srs_id)')
            c.execute("insert into gpkg_geometry_columns values ('activities','geom',29193)")
            c.execute('create table gpkg_spatial_ref_sys(srs_id,definition,organization,organization_coordsys_id)')
            c.execute("insert into gpkg_spatial_ref_sys values (29193,'unused','EPSG',29193)")
            c.execute('create table activities(person_id,activity_id,purpose,start_time,end_time,is_last,geom)')
            for i,lon in enumerate((-46.65,-46.648)):
                x,y = to_sad69.transform(lon,-23.55)
                blob = b'GP\x00\x01' + struct.pack('<i',29193) + struct.pack('<BIdd',1,1,x,y)
                c.execute('insert into activities values (?,?,?,?,?,?,?)',(1,i,'home' if i==0 else 'work',None,5 if i==0 else None,i==1,blob))
        self.run_generator(cfg)
        data=cfg.output_dir/'data'
        person=json.loads((data/'persons_0.json').read_text())[0]['data']['content']
        self.assertIn('bicycle',person['ownedVehicles'])
        self.assertEqual(person['originalPlan'][1]['mode'],'Bicycle')
        meta=json.loads((cfg.output_dir/'scenario_metadata.json').read_text())
        self.assertEqual(meta['source']['populationCrs'],'EPSG:29193')

    def test_config_file_and_cli_override(self):
        settings=self.root/'settings.json'
        settings.write_text(json.dumps({'scenario':'from-file','input-dir':'input','output':'out','input-format':'matsim','population-crs':'EPSG:32723','sample':1,'duration':120,'rail':True,'rail-types':['subway']}))
        script=Path(g.__file__)
        result=subprocess.run([sys.executable,str(script),'--config',str(settings),'--scenario','override'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.root/'out'/'override'/'simulation.json').exists())
        settings.write_text('{"unknown":1}')
        result=subprocess.run([sys.executable,str(script),'--config',str(settings)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('unknown configuration key',result.stderr)

    def test_rail_terminal_joins_close_directional_seams(self):
        cfg=config(self.root,self.inp)
        converter=g.RailTransitConverter(cfg,None,None,self.root,g.ScenarioIdRegistry())
        def stop(key,name,x):
            return {'osm_node_id':key,'name':name,'utm_e':x,'utm_n':0,'distance_from_prev':100}
        forward=[stop('a','A',0),stop('b','B',100)]
        reverse=[stop('c','B',105),stop('d','A',5)]
        converter._stations={s['osm_node_id']:{**s,'highway_node_id':s['osm_node_id']} for s in forward+reverse}
        converter._lines={'subway_1':{'route_type':'subway','forward_stations':forward,'return_stations':reverse}}
        converter._create_rail_links()
        line=converter._lines['subway_1']
        self.assertEqual(len(line['terminal_links']),2)
        for cycle in (line['fwd_cycle'],line['ret_cycle']):
            for before,after in zip(cycle,cycle[1:]+cycle[:1]):
                self.assertEqual(before['to_hw_node'],after['from_hw_node'])

    def test_line_override_object_and_legacy_list(self):
        path=self.inp/'rail.json'
        cfg=config(self.root,self.inp,rail_lines_config_file='rail.json')
        for raw in ({'subway_1':{'headway_seconds':10,'max_trains':0}},
                    [{'line_key_pattern':'subway_1','_comment':'reviewed','headway_seconds':10}]):
            path.write_text(json.dumps(raw))
            converter=g.RailTransitConverter(cfg,None,None,self.root,g.ScenarioIdRegistry())
            self.assertEqual(converter._line_overrides['subway_1']['headway_seconds'],10)
        path.write_text('{"subway_1":{"headway_seconds":0}}')
        with self.assertRaisesRegex(ValueError,'Invalid rail override'):
            g.RailTransitConverter(cfg,None,None,self.root,g.ScenarioIdRegistry())

    def test_download_unpack_does_not_write_archive_paths(self):
        import zipfile
        archive=self.root/'feed.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for table in fetch.REQUIRED | {'calendar.txt'}:
                z.writestr('../../'+table,'header\n')
            z.writestr('../../executable.py','bad')
        target=self.root/'gtfs'
        fetch.install_gtfs(archive,target)
        self.assertEqual({p.name for p in target.iterdir()}, fetch.REQUIRED | {'calendar.txt'})
        self.assertFalse((self.root/'executable.py').exists())


if __name__=='__main__':
    unittest.main()
