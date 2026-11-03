#!/usr/bin/env python3
"""Download and cache GTFS feeds and a complete OSM rail relation extract."""
import argparse
import gzip
import hashlib
import json
import shutil
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SPTRANS_URL = "https://www.sptrans.com.br/umbraco/Surface/PerfilDesenvolvedor/BaixarGTFS?memberName=sptrans"
REQUIRED = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "agency.txt"}
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, destination, data=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Downloads require HTTPS; a local GTFS ZIP is also accepted")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "HTC-SaoPaulo-Scenario/2.0"})
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as out:
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > MAX_DOWNLOAD:
                raise ValueError("Download exceeds 512 MiB")
            out.write(block)


def install_gtfs(archive, target):
    """Flatten only GTFS text tables, never arbitrary ZIP paths."""
    target.mkdir()
    with zipfile.ZipFile(archive) as z:
        tables = {}
        total = 0
        for info in z.infolist():
            if info.is_dir() or Path(info.filename).suffix.lower() != ".txt":
                continue
            name = Path(info.filename).name
            if name in tables:
                raise ValueError(f"Ambiguous GTFS table {name} in ZIP")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise ValueError("GTFS archive exceeds 2 GiB unpacked")
            tables[name] = info
        missing = REQUIRED - tables.keys()
        if missing or not ({"calendar.txt", "calendar_dates.txt"} & tables.keys()):
            raise ValueError(f"Invalid GTFS ZIP: missing tables {sorted(missing)} or service calendar")
        for name, info in tables.items():
            with z.open(info) as src, (target / name).open("wb") as out:
                shutil.copyfileobj(src, out)


def rail_query(bbox):
    south, west, north, east = bbox
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError("bbox must be south,west,north,east in WGS84 degrees")
    bounds = ",".join(str(v) for v in bbox)
    # Recursive members retain tracks and stop nodes outside the selection box.
    return f'[out:xml][timeout:150];relation["type"="route"]["route"~"^(subway|train|light_rail|monorail)$"]({bounds});(._;>>;);out body;'


def local_rail_extract(source, destination):
    """Keep railway ways, rail route relations and their node members in two passes."""
    source = Path(source)
    if source.suffix == ".pbf":
        raise ValueError("Convert PBF to XML first: osmium cat input.osm.pbf -o input.osm")
    opener = gzip.open if source.suffix == ".gz" else open
    ways, relations, wanted = [], [], set()
    with opener(source, "rb") as stream:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        for event, elem in context:
            if event != "end" or elem.tag not in ("node", "way", "relation", "bounds"):
                continue
            if elem.tag != "node":
                tags = {t.get("k"): t.get("v") for t in elem.findall("tag")}
                if elem.tag == "way" and tags.get("railway") in ("subway", "rail", "light_rail", "monorail"):
                    ways.append(ET.tostring(elem))
                    wanted.update(n.get("ref") for n in elem.findall("nd"))
                elif elem.tag == "relation" and tags.get("type") == "route" and tags.get("route") in ("subway", "train", "light_rail", "monorail"):
                    relations.append(ET.tostring(elem))
                    wanted.update(m.get("ref") for m in elem.findall("member") if m.get("type") == "node")
            root.remove(elem)
    if not relations:
        raise ValueError("Local OSM has no rail route relations")
    with destination.open("wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?><osm version="0.6" generator="HTC">\n')
        with opener(source, "rb") as stream:
            context = ET.iterparse(stream, events=("start", "end"))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or elem.tag not in ("node", "way", "relation", "bounds"):
                    continue
                if elem.tag == "node" and elem.get("id") in wanted:
                    out.write(ET.tostring(elem))
                root.remove(elem)
        for elem in ways + relations:
            out.write(elem)
        out.write(b"</osm>")


def cached(target, refresh):
    if target.exists() and not refresh:
        print(f"Cached: {target} (use --refresh to replace)")
        return True
    return False


def replace(staged, target, refresh):
    if target.exists():
        if not refresh:
            raise FileExistsError(target)
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            raise FileExistsError(f"Previous backup exists: {backup}")
        target.rename(backup)
        try:
            staged.rename(target)
        except BaseException:
            backup.rename(target)
            raise
        if backup.is_dir():
            shutil.rmtree(backup)
        else:
            backup.unlink()
    else:
        staged.rename(target)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", type=Path, default=Path("input"))
    p.add_argument("--sptrans", action="store_true", help="Fetch public SPTrans GTFS endpoint")
    p.add_argument("--gtfs", action="append", default=[], metavar="PROVIDER=URL_OR_ZIP")
    p.add_argument("--rail", action="store_true", help="Fetch OSM rail relations and all members")
    p.add_argument("--rail-from-osm", type=Path, help="Extract rail from local OSM XML/XML.gz instead of Overpass")
    p.add_argument("--bbox", default="-24.1,-47.1,-23.2,-46.1", help="OSM south,west,north,east")
    p.add_argument("--overpass-url", default="https://overpass-api.de/api/interpreter")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()
    if not (args.sptrans or args.gtfs or args.rail or args.rail_from_osm):
        p.error("select --sptrans, --gtfs and/or --rail")
    args.input_dir.mkdir(parents=True, exist_ok=True)
    feeds = list(args.gtfs)
    if args.sptrans:
        feeds.append("sptrans=" + SPTRANS_URL)
    try:
        for specification in feeds:
            provider, source = specification.split("=", 1)
            if not provider or Path(provider).name != provider or provider in (".", ".."):
                raise ValueError("provider must be a single directory name")
            target = args.input_dir / provider
            if cached(target, args.refresh):
                continue
            with tempfile.TemporaryDirectory(prefix=".gtfs-", dir=args.input_dir) as temp:
                temp = Path(temp)
                archive = temp / "feed.zip"
                if source.startswith("https://"):
                    download(source, archive)
                else:
                    shutil.copyfile(source, archive)
                staged = temp / provider
                install_gtfs(archive, staged)
                (staged / "source.json").write_text(json.dumps({
                    "source": source, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "archive_sha256": sha256(archive),
                    "tables": {f.name: sha256(f) for f in sorted(staged.glob("*.txt"))},
                }, indent=2))
                replace(staged, target, args.refresh)
                print(f"GTFS ready: {target}")
        if args.rail or args.rail_from_osm:
            target = args.input_dir / "rail.osm"
            if not cached(target, args.refresh):
                bbox = tuple(float(v) for v in args.bbox.split(","))
                if len(bbox) != 4:
                    raise ValueError("bbox requires four numbers")
                query = rail_query(bbox)
                with tempfile.TemporaryDirectory(prefix=".rail-", dir=args.input_dir) as temp:
                    staged = Path(temp) / "rail.osm"
                    if args.rail_from_osm:
                        local_rail_extract(args.rail_from_osm, staged)
                    else:
                        download(args.overpass_url, staged, urllib.parse.urlencode({"data": query}).encode())
                    root = ET.parse(staged).getroot()
                    if root.tag != "osm" or root.find("remark") is not None or root.find("relation") is None:
                        raise ValueError("Overpass returned an error or no rail relations; existing extract preserved")
                    metadata = {"source": str(args.rail_from_osm.resolve()) if args.rail_from_osm else args.overpass_url,
                                "selection": "all active rail ways and rail route relations" if args.rail_from_osm else query,
                                "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": sha256(staged)}
                    replace(staged, target, args.refresh)
                    target.with_suffix(".source.json").write_text(json.dumps(metadata, indent=2))
                    print(f"OSM rail ready: {target}")
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        p.exit(1, f"ERROR: {exc}\nObtain restricted feeds through the operator portal and pass --gtfs PROVIDER=/path/feed.zip.\n")


if __name__ == "__main__":
    main()
