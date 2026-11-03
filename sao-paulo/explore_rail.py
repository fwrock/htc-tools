#!/usr/bin/env python3
"""Inspect OSM route relation IDs and ordered rail stops before choosing variants."""
import argparse
import json
from pathlib import Path
from generate_hybrid_input import Config, OsmRailExtractor


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--osm-file',type=Path,default=Path('input/sao_paulo.osm.gz'))
    p.add_argument('--output',type=Path,help='Optional audit JSON; existing files are not overwritten')
    args=p.parse_args()
    if args.output and args.output.exists():
        p.error(f'Output already exists: {args.output}')
    cfg=Config(input_dir=args.osm_file.parent,output_dir=Path('output'),scenario_name='rail_audit')
    rail=OsmRailExtractor(cfg)
    rail.extract(args.osm_file)
    audit={'source':str(args.osm_file.resolve()),'stations':rail.station_nodes,
           'relations':rail.route_relations,'way_count':len(rail.railway_ways)}
    for relation in rail.route_relations:
        print(f"{relation['route']}_{relation['ref']}: relation {relation['osm_id']}, "
              f"{len(relation['ordered_stations'])} stops, {relation['name']}")
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(audit,indent=2,ensure_ascii=False))
        print(f'Audit saved: {args.output}')


if __name__=='__main__':
    main()
