"""Pull the real public feeds into the feeds volume, keeping each raw payload with a capture timestamp.

Feeds (verified 31 Oct 2026): NSW RFS major incidents (GeoJSON) and fire danger ratings (XML), DEA Hotspots (WFS, bounding
box of the footprint), Open-Meteo 72-hour forecast on a grid over the footprint (JSON).
If a host cannot be reached, the error is recorded and the app keeps showing the last good capture.
Usage: pull_feeds.py --root /Volumes/<c>/<s>/feeds [--loop-minutes N --every-seconds S]
"""
import argparse
import datetime as dt
import json
import os
import time
import urllib.request

UA = {"User-Agent": "AssetAtlas-demo/1.0 (Databricks)"}
BBOX = (-34.15, 150.05, -33.15, 151.60)  # south, west, north, east over the three areas

FEEDS = {
    "rfs_incidents": ("https://www.rfs.nsw.gov.au/feeds/majorIncidents.json", "json", 300),
    "rfs_fire_danger": ("https://www.rfs.nsw.gov.au/feeds/fdrToban.xml", "xml", 1800),
    "dea_hotspots": ("https://hotspots.dea.ga.gov.au/geoserver/public/wfs?service=WFS&version=1.1.0&request=GetFeature"
                     "&typeName=public:hotspots_three_days&outputFormat=application/json&srsName=EPSG:4326"
                     f"&bbox={BBOX[1]},{BBOX[0]},{BBOX[3]},{BBOX[2]},EPSG:4326", "json", 900),
}


def forecast_url():
    lats, lons = [], []
    s, w, n, e = BBOX
    steps = 6
    for i in range(steps + 1):
        for j in range(steps + 1):
            lats.append(round(s + (n - s) * i / steps, 3))
            lons.append(round(w + (e - w) * j / steps, 3))
    return ("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(map(str, lats)) + "&longitude=" + ",".join(map(str, lons)) +
            "&hourly=wind_gusts_10m,wind_speed_10m,wind_direction_10m,precipitation,temperature_2m,relative_humidity_2m,"
            "cloud_cover,shortwave_radiation&forecast_days=4&timezone=Australia%2FSydney")


FEEDS["openmeteo_forecast"] = (forecast_url(), "json", 3600)


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def pull_once(root, only=None, state=None):
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    results = {}
    for name, (url, ext, every) in FEEDS.items():
        if only and name not in only:
            continue
        if state is not None and now.timestamp() - state.get(name, 0) < every:
            continue
        d = os.path.join(root, name, now.strftime("%Y%m%d"))
        os.makedirs(d, exist_ok=True)
        try:
            t = time.time()
            body = fetch(url)
            with open(os.path.join(d, f"{name}_{stamp}.{ext}"), "wb") as f:
                f.write(body)
            results[name] = dict(ok=True, bytes=len(body), seconds=round(time.time() - t, 2))
            if state is not None:
                state[name] = now.timestamp()
        except Exception as e:  # noqa: BLE001 - record and keep going; the app falls back to the last capture
            results[name] = dict(ok=False, error=str(e)[:300])
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--loop-minutes", type=float, default=0)
    ap.add_argument("--every-seconds", type=int, default=60)
    a, _ = ap.parse_known_args()
    if not a.loop_minutes:
        print(json.dumps(pull_once(a.root), indent=1))
        return
    state, end = {}, time.time() + a.loop_minutes * 60
    while time.time() < end:
        res = pull_once(a.root, state=state)
        if res:
            print(dt.datetime.utcnow().isoformat(), json.dumps(res), flush=True)
        time.sleep(a.every_seconds)


if __name__ == "__main__":
    main()
