"""Offline test: synthetic city, no network needed.  python -m pytest tests  (or python tests/test_core.py)"""
import io, math, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trimesh
import core

BBOX = (37.60, 55.74, 37.66, 55.765)  # west, south, east, north


def way(tags, pts):
    return {"type": "way", "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in pts]}


def synthetic():
    w, s, e, n = BBOX
    els = []
    for i in range(1, 12):
        x = w + (e - w) * i / 12
        els.append(way({"highway": "primary" if i % 4 == 0 else "residential"}, [(x, s - .01), (x, n + .01)]))
    for j in range(1, 8):
        y = s + (n - s) * j / 8
        els.append(way({"highway": "secondary" if j == 4 else "tertiary"}, [(w - .01, y), (e + .01, y)]))
    # meandering river
    els.append(way({"waterway": "river"}, [(w - .01 + (e - w + .02) * t / 40,
                                              s + (n - s) * (0.3 + 0.08 * math.sin(t / 4))) for t in range(41)]))
    # lake as multipolygon relation with an island
    cx, cy, r = w + (e - w) * .75, s + (n - s) * .75, .003
    ring = lambda rr: [{"lon": cx + rr * 1.7 * math.cos(a / 20 * 2 * math.pi),
                        "lat": cy + rr * math.sin(a / 20 * 2 * math.pi)} for a in range(21)]
    els.append({"type": "relation", "tags": {"natural": "water"},
                "members": [{"type": "way", "role": "outer", "geometry": ring(r)},
                            {"type": "way", "role": "inner", "geometry": ring(r / 3)}]})
    return els


def test_build():
    leds = [(55.75, 37.62), (55.755, 37.64), (55.7625, 37.65), (55.745, 37.605)]
    data, n = core.build_from_elements(synthetic(), BBOX, 500, leds)
    z = zipfile.ZipFile(io.BytesIO(data))
    stls = [f for f in z.namelist() if f.endswith(".stl")]
    assert "layout.svg" in z.namelist()
    assert len(stls) == n and n >= 4
    for f in stls:
        m = trimesh.load(io.BytesIO(z.read(f)), file_type="stl")
        assert m.is_watertight, f
        assert max(m.extents[:2]) <= 256, (f, m.extents)
        assert abs(m.extents[2] - (core.BASE_H + core.ROAD_H + core.BLOCK_H)) < 0.05


def test_joints_exist():
    from shapely.geometry import box
    pieces = [box(0, 0, 100, 100), box(100, 0, 200, 100)]
    tabs, sockets = core.seam_joints(pieces)
    assert len(tabs[0]) >= 1 and len(sockets[1]) == len(tabs[0])
    assert tabs[0][0].difference(pieces[0]).within(pieces[1])


def test_api(monkeypatch=None):
    from fastapi.testclient import TestClient
    import app
    orig = core.fetch_osm
    core.fetch_osm = lambda *a: synthetic()
    try:
        c = TestClient(app.app)
        assert c.get("/").status_code == 200
        import json
        job = lambda **k: {"job": json.dumps(k)}
        r = c.post("/build", data=job(bbox=list(BBOX), width_mm=400, leds=[[55.75, 37.62]]))
        assert r.status_code == 200, r.text
        assert int(r.headers["X-Pieces"]) >= 2
        assert c.post("/build", data=job(bbox=[37, 55, 38, 56])).status_code == 400
        # longitude shifted by a full turn, as Leaflet sends after panning around the globe
        r = c.post("/build", data=job(bbox=[BBOX[0] + 360, BBOX[1], BBOX[2] + 360, BBOX[3]]))
        assert r.status_code == 200, r.text
        # uploaded .osm file instead of downloading
        r = c.post("/build", data=job(bbox=list(BBOX)),
                   files={"osm": ("city.osm", to_osm_xml(synthetic()).encode())})
        assert r.status_code == 200, r.text
    finally:
        core.fetch_osm = orig


def to_osm_xml(elements):
    """synthetic elements -> OSM XML, the way openstreetmap.org Export gives it"""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    nid, wid = [0], [0]
    nodes, ways, rels = [], [], []
    def add_way(geom, tags, real_id=None):
        refs = []
        for k, p in enumerate(geom):
            nid[0] += 1
            node_id = real_id * 100000 + k if real_id else nid[0]
            nodes.append(f'<node id="{node_id}" lat="{p["lat"]}" lon="{p["lon"]}"/>')
            refs.append(node_id)
        wid[0] = real_id or wid[0] + 1
        t = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        ways.append(f'<way id="{wid[0]}">' + "".join(f'<nd ref="{r}"/>' for r in refs) + t + "</way>")
        return wid[0]
    for el in elements:
        if el["type"] == "way":
            add_way(el["geometry"], el["tags"], el.get("id"))
        else:
            mem = [(add_way(m["geometry"], {}), m["role"]) for m in el["members"]]
            t = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in el["tags"].items())
            rels.append('<relation id="1">' + "".join(
                f'<member type="way" ref="{r}" role="{role}"/>' for r, role in mem) + t + "</relation>")
    # a footpath that must be ignored
    nodes.append('<node id="999001" lat="55.75" lon="37.61"/><node id="999002" lat="55.76" lon="37.61"/>')
    ways.append('<way id="999"><nd ref="999001"/><nd ref="999002"/><tag k="highway" v="footway"/></way>')
    return "\n".join(out + nodes + ways + rels + ["</osm>"])


def test_osm_xml_and_pbf(tmp=None):
    import tempfile, osm_source
    tmp = tmp or tempfile.mkdtemp()
    xml_path = os.path.join(tmp, "city.osm")
    with open(xml_path, "w") as f:
        f.write(to_osm_xml(synthetic()))
    els = osm_source.load_file(xml_path)
    assert len(els) == len(synthetic())  # footway dropped, relation kept
    assert any(e["type"] == "relation" and len(e["members"]) == 2 for e in els)
    data, n = core.run(BBOX, 400, osm_file=xml_path, log=lambda *a: None)
    assert n >= 2
    try:
        import osmium
    except ImportError:
        return
    pbf_path = os.path.join(tmp, "city.osm.pbf")
    if os.path.exists(pbf_path):
        os.remove(pbf_path)
    wr = osmium.SimpleWriter(pbf_path)
    for obj in osmium.FileProcessor(xml_path):
        wr.add(obj)
    wr.close()
    els2 = osm_source.load_file(pbf_path, BBOX)
    assert len(els2) == len(els), (len(els2), len(els))
    data, n2 = core.run(BBOX, 400, osm_file=pbf_path, log=lambda *a: None)
    assert n2 == n


def test_fallbacks():
    """Overpass answers 400 everywhere -> main OSM API is tried; its XML is used."""
    import osm_source, requests
    xml = to_osm_xml(synthetic()).encode()
    calls = []
    class R:
        def __init__(self, code, text="", content=b""):
            self.status_code, self.text, self.content = code, text, content
    def post(url, **k):
        calls.append(url); return R(400, "<html><p>Error: line 3: parse error</p></html>")
    def get(url, **k):
        calls.append(url); return R(200, content=xml)
    op, og = requests.post, requests.get
    requests.post, requests.get = post, get
    try:
        els = osm_source.fetch_online(*[BBOX[1], BBOX[0], BBOX[3], BBOX[2]], log=lambda *a: None)
        assert len(els) == len(synthetic())
        posts = [c for c in calls if c in osm_source.OVERPASS_URLS]
        assert len(posts) == len(osm_source.OVERPASS_URLS)  # 400 is not retried in smaller parts
        assert len(calls) > len(posts)                      # then the main OSM API
        # all sources down -> readable error with the server text and a hint about files
        requests.get = lambda url, **k: R(509, "Bandwidth exceeded")
        try:
            osm_source.fetch_online(*[BBOX[1], BBOX[0], BBOX[3], BBOX[2]], log=lambda *a: None)
            assert False
        except osm_source.SourceError as e:
            assert "parse error" in str(e) and "509" in str(e) and "geofabrik" in str(e)
    finally:
        requests.post, requests.get = op, og


def test_wrapped_longitude():
    import osm_source
    w, s, e, n = osm_source.normalize_bbox(BBOX[0] + 360, BBOX[1], BBOX[2] + 360, BBOX[3])
    assert abs(w - BBOX[0]) < 1e-9 and abs(e - BBOX[2]) < 1e-9
    assert "(55.7400000,37.6000000," in osm_source.overpass_query(s, w, n, e)


if __name__ == "__main__":
    test_joints_exist(); test_build(); test_api(); test_osm_xml_and_pbf()
    test_fallbacks(); test_wrapped_longitude(); print("OK")
