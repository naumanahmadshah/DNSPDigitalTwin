"""Reference geography for customer addresses: the postal locality (suburb, postcode, council) a point falls in, and the ground
elevation at a point. Both come from files in generator/ref built by tools/ref/build_ref.py. Pure Python (stdlib only)."""
import array
import csv
import gzip
import json
import math
import os

REF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref")


class Geo:
    def __init__(self, ref=REF):
        with open(os.path.join(ref, "localities.csv")) as f:
            self.loc = [dict(r, lat=float(r["lat"]), lon=float(r["lon"])) for r in csv.DictReader(f)]
        self._cells = {}
        for l in self.loc:   # a 0.05 degree bucket index, so the nearest locality is found among a few dozen
            self._cells.setdefault((int(l["lat"] // 0.05), int(l["lon"] // 0.05)), []).append(l)
        with open(os.path.join(ref, "dem.json")) as f:
            self.dem = json.load(f)
        with gzip.open(os.path.join(ref, "dem.bin.gz"), "rb") as f:
            self.h = array.array("h"); self.h.frombytes(f.read())
        if array.array("h", [1]).tobytes() != b"\x01\x00":
            self.h.byteswap()

    def locality(self, lat, lon):
        """The nearest postal locality centroid (the suburb a point is taken to be in)."""
        ci, cj = int(lat // 0.05), int(lon // 0.05)
        for ring in range(1, 6):
            cand = [l for di in range(-ring, ring + 1) for dj in range(-ring, ring + 1) for l in self._cells.get((ci + di, cj + dj), [])]
            if cand:
                k = math.cos(math.radians(lat))
                return min(cand, key=lambda l: (l["lat"] - lat) ** 2 + ((l["lon"] - lon) * k) ** 2)
        return None

    def elevation(self, lat, lon):
        """Ground height in metres above sea level, bilinear between the grid points."""
        d = self.dem
        y, x = (d["north"] - lat) / d["step"], (lon - d["west"]) / d["step"]
        i, j = max(0, min(d["rows"] - 2, int(y))), max(0, min(d["cols"] - 2, int(x)))
        fy, fx = min(1, max(0, y - i)), min(1, max(0, x - j))
        g = lambda a, b: self.h[a * d["cols"] + b]  # noqa: E731
        top = g(i, j) * (1 - fx) + g(i, j + 1) * fx
        bot = g(i + 1, j) * (1 - fx) + g(i + 1, j + 1) * fx
        return round(top * (1 - fy) + bot * fy, 1)
