"""Where the map data comes from.

All sources return a list of elements in the Overpass `out geom` shape:
  way:      {"type": "way", "tags": {...}, "geometry": [{"lat", "lon"}, ...]}
  relation: {"type": "relation", "tags": {...},
             "members": [{"type": "way", "role": "outer", "geometry": [...]}, ...]}

Order of online attempts: several Overpass servers, then the main OSM API.
Offline: a local .osm (XML) or .osm.pbf file.
"""
import math
import re
import xml.etree.ElementTree as ET

import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OSM_API = "https://api.openstreetmap.org/api/0.6/map"
OSM_API_TILE_DEG = 0.02     # main API: max 0.25 deg² and 50 000 nodes per call
OSM_API_MAX_REQUESTS = 100  # be polite; beyond that use a file
OVERPASS_TILE_DEG = 0.1     # ~11 × 6 km in mid latitudes
OVERPASS_MIN_TILE_DEG = 0.025
USER_AGENT = "CityMap3D/1.0 (personal 3D-print utility; +https://github.com/dkuriab/mapcutter)"

ROAD_CLASSES = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "residential",
    "unclassified", "service", "living_street", "pedestrian",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}
WATERWAYS = {"river", "canal", "stream", "riverbank"}


class SourceError(Exception):
    def __init__(self, msg, too_big=False):
        super().__init__(msg)
        self.too_big = too_big  # a smaller area might work


def wanted(tags):
    return (tags.get("highway") in ROAD_CLASSES
            or tags.get("waterway") in WATERWAYS
            or tags.get("natural") == "water")


def wrap_lon(x):
    return (x + 180) % 360 - 180


def normalize_bbox(west, south, east, north):
    """Leaflet gives longitudes like 397.6 after panning around the globe;
    Overpass answers those with 400 Bad Request."""
    w, e = wrap_lon(west), wrap_lon(east)
    if e <= w:
        raise SourceError("Область пересекает линию смены дат — выдели её заново")
    s, n = max(-85.0, south), min(85.0, north)
    return w, s, e, n


def _short(text, n=300):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:n]


# ---------------------------------------------------------------- Overpass

def overpass_query(south, west, north, east, roads=None):
    bb = f"({south:.7f},{west:.7f},{north:.7f},{east:.7f})"
    hw = "|".join(sorted(roads or ROAD_CLASSES))
    ww = "|".join(sorted(WATERWAYS))
    return (
        "[out:json][timeout:120];\n(\n"
        f'  way["highway"~"^({hw})$"]{bb};\n'
        f'  way["waterway"~"^({ww})$"]{bb};\n'
        f'  way["natural"="water"]{bb};\n'
        f'  relation["natural"="water"]{bb};\n'
        f'  relation["waterway"="riverbank"]{bb};\n'
        ");\nout geom;\n"
    )


def split_bbox(south, west, north, east, step):
    nx = max(1, math.ceil((east - west) / step - 1e-9))
    ny = max(1, math.ceil((north - south) / step - 1e-9))
    dx, dy = (east - west) / nx, (north - south) / ny
    return [(south + j * dy, west + i * dx, south + (j + 1) * dy, west + (i + 1) * dx)
            for j in range(ny) for i in range(nx)]


def quarters(t):
    s, w, n, e = t
    ms, mw = (s + n) / 2, (w + e) / 2
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def merge_elements(chunks):
    """Tiles overlap at the edges: keep each OSM object once."""
    seen, out = set(), []
    for chunk in chunks:
        for el in chunk:
            key = (el.get("type"), el.get("id"))
            if key[1] is not None:
                if key in seen:
                    continue
                seen.add(key)
            out.append(el)
    return out


def fetch_overpass(south, west, north, east, log=print, roads=None):
    """Big areas are downloaded tile by tile; a tile that times out is split in four."""
    servers = list(OVERPASS_URLS)
    tiles = split_bbox(south, west, north, east, OVERPASS_TILE_DEG)
    chunks, done = [], 0

    def one(tile):
        q = overpass_query(*tile, roads=roads)
        errs, too_big = [], False
        for url in list(servers):
            host = url.split("/")[2]
            try:
                r = requests.post(url, data={"data": q}, timeout=200,
                                  headers={"User-Agent": USER_AGENT})
                if r.status_code != 200:
                    raise SourceError(f"HTTP {r.status_code}: {_short(r.text)}",
                                      too_big=r.status_code in (504,))
                data = r.json()
                remark = data.get("remark", "")
                if "runtime error" in remark:  # timeout / out of memory on the server
                    raise SourceError(_short(remark), too_big=True)
                # the server that answered goes first next time
                servers.remove(url)
                servers.insert(0, url)
                return data["elements"]
            except Exception as e:
                log(f"  {host}: {e}")
                errs.append(f"{host}: {e}")
                too_big |= getattr(e, "too_big", False) or isinstance(e, requests.Timeout)
        raise SourceError("; ".join(errs), too_big=too_big)

    queue = list(tiles)
    while queue:
        tile = queue.pop(0)
        done += 1
        if len(tiles) > 1:
            log(f"Overpass: часть {done} из {done + len(queue)}…")
        else:
            log("Overpass…")
        try:
            chunks.append(one(tile))
        except SourceError as e:
            if e.too_big and tile[2] - tile[0] > OVERPASS_MIN_TILE_DEG:
                log("  не вышло, делю этот кусок на 4 части")
                queue[:0] = quarters(tile)
                continue
            raise SourceError(str(e))
    return merge_elements(chunks)


# ---------------------------------------------------------------- main OSM API

def fetch_osm_api(south, west, north, east, log=print):
    """Main OSM API returns everything (buildings too), so tiles must be small.
    A tile with too many nodes (HTTP 400) is split in four."""
    queue = split_bbox(south, west, north, east, OSM_API_TILE_DEG)
    if len(queue) > OSM_API_MAX_REQUESTS:
        raise SourceError(f"область слишком большая для основного API OSM "
                          f"({len(queue)} запросов, максимум {OSM_API_MAX_REQUESTS})")
    chunks, calls = [], 0
    while queue:
        s, w, n, e = queue.pop(0)
        calls += 1
        if calls > OSM_API_MAX_REQUESTS:
            raise SourceError("слишком много запросов к основному API OSM")
        log(f"Основной API OpenStreetMap: часть {calls} из {calls + len(queue)}…")
        r = requests.get(OSM_API, params={"bbox": f"{w:.7f},{s:.7f},{e:.7f},{n:.7f}"},
                         timeout=180, headers={"User-Agent": USER_AGENT})
        if r.status_code == 400 and n - s > OSM_API_TILE_DEG / 8:
            queue[:0] = quarters((s, w, n, e))
            continue
        if r.status_code != 200:
            raise SourceError(f"OSM API HTTP {r.status_code}: {_short(r.text)}")
        chunks.append(parse_osm_xml(r.content))
    return merge_elements(chunks)


# ---------------------------------------------------------------- files

def _assemble(nodes, ways, relations):
    """nodes: id->(lon,lat); ways: id->(node ids, tags); relations: list of (id, members, tags)."""
    def geom(nids):
        return [{"lon": nodes[i][0], "lat": nodes[i][1]} for i in nids if i in nodes]

    out = []
    for wid, (nids, tags) in ways.items():
        if wanted(tags):
            g = geom(nids)
            if len(g) >= 2:
                out.append({"type": "way", "id": wid, "tags": tags, "geometry": g})
    for rid, members, tags in relations:
        if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
            ms = []
            for ref, role in members:
                if ref in ways:
                    g = geom(ways[ref][0])
                    if len(g) >= 2:
                        ms.append({"type": "way", "role": role, "geometry": g})
            if ms:
                out.append({"type": "relation", "id": rid, "tags": tags, "members": ms})
    return out


def parse_osm_xml(data):
    """OSM XML (openstreetmap.org Export button, main API, JOSM, BBBike .osm)."""
    nodes, ways, relations = {}, {}, []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise SourceError(f"Не получилось прочитать .osm: {e}")
    for el in root:
        if el.tag == "node":
            nodes[el.get("id")] = (float(el.get("lon")), float(el.get("lat")))
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
            ways[el.get("id")] = ([nd.get("ref") for nd in el.iter("nd")], tags)
        elif el.tag == "relation":
            tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
            members = [(m.get("ref"), m.get("role") or "outer")
                       for m in el.iter("member") if m.get("type") == "way"]
            relations.append((el.get("id"), members, tags))
    return _assemble(nodes, ways, relations)


def parse_osm_pbf(path, bbox=None):
    """.osm.pbf (Geofabrik, slice.openstreetmap.us, BBBike). Needs `pip install osmium`."""
    try:
        import osmium
    except ImportError:
        raise SourceError("Для .pbf нужен пакет osmium: .venv/bin/pip install osmium")

    west, south, east, north = bbox if bbox else (-180, -90, 180, 90)
    # widen a bit so roads that cross the edge keep their outside nodes
    pad = 0.01
    inside = lambda lon, lat: (west - pad <= lon <= east + pad and south - pad <= lat <= north + pad)

    class Rels(osmium.SimpleHandler):
        """pass 1: water relations and the ids of their member ways"""
        def __init__(self):
            super().__init__()
            self.relations, self.member_ids = [], set()

        def relation(self, r):
            tags = {t.k: t.v for t in r.tags}
            if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
                members = [(str(m.ref), m.role or "outer") for m in r.members if m.type == "w"]
                self.relations.append((str(r.id), members, tags))
                self.member_ids.update(ref for ref, _ in members)

    class H(osmium.SimpleHandler):
        """pass 2: wanted ways inside the box + all members of water relations"""
        def __init__(self, member_ids):
            super().__init__()
            self.ways, self.member_ids = {}, member_ids

        def way(self, w):
            wid = str(w.id)
            tags = {t.k: t.v for t in w.tags}
            is_member = wid in self.member_ids
            if not (wanted(tags) or is_member):
                return
            pts = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
            if not pts or not (is_member or any(inside(x, y) for x, y in pts)):
                return
            self.ways[wid] = (pts, tags)

    rels = Rels()
    rels.apply_file(str(path))
    h = H(rels.member_ids)
    h.apply_file(str(path), locations=True)
    nodes, ways = {}, {}
    for wid, (pts, tags) in h.ways.items():
        ids = []
        for k, p in enumerate(pts):
            nid = f"{wid}:{k}"
            nodes[nid] = p
            ids.append(nid)
        ways[wid] = (ids, tags)
    return _assemble(nodes, ways, rels.relations)


def load_file(path, bbox=None):
    p = str(path).lower()
    if p.endswith(".pbf"):
        return parse_osm_pbf(path, bbox)
    if p.endswith(".bz2"):
        import bz2
        with bz2.open(path, "rb") as f:
            return parse_osm_xml(f.read())
    with open(path, "rb") as f:
        return parse_osm_xml(f.read())


# ---------------------------------------------------------------- all online sources

def fetch_online(south, west, north, east, log=print, roads=None):
    """roads: highway classes to ask Overpass for (fewer = faster)."""
    errors = []
    try:
        return fetch_overpass(south, west, north, east, log, roads)
    except SourceError as e:
        errors.append(f"Overpass — {e}")
    try:
        return fetch_osm_api(south, west, north, east, log)
    except Exception as e:
        errors.append(f"OSM API — {e}")
    raise SourceError(
        "Не удалось скачать карту. " + " | ".join(errors) +
        " | Можно скачать файл .osm (кнопка «Экспорт» на openstreetmap.org) "
        "или .osm.pbf (download.geofabrik.de) и загрузить его на странице.")
