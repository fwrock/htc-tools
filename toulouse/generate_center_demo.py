#!/usr/bin/env python3
"""Build a bounded demo from real Toulouse links and synthetic multimodal demand."""
import argparse
from collections import defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import sys
import xml.etree.ElementTree as ET

import networkx as nx
from pyproj import Transformer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def generate(output, scenario='toulouse_center_demo', network=None, link_count=100):
    network = network or HERE / 'input/toulouse_network.xml.gz'
    target = output / scenario
    if target.exists():
        raise ValueError(f'Refusing to replace existing scenario: {target}')
    project = Transformer.from_crs(4326, 2154, always_xy=True)
    cx, cy = project.transform(1.444, 43.6045)  # Place du Capitole
    nodes, links = {}, {}
    with gzip.open(network, 'rb') as source:
        for _, elem in ET.iterparse(source, events=('end',)):
            if elem.tag == 'node':
                x, y = float(elem.attrib['x']), float(elem.attrib['y'])
                if math.hypot(x-cx, y-cy) <= 1500:
                    nodes[elem.attrib['id']] = dict(elem.attrib)
            elif elem.tag == 'link':
                a, b = elem.attrib['from'], elem.attrib['to']
                if a in nodes and b in nodes and a != b and 'car' in elem.attrib.get('modes', '').split(','):
                    links.setdefault((a, b), dict(elem.attrib))
            elem.clear()
    road = nx.DiGraph()
    road.add_weighted_edges_from((a, b, float(link['length'])) for (a, b), link in links.items())
    if link_count > 100:
        for a,b in road.edges:
            road.edges[a,b]['weight']=float(links[a,b]['length'])/float(links[a,b]['freespeed'])
        dominated=[(a,b) for a,b in road.edges if nx.shortest_path_length(road,a,b,weight='weight') < road.edges[a,b]['weight']-1e-8]
        road.remove_edges_from(dominated)
    components = [c for c in nx.strongly_connected_components(road) if road.subgraph(c).number_of_edges() >= link_count]
    distance = lambda n: math.hypot(float(nodes[n]['x'])-cx, float(nodes[n]['y'])-cy)
    if not components:
        raise ValueError(f'No central strongly connected component: {len(nodes)} nodes, {len(links)} links')
    component = min(components, key=lambda c: min(distance(n) for n in c))
    road = road.subgraph(component).copy()
    root = min(road, key=lambda n: (distance(n), n))
    _, forward = nx.single_source_dijkstra(road, root)
    _, backward = nx.single_source_dijkstra(road.reverse(), root)
    selected = set()
    for n in sorted(road, key=lambda n: (distance(n), n)):
        paths = [forward[n], list(reversed(backward[n]))]
        edges = {(a, b) for path in paths for a, b in zip(path, path[1:])}
        if len(selected | edges) > link_count:
            continue
        selected |= edges
        selected_nodes = {v for e in selected for v in e}
        available = [(a,b) for a,b in road.edges if a in selected_nodes and b in selected_nodes and (a,b) not in selected]
        if len(selected) + len(available) >= link_count:
            selected.update(sorted(available, key=lambda e: (max(map(distance,e)),e))[:link_count-len(selected)])
            break
    if len(selected) != link_count:
        raise ValueError(f'Cannot extract exactly {link_count} strongly connected links; got {len(selected)}')
    chosen = [links[e] for e in sorted(selected)]
    graph = nx.DiGraph()
    graph.add_weighted_edges_from((v['from'], v['to'], float(v['length'])) for v in chosen)
    assert len(chosen) == link_count and nx.is_strongly_connected(graph)
    sources = output / f'{scenario}-sources'
    sources.mkdir(parents=True, exist_ok=False)
    net = ET.Element('network')
    ns, ls = ET.SubElement(net, 'nodes'), ET.SubElement(net, 'links')
    for n in sorted(selected_nodes):
        ET.SubElement(ns, 'node', nodes[n])
    for link in sorted(chosen, key=lambda l: l['id']):
        ET.SubElement(ls, 'link', link)
    ET.ElementTree(net).write(sources/'network.xml', encoding='utf-8', xml_declaration=True)
    pop = ET.Element('population')
    rng = random.Random(42)
    pairs = [(a, b) for a in sorted(graph) for b, d in nx.single_source_dijkstra_path_length(graph, a, cutoff=230).items() if 80 <= d <= 230]
    if not pairs:
        raise ValueError('No bounded reachable demo trips')
    for mode in ('walk', 'bike', 'car'):
        for i in range(30):
            a, b = rng.choice(pairs)
            departure = 5 + i * 10
            person = ET.SubElement(pop, 'person', id=f'demo_{mode}_{i:02d}')
            plan = ET.SubElement(person, 'plan', selected='yes')
            for node, end, leg in ((a, departure, mode), (b, None, None)):
                attr = {'type': 'demo', 'x': nodes[node]['x'], 'y': nodes[node]['y']}
                if end is not None:
                    attr['end_time'] = str(end)
                ET.SubElement(plan, 'activity', attr)
                if leg:
                    ET.SubElement(plan, 'leg', mode=leg, dep_time=str(end))
    ET.ElementTree(pop).write(sources/'population.xml', encoding='utf-8', xml_declaration=True)
    subprocess.run([sys.executable, str(HERE/'generate_hybrid_input.py'), '--scenario', scenario,
                    '--input-dir', str(sources), '--network-file', str(sources/'network.xml'),
                    '--population-file', str(sources/'population.xml'), '--output', str(output),
                    '--sample', '1', '--duration', '900', '--seed', '42', '--no-transit', '--no-rail',
                    '--unsupported-mode', 'error', '--no-reuse-map', '--docker-base', '/scenarios'], check=True)
    # Use the shared signal builder directly: these are demo sites, not observed OSM signals.
    import generate_hybrid_input as g
    from scenario_common.traffic_signals import build_signal_actor, wire_signal_nodes
    data = target/'data'
    ids = g.ScenarioIdRegistry()
    ids.load(data/'id_registry.json')
    incoming = defaultdict(list)
    for link in chosen:
        incoming[link['to']].append(link['id'])
    sites = sorted(selected_nodes, key=lambda n: (-graph.in_degree(n), distance(n), n))[:4]
    from types import SimpleNamespace
    cfg = SimpleNamespace(signal_cycle_duration=40, signal_yellow_duration=3, signal_all_red_duration=1, signal_green_ratio=0.5)
    signals = [build_signal_actor(cfg, ids, random.Random(42+i), n, sorted(incoming[n])) for i, n in enumerate(sites)]
    write_json(data/'traffic_signals_0.json', signals)
    wire_signal_nodes(data)
    ids.save(data/'id_registry.json')
    manifest = json.loads((target/'simulation.json').read_text())
    manifest['name'] = f'Toulouse centre · {link_count} links · walk, bike, car + lights'
    manifest['description'] = 'Real directed MATSim streets near Capitole; synthetic 90-person demand and four 40-second signal controllers. Fifteen simulated minutes; MESO for fast demonstration.'
    source = dict(manifest['actorsDataSources'][0])
    source.update(id='traffic_signals_0', classType='hybrid.actor.TrafficSignal')
    source['dataSource'] = {'sourceType':'json', 'info':{'path':f'/scenarios/{scenario}/data/traffic_signals_0.json'}}
    manifest['actorsDataSources'].append(source)
    write_json(target/'simulation.json', manifest)
    metadata=json.loads((target/'scenario_metadata.json').read_text())
    metadata['demoSignals']={'count':4,'cycleSeconds':40,'sites':sites,'source':'synthetic demo sites, not OSM observations'}
    write_json(target/'scenario_metadata.json',metadata)
    with (target/'GENERATION_REPORT.md').open('a') as report:
        report.write('\n## Demo additions\nFour synthetic traffic-light sites with 40-second cycles, wired to MESO nodes. One bounded outbound trip per person.\n')
    write_json(target/'demo-provenance.json', {'demand_profile':'single-bounded-trip', 'seed':42, 'directed_links':link_count, 'persons':90, 'cars':30, 'bikes':30,
        'pedestrians':30, 'signals':4, 'signal_cycle_seconds':40, 'horizon_seconds':900,
        'source_network':str(network), 'source_sha256':hashlib.sha256(network.read_bytes()).hexdigest(),
        'source_link_ids':[l['id'] for l in chosen], 'signal_source_nodes':sites,
        'assumptions':['Existing directed road links with original one-way restrictions; strongly connected selection, no synthesized reverse links.',
                       'Synthetic demand and signal locations/timing; shared road graph for all modes, not calibrated.',
                       'MESO; SimEDaPE evaluation covers car flow only.']})
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=HERE/'output')
    parser.add_argument('--scenario', default='toulouse_center_demo')
    parser.add_argument('--network', type=Path)
    parser.add_argument('--links', type=int, default=100)
    args = parser.parse_args()
    print(generate(args.output.resolve(), args.scenario, args.network, args.links))
