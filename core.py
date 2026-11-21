"""City Map 3D: OSM area -> relief model -> printable STL pieces with dovetail joints."""
import io
import math
import zipfile

import requests
import pyproj
import shapely
import trimesh
from shapely.geometry import box, LineString, Point, Polygon, MultiPolygon
from shapely.ops import unary_union, polygonize, linemerge, transform
from shapely.affinity import scale as shp_scale, translate, rotate

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "CityMap3D/1.0 (personal 3D-print utility)"

# road width in metres by OSM class
ROAD_WIDTH = {
    "motorway": 24, "trunk": 20, "primary": 16, "secondary": 12,
    "tertiary": 10, "residential": 7, "unclassified": 6, "service": 4,
    "living_street": 5, "pedestrian": 5,
}
# the model is allowed to be cut along these lines
CUT_ROADS = {"motorway", "trunk", "primary", "secondary"}
RIVER_WIDTH = {"river": 30, "canal": 15, "stream": 4}

# --- model dimensions, mm ---
BASE_H = 2.0       # solid base
ROAD_H = 1.0       # roads above the base
BLOCK_H = 1.2      # blocks above the roads
GAP = 0.15         # gap at seams
MIN_FEATURE = 0.8  # anything thinner is dropped (~2 x 0.4 nozzle)
EPS = 0.01         # layer overlap so boolean union fuses the layers

# --- dovetails ---
DT_NECK = 6.0
DT_HEAD = 10.0
DT_DEPTH = 7.0
DT_STEP = 60.0
DT_CLEAR = 0.2

# --- LEDs ---
LED_D = 3.0
LED_SHIFT = 4.0

BED_MARGIN = DT_DEPTH + 3  # tabs stick out of the piece, keep room for them

MAX_AREA_KM2 = 60


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- OSM

def fetch_osm(south, west, north, east):
    bb = f"({south},{west},{north},{east})"
    hw = "|".join(sorted(ROAD_WIDTH)) + "|" + "|".join(f"{k}_link" for k in CUT_ROADS)
    q = f"""
    [out:json][timeout:120];
    (
      way["highway"~"^({hw})$"]{bb};
      way["waterway"~"^(river|canal|stream|riverbank)$"]{bb};
      way["natural"="water"]{bb};
      relation["natural"="water"]{bb};
      relation["waterway"="riverbank"]{bb};
    );
    out geom;
    """
    last = None
    for url in OVERPASS_URLS:
        try:
            r = requests.post(url, data={"data": q}, timeout=180,
                              headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return r.json()["elements"]
        except Exception as e:  # try the next mirror
            last = e
    raise BuildError(f"Overpass API недоступен: {last}")


def make_projector(lat, lon):
    zone = int((lon + 180) // 6) + 1
    epsg = (32700 if lat < 0 else 32600) + zone
    t = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    return lambda geom: transform(t.transform, geom)


def _relation_polygon(el):
    outer, inner = [], []
    for m in el.get("members", []):
        g = m.get("geometry")
        if m.get("type") != "way" or not g or len(g) < 2:
            continue
        line = LineString([(p["lon"], p["lat"]) for p in g])
        (inner if m.get("role") == "inner" else outer).append(line)
    if not outer:
        return None
    poly = unary_union(list(polygonize(unary_union(outer))))
    if inner:
        poly = poly.difference(unary_union(list(polygonize(unary_union(inner)))))
    return poly if not poly.is_empty else None


def build_layers(elements, bbox_ll, proj):
    roads, cut_lines, water = [], [], []
    for el in elements:
        tags = el.get("tags", {})
        if el.get("type") == "relation":
            if tags.get("natural") == "water" or tags.get("waterway") == "riverbank":
                p = _relation_polygon(el)
                if p is not None:
                    water.append(proj(p).buffer(0))
            continue
        if "geometry" not in el:
            continue
        pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(pts) < 2:
            continue
        line = proj(LineString(pts))
        hw = tags.get("highway")
        if hw and hw.endswith("_link"):
            hw = hw[:-5]
        ww = tags.get("waterway")
        if hw in ROAD_WIDTH:
            roads.append(line.buffer(ROAD_WIDTH[hw] / 2, cap_style=2))
            if hw in CUT_ROADS:
                cut_lines.append(line)
        elif ww in RIVER_WIDTH:
            water.append(line.buffer(RIVER_WIDTH[ww] / 2))
            if ww != "stream":
                cut_lines.append(line)
        elif tags.get("natural") == "water" or ww == "riverbank":
            if len(pts) >= 4 and pts[0] == pts[-1]:
                water.append(proj(Polygon(pts)).buffer(0))
    area = proj(box(*bbox_ll))
    water_u = unary_union(water).intersection(area) if water else Polygon()
    roads_u = unary_union(roads).intersection(area).difference(water_u) if roads else Polygon()
    blocks = area.difference(roads_u).difference(water_u)
    return area, roads_u, water_u, blocks, cut_lines


# ---------------------------------------------------------------- geometry helpers

def polys(geom):
    """Iterate polygons of any geometry."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in polys(g)]
    return []


def to_model(geom, origin, k):
    g = translate(geom, -origin[0], -origin[1])
    return shp_scale(g, k, k, origin=(0, 0))


def clean(geom, w=MIN_FEATURE):
    # morphological opening: removes thin necks and crumbs
    return geom.buffer(-w / 2, join_style=2).buffer(w / 2, join_style=2)


def rect_sides(poly):
    r = poly.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return 0.0, 0.0
    c = list(r.exterior.coords)
    a, b = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    return min(a, b), max(a, b)


def fits(poly, bed, margin=BED_MARGIN):
    a, b = rect_sides(poly)
    return b <= bed - 2 * margin


def split_straight(poly, bed):
    # fallback: halve along the long side until it fits
    if fits(poly, bed):
        return [poly]
    minx, miny, maxx, maxy = poly.bounds
    if maxx - minx >= maxy - miny:
        mid = (minx + maxx) / 2
        halves = [box(minx, miny, mid, maxy), box(mid, miny, maxx, maxy)]
    else:
        mid = (miny + maxy) / 2
        halves = [box(minx, miny, maxx, mid), box(minx, mid, maxx, maxy)]
    out = []
    for h in halves:
        for p in polys(poly.intersection(h)):
            if p.area > 1:
                out += split_straight(p, bed)
    return out


def make_cells(area, cut_lines, bed):
    lines = [area.exterior]
    for l in cut_lines:
        g = l.intersection(area)
        if not g.is_empty:
            lines.append(g)
    cells = [c for c in polygonize(unary_union(lines)) if c.area > 1]
    # polygonize can miss slivers; make sure the cells cover the area
    rest = area.difference(unary_union(cells)) if cells else area
    cells += [p for p in polys(rest) if p.area > 1]
    result = []
    for c in cells:
        result += split_straight(c, bed)
    return result


def merge_cells(cells, bed):
    # greedy: take the largest free cell and grow it with neighbours
    free = sorted(range(len(cells)), key=lambda i: -cells[i].area)
    pieces = []
    while free:
        piece = cells[free.pop(0)]
        while True:
            best, best_len = None, 0.5
            for i in free:
                if not piece.intersects(cells[i]):
                    continue
                shared = piece.boundary.intersection(cells[i].boundary).length
                if shared > best_len:
                    cand = unary_union([piece, cells[i]])
                    if cand.geom_type == "Polygon" and fits(cand, bed):
                        best, best_len = i, shared
            if best is None:
                break
            piece = unary_union([piece, cells[best]])
            free.remove(best)
        pieces.append(piece)
    return pieces


# ---------------------------------------------------------------- dovetails

def dovetail(p, t, n, depth=DT_DEPTH):
    # p: point on the seam, t: tangent, n: normal pointing into the neighbour
    def at(u, v):
        return (p[0] + t[0] * u + n[0] * v, p[1] + t[1] * u + n[1] * v)
    return Polygon([at(-DT_NECK / 2, -1), at(DT_NECK / 2, -1), at(DT_NECK / 2, 0),
                    at(DT_HEAD / 2, depth), at(-DT_HEAD / 2, depth), at(-DT_NECK / 2, 0)])


def seam_joints(pieces, water=None):
    """For each pair of neighbours: tabs on piece a, sockets in piece b."""
    tabs = [[] for _ in pieces]
    sockets = [[] for _ in pieces]
    for a in range(len(pieces)):
        for b in range(a + 1, len(pieces)):
            if not pieces[a].intersects(pieces[b]):
                continue
            seam = pieces[a].boundary.intersection(pieces[b].boundary)
            lines = [g for g in getattr(seam, "geoms", [seam]) if g.geom_type in ("LineString", "MultiLineString")]
            if not lines:
                continue
            seam = unary_union(lines)
            if seam.geom_type == "MultiLineString":
                seam = linemerge(seam)
            for line in getattr(seam, "geoms", [seam]):
                if line.geom_type != "LineString" or line.length < DT_HEAD * 2:
                    continue
                count = max(1, int(line.length // DT_STEP))
                for k in range(count):
                    d = line.length * (k + 0.5) / count
                    p0, p1 = line.interpolate(d - 1), line.interpolate(d + 1)
                    tx, ty = p1.x - p0.x, p1.y - p0.y
                    L = math.hypot(tx, ty) or 1
                    t = (tx / L, ty / L)
                    n = (-t[1], t[0])
                    pc = line.interpolate(d)
                    p = (pc.x, pc.y)
                    # point the tab into b
                    probe = Point(p[0] + n[0] * DT_DEPTH / 2, p[1] + n[1] * DT_DEPTH / 2)
                    if not pieces[b].contains(probe):
                        n = (-n[0], -n[1])
                    tab = dovetail(p, t, n)
                    # the socket (with a 1.5 mm wall around it) must lie inside b
                    socket = tab.buffer(DT_CLEAR, join_style=2)
                    outside_a = socket.buffer(1.5, join_style=2).difference(pieces[a].buffer(0.05))
                    if not outside_a.within(pieces[b].buffer(1e-6)):
                        continue
                    # socket under water would be a through-hole
                    if water is not None and socket.intersects(water):
                        continue
                    tabs[a].append(tab)
                    sockets[b].append(socket)
    return tabs, sockets


# ---------------------------------------------------------------- meshes

def extrude(geom, z0, h):
    meshes = []
    # snap to a 1 µm grid: removes near-duplicate vertices that turn into
    # degenerate triangles once the STL is written in float32
    geom = shapely.set_precision(geom, 0.001)
    for p in polys(geom):
        if p.area > 0.5:
            m = trimesh.creation.extrude_polygon(p, h)
            m.apply_translation((0, 0, z0))
            meshes.append(m)
    return meshes


def piece_mesh(piece, water, blocks, tabs=(), sockets=()):
    shell = piece.buffer(-GAP / 2, join_style=2)
    base = shell
    if tabs:
        base = unary_union([base] + list(tabs))
    if sockets:
        base = base.difference(unary_union(list(sockets)))
    land = clean(shell.difference(water))
    blk = clean(blocks.intersection(shell))
    parts = (extrude(base, 0, BASE_H)
             + extrude(land, BASE_H - EPS, ROAD_H + EPS)
             + extrude(blk, BASE_H + ROAD_H - EPS, BLOCK_H + EPS))
    if not parts:
        return None
    # manifold output is already clean; merge_vertices() would weld vertices
    # across the tiny seam gaps and break watertightness
    return trimesh.boolean.union(parts, engine="manifold") if len(parts) > 1 else parts[0]


def led_points_for_piece(points_model, piece, sockets):
    """LED holes inside this piece, moved off sockets and edges."""
    sock = unary_union(list(sockets)).buffer(LED_D / 2 + 0.6) if sockets else None
    inner = piece.buffer(-LED_D, join_style=2)
    out = []
    for (x, y) in points_model:
        pt = Point(x, y)
        if not piece.contains(pt):
            continue
        for _ in range(6):
            bad_edge = not inner.contains(pt)
            bad_sock = sock is not None and sock.contains(pt)
            if not (bad_edge or bad_sock):
                break
            # step away from the nearest edge, into the piece
            c = piece.exterior.interpolate(piece.exterior.project(pt))
            dx, dy = pt.x - c.x, pt.y - c.y
            L = math.hypot(dx, dy)
            if L < 1e-6:
                cc = piece.centroid
                dx, dy = cc.x - pt.x, cc.y - pt.y
                L = math.hypot(dx, dy) or 1
            pt = Point(pt.x + dx / L * LED_SHIFT, pt.y + dy / L * LED_SHIFT)
        else:
            continue
        out.append((pt.x, pt.y))
    return out


def led_cutters(points):
    holes = []
    total = BASE_H + ROAD_H + BLOCK_H
    for (x, y) in points:
        cyl = trimesh.creation.cylinder(radius=LED_D / 2, height=total + 10, sections=32)
        cyl.apply_translation((x, y, total / 2))
        holes.append(cyl)
    return holes


def print_pose(piece):
    """Rotation angle (deg) and offset that put the piece flat in the bed corner."""
    r = piece.minimum_rotated_rectangle
    c = list(r.exterior.coords)
    i = 0 if math.dist(c[0], c[1]) >= math.dist(c[1], c[2]) else 1
    ang = math.degrees(math.atan2(c[i + 1][1] - c[i][1], c[i + 1][0] - c[i][0]))
    return -ang


def layout_svg(area, pieces, water, leds):
    minx, miny, maxx, maxy = area.bounds
    w, h = maxx - minx, maxy - miny
    def path(g):
        d = []
        for p in polys(g):
            for ring in [p.exterior, *p.interiors]:
                pts = " L ".join(f"{x - minx:.2f},{maxy - y:.2f}" for x, y in ring.coords)
                d.append(f"M {pts} Z")
        return " ".join(d)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-5 -5 {w + 10:.1f} {h + 10:.1f}" '
           f'width="{w + 10:.0f}mm" height="{h + 10:.0f}mm" font-family="sans-serif">',
           f'<path d="{path(water)}" fill="#9cc8ef" fill-rule="evenodd"/>']
    for i, pc in enumerate(pieces):
        out.append(f'<path d="{path(pc)}" fill="none" stroke="#c22" stroke-width="0.6"/>')
        c = pc.representative_point()
        out.append(f'<text x="{c.x - minx:.1f}" y="{maxy - c.y:.1f}" font-size="{max(6, min(w, h) / 25):.1f}" '
                   f'text-anchor="middle" fill="#222">{i + 1:02d}</text>')
    for x, y in leds:
        out.append(f'<circle cx="{x - minx:.1f}" cy="{maxy - y:.1f}" r="1.5" fill="#e90"/>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- pipeline

def build_from_elements(elements, bbox_ll, model_width_mm, leds_ll=(), bed=256, log=print):
    west, south, east, north = bbox_ll
    proj = make_projector((south + north) / 2, (west + east) / 2)
    area, roads, water, blocks, cuts = build_layers(elements, bbox_ll, proj)
    minx, miny, maxx, maxy = area.bounds
    k = model_width_mm / (maxx - minx)
    o = (minx, miny)
    area, roads, water, blocks = (to_model(g, o, k) for g in (area, roads, water, blocks))
    cuts = [to_model(c, o, k) for c in cuts]
    leds = [to_model(proj(Point(lon, lat)), o, k) for lat, lon in leds_ll]
    leds = [(p.x, p.y) for p in leds]
    log(f"Модель {maxx - minx:.0f}×{maxy - miny:.0f} м → "
        f"{area.bounds[2]:.0f}×{area.bounds[3]:.0f} мм, масштаб 1:{1000 / k:.0f}")

    cells = make_cells(area, cuts, bed)
    pieces = merge_cells(cells, bed)
    log(f"Ячеек {len(cells)}, кусков {len(pieces)}")
    tabs, sockets = seam_joints(pieces, water)
    log(f"Замков: {sum(len(t) for t in tabs)}")

    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, pc in enumerate(pieces):
            m = piece_mesh(pc, water, blocks, tabs[i], sockets[i])
            if m is None:
                continue
            pts = led_points_for_piece(leds, pc, sockets[i])
            if pts:
                m = trimesh.boolean.difference([m] + led_cutters(pts), engine="manifold")
            # rotate so the piece lies along the bed axes, then move to the origin
            m.apply_transform(trimesh.transformations.rotation_matrix(
                math.radians(print_pose(pc)), (0, 0, 1)))
            m.apply_translation(-m.bounds[0])
            ext = m.extents
            if max(ext[0], ext[1]) > bed:
                log(f"  ! кусок {i + 1} {ext[0]:.0f}×{ext[1]:.0f} мм больше стола")
            log(f"  piece_{i + 1:02d}: {ext[0]:.0f}×{ext[1]:.0f} мм, "
                f"замков {len(tabs[i])}+{len(sockets[i])}, LED {len(pts)}, "
                f"{'ok' if m.is_watertight else 'НЕ watertight'}")
            z.writestr(f"piece_{i + 1:02d}.stl", m.export(file_type="stl"))
            n += 1
        z.writestr("layout.svg", layout_svg(area, pieces, water, leds))
    return buf.getvalue(), n


def check_bbox(bbox_ll):
    west, south, east, north = bbox_ll
    if not (west < east and south < north):
        raise BuildError("Неверная область")
    lat = math.radians((south + north) / 2)
    km2 = (east - west) * 111.32 * math.cos(lat) * (north - south) * 110.57
    if km2 > MAX_AREA_KM2:
        raise BuildError(f"Область {km2:.0f} км² слишком большая (максимум {MAX_AREA_KM2})")


def run(bbox_ll, model_width_mm, leds_ll=(), bed=256, log=print):
    check_bbox(bbox_ll)
    west, south, east, north = bbox_ll
    log("Скачиваю OSM…")
    els = fetch_osm(south, west, north, east)
    log(f"Объектов OSM: {len(els)}")
    return build_from_elements(els, bbox_ll, model_width_mm, leds_ll, bed, log)
