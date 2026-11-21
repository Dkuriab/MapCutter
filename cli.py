"""Command-line build, no browser needed.

python cli.py --bbox 37.60 55.74 37.66 55.765 --width 500 --led 55.75 37.62 -o city_map.zip
"""
import argparse
import core

p = argparse.ArgumentParser(description="City Map 3D: OSM -> STL pieces")
p.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
p.add_argument("--width", type=float, default=400, help="model width, mm")
p.add_argument("--bed", type=float, default=256, help="printer bed size, mm")
p.add_argument("--led", nargs=2, type=float, action="append", default=[], metavar=("LAT", "LON"))
p.add_argument("-o", "--out", default="city_map.zip")
a = p.parse_args()

data, n = core.run(a.bbox, a.width, a.led, bed=a.bed)
with open(a.out, "wb") as f:
    f.write(data)
print(f"Saved {n} pieces to {a.out}")
