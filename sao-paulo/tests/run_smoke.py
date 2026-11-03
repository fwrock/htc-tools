#!/usr/bin/env python3
"""Generate a converted fixture, run HTC, and verify model outcomes from JSON reports."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from test_converter import fixtures, config, g


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-root', type=Path, required=True, help='Fresh directory for inputs, actors and runtime evidence')
    p.add_argument('--htc-root', type=Path, default=Path(__file__).resolve().parents[3]/'hyperbolic-time-chamber')
    p.add_argument('--jar', type=Path)
    p.add_argument('--base-port', type=int, default=32650)
    p.add_argument('--signals', action='store_true', help='Include extracted OSM traffic lights and verify phase changes')
    p.add_argument('--validate-only',action='store_true',help='Recheck preserved reports without running HTC')
    args = p.parse_args()
    root = args.output_root.resolve()
    if args.validate_only:
        expected = json.loads((root/'expected.json').read_text())
    else:
        root.mkdir(parents=True, exist_ok=False)
        inp = fixtures(root)
        if args.signals:
            osm = inp/'sao_paulo.osm'
            osm.write_text(osm.read_text().replace('<node id="2" lat="-23.55" lon="-46.649"/>',
                '<node id="2" lat="-23.55" lon="-46.649"><tag k="highway" v="traffic_signals"/></node>'))
        e,n = g._wgs84_to_utm23s(-23.55,-46.65)
        e2,n2 = g._wgs84_to_utm23s(-23.55,-46.648)
        plans = inp/'output_plans.xml'
        plans.write_text(plans.read_text().replace('</population>', f'''<person id="rider"><plan selected="yes">
    <activity type="home" x="{e}" y="{n}" end_time="00:00:05"/><leg mode="pt"/>
    <activity type="work" x="{e2}" y="{n2}"/></plan></person></population>'''))
        cfg = config(root, inp, include_transit=True, transit_providers=['bus'], include_rail=True,
                     unsupported_mode='replan', mc_included_modes=['bus'], mode_choice_strategy='raptor',
                     train_interval=20, max_trains_per_line=1,
                     include_signals=args.signals, signal_cycle_duration=20)
        cfg.duration = 240
        cfg.start_real_time = '2026-10-27T00:00:00'
        g.HybridInputGenerator(cfg).run()
        data = cfg.output_dir/'data'
        def actors(base):
            return [a for f in data.glob(base+'_*.json') for a in json.loads(f.read_text())]
        people, buses = actors('persons'), actors('buses')
        expected = {
            'persons': len(people),
            'person_trips': sum(sum(e['kind'] in ('PrivateVehicleLeg','PendingDecision') for e in p['data']['content']['originalPlan']) for p in people),
            'car_journeys': sum(sum(e['kind']=='PrivateVehicleLeg' for e in p['data']['content']['originalPlan']) for p in people),
            'bus_activations': sorted(a['data']['content']['startTick'] for a in buses),
            'train_activations': 2,
            'bus_passengers_boarded': 1,
        }
        (root/'expected.json').write_text(json.dumps(expected,indent=2))
        (root/'signal_checks.json').write_text(json.dumps({'enabled': args.signals}))
        htc = args.htc_root.resolve()
        jar = args.jar or htc/'target/scala-3.3.5/hyperbolic-time-chamber-3.2.0.jar'
        command = [sys.executable, str(htc/'scripts/scenario/run_cluster_validation.py'), '--jar', str(jar),
                   '--simulation', str(cfg.output_dir/'simulation.json'), '--output', str(root/'runtime'),
                   '--base-port', str(args.base_port), '--nodes', '1', '--timeout', '90']
        (root/'command.json').write_text(json.dumps(command,indent=2))
        subprocess.run(command,check=True)
    rows = [json.loads(line) for path in (root/'runtime/reports').rglob('*.jsonl') for line in path.read_text().splitlines()]
    records = [r['data'] for r in rows]
    observed = {
        'persons': sum(d.get('event_type')=='schedule_complete' for d in records),
        'person_trips': sum(d.get('total_trips',0) for d in records if d.get('event_type')=='schedule_complete'),
        'car_journeys': sum(d.get('event_type')=='sumo_tripinfo' and d.get('vehicle_type')=='car' and d.get('arrival',-1)>=0 for d in records),
        'bus_activations': sorted(d['tick'] for d in records if d.get('event_type')=='journey_started' and 'bus_id' in d),
        'train_activations': sum(d.get('event_type')=='sumo_tripinfo' and d.get('vehicle_type')=='subway' for d in records),
        'bus_passengers_boarded': sum(d.get('passengers_loaded',0) for d in records if d.get('event_type')=='bus_load_passengers'),
    }
    failures = [key for key in expected if expected[key]!=observed[key]]
    engine = json.loads((root/'runtime/result.json').read_text())
    if not engine['engineCompleted'] or engine['runtimeFailed'] or any(engine['engineErrors']):
        failures.append('engine_completion')
    if any(d.get('event_type')=='trip_aborted' for d in records):
        failures.append('trip_aborted')
    if not any(d.get('event_type')=='subway_arrival' for d in records):
        failures.append('subway_arrival')
    signal_checks = root/'signal_checks.json'
    if signal_checks.exists() and json.loads(signal_checks.read_text())['enabled']:
        for event_type in ('signal_phase_change', 'signal_state_requested'):
            if not any(d.get('event_type') == event_type for d in records):
                failures.append(event_type)
    result = {'expected':expected,'observed':observed,'failed_checks':failures,'passed':not failures,
              'scope':'One JVM, conservative discrete-event, MESO, seed 42; circulating transit is horizon-censored.'}
    (root/'outcomes.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    return bool(failures)


if __name__=='__main__':
    sys.exit(main())
