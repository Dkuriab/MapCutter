"""Big synthetic city for load tests."""
import math

BIG_BBOX = (37.40, 55.65, 37.72, 55.785)  # ~20 x 15 km


def way(i, tags, pts):
    return {"type": "way", "id": i, "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in pts]}


def big_city(step_m=150):
    w, s, e, n = BIG_BBOX
    dlon = step_m / (111320 * math.cos(math.radians((s + n) / 2)))
    dlat = step_m / 110570
    els, i = [], 0
    x, k = w + dlon, 0
    while x < e:
        k += 1; i += 1
        cls = "primary" if k % 12 == 0 else ("tertiary" if k % 4 == 0 else "residential")
        els.append(way(i, {"highway": cls}, [(x, s - .005), (x, n + .005)]))
        x += dlon
    y, k = s + dlat, 0
    while y < n:
        k += 1; i += 1
        cls = "secondary" if k % 12 == 0 else ("tertiary" if k % 4 == 0 else "residential")
        els.append(way(i, {"highway": cls}, [(w - .005, y), (e + .005, y)]))
        y += dlat
    i += 1
    els.append(way(i, {"waterway": "river"},
                   [(w - .01 + (e - w + .02) * t / 200, s + (n - s) * (0.45 + 0.1 * math.sin(t / 15)))
                    for t in range(201)]))
    return els
