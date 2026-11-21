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
        r = c.post("/build", json={"bbox": list(BBOX), "width_mm": 400, "leds": [[55.75, 37.62]]})
        assert r.status_code == 200, r.text
        assert int(r.headers["X-Pieces"]) >= 2
        assert c.post("/build", json={"bbox": [37, 55, 38, 56]}).status_code == 400
    finally:
        core.fetch_osm = orig


if __name__ == "__main__":
    test_joints_exist(); test_build(); test_api(); print("OK")
