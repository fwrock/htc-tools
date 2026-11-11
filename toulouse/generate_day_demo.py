#!/usr/bin/env python3
"""Full-day extension of the 100-link Toulouse demo with deliberate hourly coverage."""
import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import networkx as nx

HERE=Path(__file__).resolve().parent


def generate(output, scenario='toulouse_center_day', link_count=100, demand_scale=1):
    if link_count < 100 or demand_scale < 1:
        raise ValueError('links must be at least 100 and demand-scale positive')
    base_name='toulouse_center_demo' if link_count==100 else f'toulouse_center_{link_count}_links'
    base=HERE/'output'/base_name
    network=base.parent/(base_name+'-sources/network.xml')
    if not network.exists():
        subprocess.run([sys.executable,str(HERE/'generate_center_demo.py'),'--scenario',base_name,'--links',str(link_count)],check=True)
    target=output/scenario
    sources=output/(scenario+'-sources')
    if target.exists() or sources.exists():
        raise ValueError(f'Refusing to replace existing output for {scenario}')
    tree=ET.parse(network)
    nodes={n.get('id'):n.attrib for n in tree.findall('.//node')}
    links=tree.findall('.//link')
    graph=nx.DiGraph()
    for link in links:
        graph.add_edge(link.get('from'),link.get('to'),id=link.get('id'),weight=float(link.get('length'))/float(link.get('freespeed')))
    paths=[p for _, targets in nx.all_pairs_dijkstra_path(graph) for p in targets.values() if len(p)>1]
    edges=[{graph.edges[a,b]['id'] for a,b in zip(p,p[1:])} for p in paths]
    uncovered={l.get('id') for l in links}
    cover=[]
    while uncovered:
        index=max(range(len(paths)),key=lambda i:len(edges[i]&uncovered))
        if not edges[index]&uncovered:
            raise ValueError(f'Links cannot be covered by multi-link shortest paths: {uncovered}')
        cover.append(paths[index]);uncovered-=edges[index]
    sources.mkdir(parents=True)
    shutil.copy2(network,sources/'network.xml')
    pop=ET.Element('population')
    demand=[]
    rng=random.Random(42)
    # All mandatory car routes run in every hour, with additional morning/evening demand.
    for hour in range(24):
        copies=(8 if hour in (8,18) else 4 if hour in (7,9,17,19)
                else 3 if hour in (12,13) else 1 if hour<6 or hour>=22 else 2)
        copies *= demand_scale
        for route_index,path in enumerate(cover):
            for copy in range(copies):
                departure=hour*3600+30+route_index*20+copy*(240 if demand_scale==1 else max(2,1800//copies))
                demand.append(('car',path[0],path[-1],departure,f'h{hour:02d}_r{route_index}_c{copy}'))
        short=[p for p in paths if 2<=len(p)-1<=3]
        for mode in ('walk','bike'):
            for copy in range(2*demand_scale):
                path=rng.choice(short)
                demand.append((mode,path[0],path[-1],hour*3600+120+copy*(900 if demand_scale==1 else max(2,2400//(2*demand_scale))),f'h{hour:02d}_{mode}_{copy}'))
    for mode,a,b,departure,pid in sorted(demand,key=lambda d:(d[3],d[4])):
        person=ET.SubElement(pop,'person',id='day_'+pid)
        plan=ET.SubElement(person,'plan',selected='yes')
        ET.SubElement(plan,'activity',type='demo',x=nodes[a]['x'],y=nodes[a]['y'],end_time=str(departure))
        ET.SubElement(plan,'leg',mode=mode,dep_time=str(departure))
        ET.SubElement(plan,'activity',type='demo',x=nodes[b]['x'],y=nodes[b]['y'])
    ET.ElementTree(pop).write(sources/'population.xml',encoding='utf-8',xml_declaration=True)
    subprocess.run([sys.executable,str(HERE/'generate_hybrid_input.py'),'--scenario',scenario,
        '--input-dir',str(sources),'--network-file',str(sources/'network.xml'),'--population-file',str(sources/'population.xml'),
        '--output',str(output),'--sample','1','--duration','86400','--seed','42','--no-transit','--no-rail',
        '--unsupported-mode','error','--no-reuse-map','--docker-base','/scenarios'],check=True)
    # Source node/link ordering is unchanged, preserving IDs of the four existing signal controllers.
    import generate_hybrid_input as g
    from scenario_common.traffic_signals import build_signal_actor,wire_signal_nodes
    from types import SimpleNamespace
    ids=g.ScenarioIdRegistry();ids.load(target/'data/id_registry.json')
    provenance=json.loads((base/'demo-provenance.json').read_text())
    cfg=SimpleNamespace(signal_cycle_duration=40,signal_yellow_duration=3,signal_all_red_duration=1,signal_green_ratio=.5)
    signals=[]
    for i,node in enumerate(provenance['signal_source_nodes']):
        incoming=sorted(l.get('id') for l in links if l.get('to')==node)
        signals.append(build_signal_actor(cfg,ids,random.Random(42+i),node,incoming))
    (target/'data/traffic_signals_0.json').write_text(json.dumps(signals))
    wire_signal_nodes(target/'data');ids.save(target/'data/id_registry.json')
    manifest=json.loads((target/'simulation.json').read_text())
    manifest['name']=f'Toulouse centre · full day · {link_count} links · traffic ×{demand_scale}'
    manifest['description']='Synthetic hourly multimodal demand on real Toulouse streets; mandatory car routes cover all retained links every hour; 24 hours, MESO.'
    signal=dict(manifest['actorsDataSources'][0]);signal.update(id='traffic_signals_0',classType='hybrid.actor.TrafficSignal')
    signal['dataSource']={'sourceType':'json','info':{'path':f'/scenarios/{scenario}/data/traffic_signals_0.json'}}
    manifest['actorsDataSources'].append(signal)
    (target/'simulation.json').write_text(json.dumps(manifest,indent=2)+'\n')
    counts={mode:sum(d[0]==mode for d in demand) for mode in ('car','bike','walk')}
    provenance.update(demand_profile='hourly-shortest-path-coverage',horizon_seconds=86400,persons=len(demand),
        cars=counts['car'],bikes=counts['bike'],pedestrians=counts['walk'],
        hourly_cover_routes=cover,hourly_coverage_expected_links=link_count,directed_links=link_count,demand_scale=demand_scale,
        car_departures_by_hour={str(h):sum(d[0]=='car' and d[3]//3600==h for d in demand) for h in range(24)},
        peak_hours=[8,18],
        series_contract={'series':link_count,'samples_per_series':288,'sample_period_seconds':300,
                         'value':'time-weighted mean car occupancy; completed, initially empty run'},
        parent_scenario=base_name)
    (target/'demo-provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
    metadata=json.loads((target/'scenario_metadata.json').read_text())
    metadata['demoSignals']={'count':4,'cycleSeconds':40,'sites':provenance['signal_source_nodes'],'source':'synthetic demo sites'}
    metadata['demoDemand']={'carDeparturesByHour':provenance['car_departures_by_hour'],'peakHours':[8,18],'seriesContract':provenance['series_contract']}
    (target/'scenario_metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    with (target/'GENERATION_REPORT.md').open('a') as report:
        report.write('\n## Full-day demo additions\nFour synthetic traffic lights with 40-second cycles. Mandatory car routes cover every link each hour, with 08:00 and 18:00 demand peaks. SimEDaPE uses one daily series per link, integrating occupancy into 288 five-minute means.\n')
    print(json.dumps({'scenario':str(target),'counts':counts,'cover_routes':len(cover),'series_target':f'{link_count} × 288'}))
    return target


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=HERE/'output')
    parser.add_argument('--scenario',default='toulouse_center_day')
    parser.add_argument('--links',type=int,default=100)
    parser.add_argument('--demand-scale',type=int,default=1)
    args=parser.parse_args()
    generate(args.output.resolve(),args.scenario,args.links,args.demand_scale)
