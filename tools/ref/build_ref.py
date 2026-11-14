"""Builds the reference geography the generator uses for customer addresses (run once, locally; the outputs are committed so the
setup job never needs the internet).

- generator/ref/localities.csv: every NSW postal delivery locality in and around the network footprint, with its postcode and centroid
  (the source's council field is unreliable, so it is not kept). Source: the community Australian postcodes dataset (matthewproctor/australianpostcodes on GitHub), built from
  Australia Post and ABS data.
- generator/ref/dem.bin.gz: ground elevation on a 0.001 degree grid (about 100 m) over the footprint, as little-endian int16 metres
  above sea level, row 0 at the north edge. Source: the AWS open terrain tiles (Terrarium encoding, zoom 12, about 30 m), averaged.
  generator/ref/dem.json describes the grid.
  python3 tools/ref/build_ref.py
"""
import array
import csv
import gzip
import io
import json
import math
import os
import re
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "generator", "ref")
BBOX = (-34.12, -33.14, 150.00, 151.58)   # south, north, west, east: the network's distribution substations plus a margin
STEP = 0.001
Z = 12
# delivery areas that are a shopping centre, campus, base or mail centre rather than a suburb
NOT_A_SUBURB = re.compile(r"\b(plaza|westpoint|westfield|mall|centre|center|shopping|square|dc|mc|business|university|hospital|barracks|base|"
                          r"airport|depot|delivery|mail|post|parcel|lpo|facility|industrial|correctional|gaol|private bag)\b", re.I)


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def localities():
    rows = csv.DictReader(io.StringIO(get("https://raw.githubusercontent.com/matthewproctor/australianpostcodes/master/australian_postcodes.csv")
                                      .decode("utf-8-sig")))
    s, n, w, e = BBOX
    seen, out = set(), []
    for r in rows:
        if r["state"] != "NSW" or r["type"] != "Delivery Area":
            continue
        lat, lon = float(r["Lat_precise"] or r["lat"] or 0), float(r["Long_precise"] or r["long"] or 0)
        if not (s <= lat <= n and w <= lon <= e):
            continue
        k = (r["locality"], r["postcode"])
        if k in seen:
            continue
        seen.add(k)
        if NOT_A_SUBURB.search(r["locality"]):
            continue
        out.append(dict(locality=r["locality"].title(), postcode=r["postcode"], lat=round(lat, 6), lon=round(lon, 6)))
    with open(os.path.join(OUT, "localities.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["locality", "postcode", "lat", "lon"])
        wr.writeheader(); wr.writerows(sorted(out, key=lambda x: (x["locality"], x["postcode"])))
    print("localities", len(out))


def tile_xy(lat, lon):
    n = 2 ** Z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def dem():
    s, n, w, e = BBOX
    x0, y0 = tile_xy(n, w); x1, y1 = tile_xy(s, e)
    tiles = {}
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            im = Image.open(io.BytesIO(get(f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{Z}/{tx}/{ty}.png"))).convert("RGB")
            tiles[(tx, ty)] = im.load()
    print("tiles", len(tiles))

    def height(lat, lon):
        x, y = tile_xy(lat, lon)
        px = tiles[(int(x), int(y))][int((x % 1) * 256), int((y % 1) * 256)]
        return px[0] * 256 + px[1] + px[2] / 256 - 32768

    rows, cols = round((n - s) / STEP) + 1, round((e - w) / STEP) + 1
    grid = array.array("h")
    for i in range(rows):
        lat = n - i * STEP
        for j in range(cols):
            lon = w + j * STEP
            hs = [height(lat + dy, lon + dx) for dy in (-STEP / 4, STEP / 4) for dx in (-STEP / 4, STEP / 4)]   # a 2 x 2 average
            grid.append(max(-50, round(sum(hs) / 4)))
    if array.array("h", [1]).tobytes() != b"\x01\x00":
        grid.byteswap()
    with gzip.open(os.path.join(OUT, "dem.bin.gz"), "wb", compresslevel=9) as f:
        f.write(grid.tobytes())
    with open(os.path.join(OUT, "dem.json"), "w") as f:
        json.dump(dict(north=n, west=w, step=STEP, rows=rows, cols=cols, units="metres above sea level", dtype="int16 little-endian",
                       source="AWS open terrain tiles, Terrarium encoding, zoom 12"), f, indent=1)
    print("dem", rows, "x", cols, "min", min(grid), "max", max(grid))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    localities()
    dem()
