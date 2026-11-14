"""3D site scenes. The server says what is really at a site: a contiguous run of the feeder walked along its real spans (poles,
conductors, the devices on each pole, LiDAR clearance and lean), the distribution substation and the customers it supplies, the
zone substation's transformers and feeder exits, and any live fault with its cause. The browser lays the site out to scale.
Every read is Lakebase: synced gold in serving.*, the live fault and loads in atlas.* and app.*."""
import json
import math
import re
from collections import defaultdict

import core
from core import pg, pq
from situations import fire_danger

SPEECH_SYS = ("You are a cheerful, sharp Australian lineworker in a comic. In one or two short sentences (under 28 words), say what you make of "
              "this asset from the facts. Be specific with one number. Never call anyone 'mate'. No dashes as punctuation, no emojis.")
RANGES = ("LPH", "KNV", "GVL")
POLE_DEVICES = ("recloser", "gas_switch", "fuse", "swer_isolator", "nop")
_WKT = re.compile(r"(-?\d+\.?\d*) (-?\d+\.?\d*)")


def _st(s):
    return s or "green"


def _label(domain, code):
    rows = pq("SELECT code, label FROM ref_label WHERE domain = %(d)s", {"d": domain}, ttl=3600)
    return next((r["label"] for r in rows if r["code"] == code), None)


def _metres(lat0, lon0):
    k = math.cos(math.radians(lat0))
    return lambda lat, lon: ((lon - lon0) * 111320 * k, (lat0 - lat) * 110540)   # x east, z south (three.js ground plane)


# ------------------------------------------------------------------ the feeder as a graph of poles and spans
def _graph(feeder_id):
    poles = pq("""SELECT asset_id, lat, lon, material, age_years, install_year, open_defects, worst_open_priority, customers_downstream,
                         life_support_downstream FROM gld_asset WHERE feeder_id = %(f)s AND asset_class = 'pole'""", {"f": feeder_id}, ttl=1800)
    spans = pq("SELECT asset_id, geom_wkt, material FROM gld_asset WHERE feeder_id = %(f)s AND asset_class = 'overhead_span'",
               {"f": feeder_id}, ttl=1800)
    devs = pq("""SELECT a.asset_id, a.asset_class, l.label AS class_label, a.lat, a.lon, a.customers_downstream FROM gld_asset a
                 LEFT JOIN ref_label l ON l.domain = 'asset_class' AND l.code = a.asset_class
                 WHERE a.feeder_id = %(f)s AND a.asset_class = ANY(%(c)s::text[])""", {"f": feeder_id, "c": list(POLE_DEVICES)}, ttl=1800)
    key = lambda lat, lon: (round(float(lat), 6), round(float(lon), 6))  # noqa: E731 - spans end exactly on pole coordinates
    at = {key(p["lat"], p["lon"]): p for p in poles}
    adj = defaultdict(list)
    for s in spans:
        pts = _WKT.findall(s["geom_wkt"] or "")
        if len(pts) < 2:
            continue
        a, b = at.get(key(pts[0][1], pts[0][0])), at.get(key(pts[-1][1], pts[-1][0]))
        if a and b and a is not b:
            adj[a["asset_id"]].append((b["asset_id"], s))
            adj[b["asset_id"]].append((a["asset_id"], s))
    on_pole = defaultdict(list)
    for d in devs:
        p = at.get(key(d["lat"], d["lon"]))
        if p:
            on_pole[p["asset_id"]].append(d)
    return {p["asset_id"]: p for p in poles}, adj, on_pole


def _nearest(poles, lat, lon, prefer=None):
    cand = [p for p in poles.values() if prefer is None or prefer(p)] or list(poles.values())
    return min(cand, key=lambda p: (p["lat"] - lat) ** 2 + ((p["lon"] - lon) * 0.83) ** 2)["asset_id"] if cand else None


def _chain(poles, adj, start, n, toward=None):
    """A contiguous run of n poles through `start`, extended at both ends along the straightest continuation (how a line runs)."""
    xy = lambda pid: (poles[pid]["lon"] * 0.83, poles[pid]["lat"])  # noqa: E731
    path, seen = [start], {start}

    def nxt(prev, cur):
        best, bs = None, -9
        for nb, _ in adj[cur]:
            if nb in seen:
                continue
            if prev is None:
                score = poles[nb]["customers_downstream"] or 0
                score = 1 + score / 1e6
            else:
                a, b, c = xy(prev), xy(cur), xy(nb)
                u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
                score = (u[0] * v[0] + u[1] * v[1]) / (math.hypot(*u) * math.hypot(*v) + 1e-12)
            if score > bs:
                best, bs = nb, score
        return best

    first = toward if toward in [nb for nb, _ in adj[start]] else nxt(None, start)
    if first:
        path.append(first)
        seen.add(first)
    grow_end = True
    while len(path) < n:
        if grow_end:
            nb = nxt(path[-2], path[-1]) if len(path) > 1 else None
            if nb:
                path.append(nb)
        else:
            nb = nxt(path[1], path[0]) if len(path) > 1 else None
            if nb:
                path.insert(0, nb)
        if nb:
            seen.add(nb)
        elif not (nxt(path[-2], path[-1]) if len(path) > 1 else None) and not (nxt(path[1], path[0]) if len(path) > 1 else None):
            break
        grow_end = not grow_end
    spans = []
    for a, b in zip(path, path[1:]):
        spans.append(next(s for nb, s in adj[a] if nb == b))
    return path, spans


def _veg(span_ids):
    if not span_ids:
        return {}
    rows = pq("""SELECT v.span_id, v.clearance_m, v.clearance_12m_m, v.category_label, v.dominant_species, v.hazard_tree, v.bushfire_prone,
                        v.pole_lean_deg, v.status, v.risk_score, r.fault_probability
                 FROM gld_vegetation_risk v LEFT JOIN ml_span_risk r USING (span_id) WHERE v.span_id = ANY(%(s)s::text[])""",
              {"s": list(span_ids)}, ttl=1800)
    return {r["span_id"]: r for r in rows}


def _run(feeder_id, centre_lat, centre_lon, n, prefer=None):
    poles, adj, on_pole = _graph(feeder_id)
    if not poles:
        return [], []
    start = _nearest(poles, centre_lat, centre_lon, prefer)
    path, spans = _chain(poles, adj, start, n)
    veg = _veg([s["asset_id"] for s in spans])
    p0 = poles[start]
    m = _metres(p0["lat"], p0["lon"])
    out_p = []
    for pid in path:
        p = poles[pid]
        x, z = m(p["lat"], p["lon"])
        out_p.append(dict(asset_id=pid, label=pid, material=p["material"], x=round(x, 1), z=round(z, 1), lat=p["lat"], lon=p["lon"], centre=pid == start,
                          status="red" if p["worst_open_priority"] == 1 else "amber" if p["open_defects"] else "green",
                          devices=[dict(asset_id=d["asset_id"], kind=d["asset_class"], label=d["class_label"] or d["asset_class"]) for d in on_pole.get(pid, [])],
                          facts=dict(material=(p["material"] or "").title(), age_years=p["age_years"], open_defects=p["open_defects"],
                                     customers_behind=p["customers_downstream"], life_support_behind=p["life_support_downstream"])))
    out_s = []
    for s in spans:
        v = veg.get(s["asset_id"]) or {}
        out_s.append(dict(span_id=s["asset_id"], label=s["asset_id"], conductor=s["material"], status=_st(v.get("status")),
                          clearance_m=v.get("clearance_m"), hazard_tree=bool(v.get("hazard_tree")), lean_deg=v.get("pole_lean_deg"),
                          facts=dict(conductor=s["material"], clearance_m=v.get("clearance_m"), clearance_in_12_months_m=v.get("clearance_12m_m"),
                                     encroachment=v.get("category_label"), species=v.get("dominant_species"), hazard_tree=v.get("hazard_tree"),
                                     fault_probability=v.get("fault_probability"))))
    return out_p, out_s


def _outage(feeder_id):
    out = pg.q("SELECT * FROM atlas.live_outage WHERE feeder_id = %s AND status = 'active' ORDER BY start_time DESC LIMIT 1", (feeder_id,), one=True)
    if out:
        out["cause_label"] = _label("cause", out.get("cause_code")) or "Cause under investigation"
    return out


def _mark_fault(poles, spans, out):
    """The live fault sits at a network node: mark the pole and the span beyond it that the crew will patrol."""
    if not out or not poles:
        return None
    k = next((i for i, p in enumerate(poles) if p["centre"]), 0)   # the run is centred on the fault node
    si = (k if k < len(spans) else k - 1) if spans else None
    if si is not None:
        spans[si]["fault"] = True
        spans[si]["status"] = "red"
    return dict(pole_index=k, span_index=si, cause_code=out.get("cause_code"), cause_label=out["cause_label"],
                customers_off=out.get("customers_off"), outage_id=out.get("outage_id"))


# ------------------------------------------------------------------ scenes
def zone(zs_id):
    tx = pq("SELECT * FROM gld_zone_transformer_state WHERE zs_id = %(z)s ORDER BY transformer_id", {"z": zs_id}, ttl=600)
    fd = pq("""SELECT feeder_id, feeder_name, status, customers, amps_peak_30d, rating_amps FROM gld_feeder_state WHERE zs_id = %(z)s
               ORDER BY feeder_id""", {"z": zs_id}, ttl=600)
    zs = pq("SELECT name FROM gld_asset WHERE asset_id = %(z)s", {"z": zs_id}, ttl=3600)
    live = (pg.q("SELECT value FROM app.live_state WHERE key = 'zone_transformers'", one=True) or {}).get("value") or {}
    out = core.feeder_outages()   # tripped feeders (the bay's breaker opened) and feeders with an outage on a fused spur only
    tripped = {k for k, v in out.items() if v["tripped"]}
    trans = []
    for i, t in enumerate(tx):
        lv = live.get(t["transformer_id"], {})
        trans.append(dict(asset_id=t["transformer_id"], label=f"Transformer {i + 1}", status=_st(t["status"]),
                          facts=dict(rating_mva=t["rating_mva"], mw_now=lv.get("mw"), top_oil_c=lv.get("top_oil"), h2_ppm=t["h2_now"],
                                     h2_rise_90d_ppm=t["h2_rise_90d"], peak_utilisation_30d=t["peak_utilisation_30d"])))
    feeders = [dict(asset_id=f["feeder_id"], label=f["feeder_id"] + (" tripped" if f["feeder_id"] in tripped else ""),
                    status="red" if f["feeder_id"] in tripped else "amber" if f["feeder_id"] in out else _st(f["status"]),
                    tripped=f["feeder_id"] in tripped,
                    facts=dict(customers=f["customers"], peak_amps_30d=f["amps_peak_30d"], rating_amps=f["rating_amps"], tripped=f["feeder_id"] in tripped))
               for f in fd]
    rank = {"red": 2, "amber": 1}
    worst = max(trans + feeders, key=lambda i: rank.get(i["status"], 0), default={})
    name = (zs[0]["name"] if zs else zs_id.replace("ZS-", "")) or zs_id
    return dict(kind="zone", title=name if "zone substation" in name.lower() else f"{name} zone substation", zs_id=zs_id, transformers=trans,
                feeders=feeders, status=worst.get("status", "green"), seed=zs_id)


def transmission(ts_id):
    """A transmission substation: its 132 kV transformers, the 33 or 66 kV circuits out to its zone substations, the 132 kV circuits in
    from the bulk supply point, and the zones it feeds. The status is the site's (its load against firm capacity)."""
    site = pq("SELECT * FROM gld_transmission_substation WHERE ts_id = %(t)s", {"t": ts_id}, ttl=600)
    if not site:
        return None
    s = site[0]
    tx = pq("SELECT * FROM gld_transmission_transformer_state WHERE ts_id = %(t)s ORDER BY transformer_id", {"t": ts_id}, ttl=600)
    circ = pq("""SELECT l.line_id, l.zs_id, l.circuit, l.kv, l.route_km, l.rating_mva, a.name zone_name, a.customers_downstream customers,
                        coalesce(t.status, 'green') status
                 FROM gld_subtransmission l JOIN gld_asset a ON a.asset_id = l.zs_id
                 LEFT JOIN (SELECT zs_id, CASE WHEN bool_or(status = 'red') THEN 'red' WHEN bool_or(status = 'amber') THEN 'amber'
                                               ELSE 'green' END status
                            FROM gld_zone_transformer_state GROUP BY zs_id) t ON t.zs_id = l.zs_id
                 WHERE l.ts_id = %(t)s ORDER BY l.zs_id, l.circuit""", {"t": ts_id}, ttl=600)
    inc = pq("""SELECT l.line_id, l.bsp_id, l.circuit, l.kv, l.route_km, l.rating_mva, b.name bsp_name
                FROM gld_transmission_line l JOIN gld_bulk_supply b ON b.bsp_id = l.bsp_id
                WHERE l.ts_id = %(t)s ORDER BY l.circuit""", {"t": ts_id}, ttl=600)
    transformers = [dict(asset_id=t["transformer_id"], label=f"Transformer {i + 1}", status=_st(t["status"]),
                         facts=dict(rating_mva=t["rating_mva"], mw_now=t["mw_now"], top_oil_c=t["top_oil_now"],
                                    peak_mw_30d=t["peak_mw_30d"], peak_utilisation_30d=t["peak_utilisation_30d"]))
                    for i, t in enumerate(tx)]
    circuits = [dict(asset_id=c["line_id"], label=f"{c['kv']} kV circuit {c['circuit']} to {c['zone_name']}", status=_st(c["status"]),
                     zs_id=c["zs_id"], zone_name=c["zone_name"],
                     facts=dict(kv=c["kv"], rating_mva=c["rating_mva"], route_km=c["route_km"], circuit=c["circuit"], customers=c["customers"]))
                for c in circ]
    incoming = [dict(asset_id=l["line_id"], label=f"132 kV circuit {l['circuit']} from {l['bsp_name']}", status="green", bsp_id=l["bsp_id"],
                     bsp_name=l["bsp_name"], facts=dict(kv=l["kv"], rating_mva=l["rating_mva"], route_km=l["route_km"], circuit=l["circuit"]))
                for l in inc]
    zones = [dict(zs_id=c["zs_id"], name=c["zone_name"], status=c["status"]) for c in {c["zs_id"]: c for c in circ}.values()]
    return dict(kind="transmission", title=s["name"], ts_id=ts_id, status=_st(s["status"]), seed=ts_id, transformers=transformers,
                circuits=circuits, incoming=incoming, zones=zones, bsp_name=s["bulk_supply_name"], bsp_id=s["bsp_id"],
                facts=dict(kv=s["kv"], secondary_kv=s["secondary_kv"], installed_mva=s["installed_mva"], firm_mva=s["firm_mva"],
                           peak_mw_30d=s["peak_mw_30d"], peak_utilisation_30d=s["peak_utilisation_30d"], customers=s["customers"],
                           life_support=s["life_support"], single_transmission_zones=s["single_transmission_zones"]))


def street(feeder_id):
    """Overhead suburban street: the run of poles around the busiest pole-mounted substation (or around the live fault)."""
    out = _outage(feeder_id)
    dss = pq("SELECT * FROM gld_ds_state WHERE feeder_id = %(f)s AND ds_type = 'pole' ORDER BY customers DESC", {"f": feeder_id}, ttl=1800)
    if out and out.get("lat") and dss:   # at a fault, the substation nearest the fault; otherwise the busiest one
        ds = min(dss, key=lambda d: (d["lat"] - out["lat"]) ** 2 + ((d["lon"] - out["lon"]) * 0.83) ** 2)
    else:
        ds = dss[0] if dss else None
    lat, lon = (out["lat"], out["lon"]) if out and out.get("lat") else ((ds["lat"], ds["lon"]) if ds else (None, None))
    if lat is None:
        top = pq("SELECT lat, lon FROM gld_asset WHERE feeder_id = %(f)s AND asset_class = 'pole' ORDER BY customers_downstream DESC LIMIT 1",
                 {"f": feeder_id}, ttl=1800)
        lat, lon = (top[0]["lat"], top[0]["lon"]) if top else (0, 0)
    poles, spans = _run(feeder_id, lat, lon, 6)
    if ds and poles:
        m = _metres(ds["lat"], ds["lon"])
        gap = [math.hypot(*m(p["lat"], p["lon"])) for p in poles]
        k = min(range(len(poles)), key=gap.__getitem__)
        on_run = gap[k] <= 60   # pole-mounted substations sit on (or beside) a pole
        nm = pq("""SELECT count(*) n, sum(CASE WHEN solar_kw > 0 THEN 1 ELSE 0 END) solar, sum(has_ev_charger) ev, sum(has_battery) bat,
                          sum(CASE WHEN life_support THEN 1 ELSE 0 END) ls FROM gld_nmi WHERE ds_id = %(d)s""", {"d": ds["ds_id"]}, ttl=1800)
        nm = nm[0] if nm else {}
        if on_run:
            poles[k]["ds"] = dict(asset_id=ds["ds_id"], label=ds["ds_id"], status=_st(ds["status"]),
                                  facts=dict(kva=ds["kva"], customers=ds["customers"], solar_kw=ds["solar_kw"], ev_chargers=ds["ev_chargers"],
                                             peak_utilisation=ds["est_peak_utilisation"], midday_reverse_flow=ds["est_midday_reverse_flow"]))
        customers = dict(n=nm.get("n") or ds["customers"], solar=nm.get("solar") or 0, ev=nm.get("ev") or 0, battery=nm.get("bat") or 0,
                         life_support=nm.get("ls") or 0, ds_id=ds["ds_id"])
    else:
        customers = dict(n=0, solar=0, ev=0, battery=0, life_support=0, ds_id=None)
    fault = _mark_fault(poles, spans, out)
    status = "red" if out else ("amber" if any(p["status"] != "green" for p in poles) else "green")
    return dict(kind="street", title=f"{feeder_id} street", feeder_id=feeder_id, poles=poles, spans=spans, customers=customers, fault=fault,
                outage=out, status=status, seed=feeder_id)


def rural(feeder_id):
    """Bushfire-prone line: the run of spans around the riskiest one, each with its LiDAR clearance, species and modelled fault probability."""
    out = _outage(feeder_id)
    worst = pq("""SELECT v.span_id, v.lat, v.lon FROM gld_vegetation_risk v WHERE v.feeder_id = %(f)s ORDER BY v.risk_score DESC LIMIT 1""",
               {"f": feeder_id}, ttl=1800)
    lat, lon = (out["lat"], out["lon"]) if out and out.get("lat") else ((worst[0]["lat"], worst[0]["lon"]) if worst else (0, 0))
    poles, spans = _run(feeder_id, lat, lon, 6)
    fault = _mark_fault(poles, spans, out)
    fd = [r for r in fire_danger() if "Blue Mountains" in (r.get("councils") or "")] or fire_danger()
    rating = max(fd, key=lambda r: r["rank_today"])["rating_today"] if fd else "MODERATE"
    district = max(fd, key=lambda r: r["rank_today"])["district"] if fd else None
    swer = any(d["kind"] == "swer_isolator" for p in poles for d in p["devices"])
    crew = any(s["status"] == "red" for s in spans)
    status = "red" if out or crew else ("amber" if any(s["status"] == "amber" for s in spans) else "green")
    return dict(kind="rural", title=f"{feeder_id} bushfire-prone line", feeder_id=feeder_id, poles=poles, spans=spans, fault=fault, outage=out,
                fire_rating=rating, fire_rating_label=(rating or "").title(), fire_district=district, swer=swer, veg_crew=crew, status=status, seed=feeder_id)


def estate(feeder_id):
    """Underground estate: padmount kiosks on the nature strip, the ring main unit, LV pillars, and what the estate plugs in."""
    out = _outage(feeder_id)
    ds = pq("SELECT * FROM gld_ds_state WHERE feeder_id = %(f)s AND ds_type = 'kiosk' ORDER BY customers DESC LIMIT 2", {"f": feeder_id}, ttl=1800)
    hub = pq("""SELECT * FROM gld_public_charging WHERE substation_ref IN (SELECT ds_id FROM gld_ds_state WHERE feeder_id = %(f)s)""",
             {"f": feeder_id}, ttl=3600)
    kiosks = [dict(asset_id=d["ds_id"], label=d["ds_id"], status=_st(d["status"]),
                   facts=dict(kva=d["kva"], customers=d["customers"], solar_kw=d["solar_kw"], ev_chargers=d["ev_chargers"],
                              peak_utilisation=d["est_peak_utilisation"], midday_reverse_flow=d["est_midday_reverse_flow"])) for d in ds]
    nm = pq("""SELECT count(*) n, sum(CASE WHEN solar_kw > 0 THEN 1 ELSE 0 END) solar, sum(has_ev_charger) ev, sum(has_battery) bat FROM gld_nmi
               WHERE ds_id = ANY(%(d)s::text[])""", {"d": [k["asset_id"] for k in kiosks]}, ttl=1800)
    nm = nm[0] if nm else {}
    status = "red" if out else ("amber" if any(k["status"] != "green" for k in kiosks) else "green")
    return dict(kind="estate", title=f"{feeder_id} underground estate", feeder_id=feeder_id, kiosks=kiosks,
                customers=dict(n=nm.get("n") or 0, solar=nm.get("solar") or 0, ev=nm.get("ev") or 0, battery=nm.get("bat") or 0),
                hub=dict(evse_count=hub[0]["evse_count"], name=hub[0]["site_id"]) if hub else None,
                outage=out, status=status, seed=feeder_id)


def for_entity(kind, eid):
    if kind == "zone":
        return zone(eid)
    if kind == "transmission":
        return transmission(eid)
    if kind == "feeder":
        if eid[:3] in RANGES:
            return rural(eid)
        under = pq("SELECT count(*) FILTER (WHERE ds_type = 'kiosk') k, count(*) n FROM gld_ds_state WHERE feeder_id = %(f)s", {"f": eid}, ttl=3600)
        return estate(eid) if under and under[0]["k"] > under[0]["n"] / 2 else street(eid)
    if kind in ("street", "rural", "estate"):
        return {"street": street, "rural": rural, "estate": estate}[kind](eid)
    return zone("ZS-KNV")


def sites():
    """The site list for the 3D view: every transmission substation (kind transmission, ts_id, the zones it feeds), then every zone
    substation (kind zone) with its feeders, grouped under the zone, and the kind of scene each feeder opens. Items of kind transmission
    carry zs_id None and no feeders, so a zone-only consumer can skip them by kind."""
    ts = pq("SELECT ts_id, name, status, zones FROM gld_transmission_substation ORDER BY name", ttl=3600)
    fd = pq("SELECT feeder_id, zs_id, status FROM gld_feeder_state ORDER BY feeder_id", ttl=600)
    zs = pq("SELECT asset_id, name FROM gld_asset WHERE asset_class = 'zone_substation' ORDER BY name", ttl=3600)
    under = {r["feeder_id"]: r["k"] > r["n"] / 2 for r in pq(
        "SELECT feeder_id, count(*) FILTER (WHERE ds_type = 'kiosk') k, count(*) n FROM gld_ds_state GROUP BY feeder_id", ttl=3600)}
    live = core.feeder_outages()
    tripped = {k for k, v in live.items() if v["tripped"]}
    out = [dict(kind="transmission", ts_id=t["ts_id"], zs_id=None, name=t["name"], zones=list(t["zones"] or []), status=_st(t["status"]),
                feeders=[]) for t in ts]
    for z in zs:
        feeders = [dict(feeder_id=f["feeder_id"], status="red" if f["feeder_id"] in tripped else "amber" if f["feeder_id"] in live else _st(f["status"]),
                        scene="Bushfire-prone line" if f["feeder_id"][:3] in RANGES else "Underground estate" if under.get(f["feeder_id"]) else "Overhead street")
                   for f in fd if f["zs_id"] == z["asset_id"]]
        out.append(dict(kind="zone", zs_id=z["asset_id"], name=z["name"], feeders=feeders))
    return out


_speech = {}
FALLBACK = {"red": "That one needs a crew today, boss. I'll get it on the board.",
            "amber": "Not broken yet, but I'd put it on next week's list.",
            "green": "All good here, boss. Nothing to fix on this one."}


def speech(asset_id, facts):
    key = (asset_id, json.dumps(facts, sort_keys=True, default=str))
    if key not in _speech:
        # the fast model reasons for ~550 tokens before it writes, so give it room; an empty or failed answer falls back and is not cached
        text = core.write(json.dumps(dict(asset=asset_id, facts=facts), default=str), SPEECH_SYS, max_tokens=2000)
        if not text or text.startswith("(briefing unavailable"):
            print(f"speech fallback for {asset_id}: {text[:200] if text else 'empty answer'}", flush=True)
            return FALLBACK.get((facts or {}).get("status"), FALLBACK["green"])
        _speech[key] = re.sub(r",?\s*\bmate\b", "", text, flags=re.I)   # in case the model says it anyway
    return _speech[key]
