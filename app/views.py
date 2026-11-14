"""Read models for the screens: Command Centre, map layers, Network, Operations, Plan and Invest, Customers, Explorer.
Every screen reads only Lakebase (synced gold in serving.*, live streams in live.*, working state in app.* and atlas.*)."""
import datetime as dt
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import core
from core import pg, pq
from rm import data as RD, dispatch as RX, plan as RP
from situations import ALERT_SEVERITIES, fire_danger, listing


def engine_state():
    rows = pg.q("SELECT key, value, updated_at FROM app.live_state WHERE key IN ('engine', 'feeders')")
    st = {r["key"]: r for r in rows}
    eng = (st.get("engine") or {}).get("value") or {}
    upd = (st.get("feeders") or {}).get("updated_at")
    run = core.live_run()
    fresh = bool(upd) and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(upd))).total_seconds() < 60
    live = eng.get("status") == "live" and fresh
    # phase drives the live ingestion switch: idle (off), starting (job queued or warming up), live (ticking, until the time box ends),
    # stopping (switched off, the run is ending)
    # a run whose engine has already started (its start is on the engine record) and has since paused is ending, not starting
    began = bool(run and run.get("start_ms") and eng.get("started") and eng.get("status") != "live"
                 and dt.datetime.fromisoformat(str(eng["started"])).timestamp() * 1000 >= run["start_ms"])
    phase = "live" if live else "stopping" if run and (eng.get("status") == "stopping" or began) else "starting" if run else "idle"
    until = None
    if eng.get("started") and eng.get("minutes"):
        try:
            until = (dt.datetime.fromisoformat(str(eng["started"])) + dt.timedelta(minutes=float(eng["minutes"]))).isoformat()
        except ValueError:
            until = None
    return dict(status=eng.get("status", "paused"), started=eng.get("started"), minutes=eng.get("minutes"), last_tick=upd,
                active_outages=eng.get("active_outages"), phase=phase, until=until if live else None, run_id=run and run["run_id"])


def command_centre():
    live = pg.q("""SELECT coalesce(sum(customers_off), 0) off, coalesce(sum(life_support_off), 0) ls, count(*) n FROM atlas.live_outage
                   WHERE status = 'active'""", one=True)
    sev = pg.q("SELECT severity, count(*) n FROM atlas.situation WHERE status <> 'closed' AND severity = ANY(%s) GROUP BY severity",
               (ALERT_SEVERITIES,))   # the same alerts the Alerts list shows
    rel = pq("SELECT * FROM gld_reliability_year ORDER BY regulatory_year DESC LIMIT 1", ttl=3600)
    rel_days = pq("""SELECT day, saidi, is_major_event_day FROM gld_reliability_daily
                     WHERE regulatory_year = (SELECT max(regulatory_year) FROM gld_reliability_daily) ORDER BY day""", ttl=3600)
    prev_days = pq("""SELECT day, saidi, is_major_event_day FROM gld_reliability_daily
                      WHERE regulatory_year = (SELECT max(regulatory_year) - 1 FROM gld_reliability_daily) ORDER BY day""", ttl=3600)
    spine = pq("SELECT * FROM gld_spine_status ORDER BY ord", ttl=600)
    for s in spine:
        if s["stop"] == "customers_der":
            s["red"] = int(live["off"])
            s["green"] = int(s["total"]) - int(live["off"]) - int(s["amber"])
        if s["stop"] == "hv_feeders":
            tripped = pg.q("SELECT count(DISTINCT feeder_id) n FROM atlas.live_outage WHERE status = 'active'", one=True)["n"]
            s["live_red"] = tripped
    fd = fire_danger()
    top = max(fd, key=lambda r: r["rank_today"]) if fd else None
    top_t = max(fd, key=lambda r: r["rank_tomorrow"]) if fd else None
    return dict(customers_off=int(live["off"]), life_support_off=int(live["ls"]), active_outages=live["n"],
                situations={r["severity"]: r["n"] for r in sev},
                crews=RX.summary(),
                reliability=rel[0] if rel else {}, reliability_days=rel_days, reliability_prev=prev_days, spine=spine,
                fire_danger=dict(today=top["rating_today"] if top else None, district=top["district"] if top else None,
                                 tomorrow=top_t["rating_tomorrow"] if top_t else None, district_tomorrow=top_t["district"] if top_t else None,
                                 fire_ban_today=any(r["fire_ban_today"] for r in fd), districts=fd),
                at_risk=_at_risk_summary(), long_outages=_long_outages(), engine=engine_state())


def finance():
    """Revenue, costs and profit for the executive view (gold gld_finance_month; illustrative rates, see pipelines/finance.py):
    per month for the last twelve months, and the regulatory year to date by line, actual against budget."""
    rows = pq("SELECT * FROM gld_finance_month ORDER BY month, sort_order", ttl=3600)
    if not rows:
        return {}
    groups = ("revenue", "opex", "depreciation", "finance_cost")
    def totals(rs, col):
        t = {g: sum(r[col] for r in rs if r["line_group"] == g) for g in groups}
        t["ebitda"] = t["revenue"] - t["opex"]
        t["profit"] = t["ebitda"] - t["depreciation"] - t["finance_cost"]
        return t
    months = []
    for m in sorted({r["month"] for r in rows}):
        months.append(dict(month=m, **totals([r for r in rows if r["month"] == m], "actual_aud")))
    year = max(r["regulatory_year"] for r in rows)
    ytd = [r for r in rows if r["regulatory_year"] == year]
    lines = {}
    for r in ytd:
        x = lines.setdefault(r["line"], dict(line_label=r["line_label"], line_group=r["line_group"], line_group_label=r["line_group_label"],
                                             sort_order=r["sort_order"], basis=r["basis"], is_twin_driven=r["is_twin_driven"], actual=0.0, budget=0.0))
        x["actual"] += r["actual_aud"]; x["budget"] += r["budget_aud"]
    return dict(regulatory_year=year, months_in_year=len({r["month"] for r in ytd}), ytd=totals(ytd, "actual_aud"),
                ytd_budget=totals(ytd, "budget_aud"), months=months, lines=sorted(lines.values(), key=lambda x: x["sort_order"]))


def _at_risk_summary():
    """Revenue at risk across the open critical, high and medium situations (see situations.RISK for how each is estimated)."""
    rows = listing()
    return dict(revenue=sum(r["revenue_at_risk"] or 0 for r in rows), situations=len(rows),
                customers=sum(r["customers_impacted"] or 0 for r in rows))


def _long_outages():
    """Customers still off supply more than 12 hours after their outage began: the point at which a distributor's guaranteed
    service level is breached and it owes the customer a payment."""
    r = pg.q("""SELECT coalesce(sum(customers_off), 0) customers, coalesce(sum(life_support_off), 0) life_support, count(*) outages
                FROM atlas.live_outage WHERE status = 'active' AND start_time < now() - interval '12 hours'""", one=True) or {}
    return dict(customers=int(r.get("customers") or 0), life_support=int(r.get("life_support") or 0), outages=int(r.get("outages") or 0))


def _geo(v):
    """A GeoJSON shape as stored (text) parsed into a dict; a missing shape stays None."""
    return json.loads(v) if isinstance(v, str) else v


# ------------------------------------------------------------------ map layers
def map_network():
    # the feeder's overhead spans and cable sections, already unioned and simplified into one GeoJSON shape per feeder (gld_feeder_geo)
    feeders = pq("""SELECT f.feeder_id, f.feeder_name, f.status, f.customers, f.area_label, g.geo
                    FROM gld_feeder_state f JOIN gld_feeder_geo g USING (feeder_id)""", ttl=3600)
    zs = pq("SELECT asset_id, name, lat, lon, material area FROM gld_asset WHERE asset_class = 'zone_substation'", ttl=3600)
    feeders = [dict(f, geo=_geo(f["geo"])) for f in feeders]
    # the transmission level: the 132 kV circuits from the bulk supply points, and the transmission substations they reach
    ts = pq("""SELECT ts_id, name, area, kv, secondary_kv, transformers, transformer_mva, firm_mva, status, lat, lon, zone_count
               FROM gld_transmission_substation""", ttl=3600)
    tl = pq("SELECT line_id, bsp_id, ts_id, circuit, kv, route_km, geo FROM gld_transmission_line", ttl=3600)
    return dict(feeders=feeders, zones=zs, transmission=ts, tlines=[dict(l, geo=_geo(l["geo"])) for l in tl])


def impact():
    """Everything the Impact analysis needs to trace who depends on what: bulk supply points, the circuits into each zone substation,
    zone substations, feeders (status, live trips, backup ties) and suburbs with the cohorts that matter in an outage."""
    bsps = pq("SELECT bsp_id, name, area, kv, capacity_mva, transformers, lat, lon FROM gld_bulk_supply ORDER BY bsp_id", ttl=3600)
    transmission = pq("""SELECT ts_id, name, area, kv, secondary_kv, transformers, transformer_mva, firm_mva, bsp_id, lat, lon, zone_count,
                                customers, single_transmission_zones FROM gld_transmission_substation ORDER BY ts_id""", ttl=3600)
    tlines = pq("SELECT line_id, bsp_id, ts_id, circuit, kv, route_km, geo FROM gld_transmission_line ORDER BY line_id", ttl=3600)
    lines = pq("SELECT line_id, ts_id, bsp_id, zs_id, circuit, kv, route_km, geo FROM gld_subtransmission ORDER BY line_id", ttl=3600)
    zones = pq("""SELECT s.*, a.lat, a.lon, coalesce(t.status, 'green') status FROM gld_zone_supply s
                  JOIN gld_asset a ON a.asset_id = s.zs_id
                  LEFT JOIN (SELECT zs_id, CASE WHEN bool_or(status = 'red') THEN 'red' WHEN bool_or(status = 'amber') THEN 'amber' ELSE 'green' END status
                             FROM gld_zone_transformer_state GROUP BY zs_id) t ON t.zs_id = s.zs_id ORDER BY s.zs_name""", ttl=600)
    feeders = pq("""SELECT feeder_id, feeder_name, zs_id, area, area_label, status, customers, life_support, critical_sites,
                           rating_amps, emergency_amps, amps_peak_30d, spare_amps, limited_by
                    FROM gld_feeder_state ORDER BY feeder_id""", ttl=600)
    ties = pq("SELECT nop, feeder_id, tie_feeder, tie_zs_id, other_zone, transfer_amps, transfer_share FROM gld_feeder_tie", ttl=3600)
    suburbs = pq("SELECT * FROM gld_suburb_supply", ttl=3600)
    out = core.feeder_outages()
    for f in feeders:
        o = out.get(f["feeder_id"]) or {}
        f["tripped"] = bool(o.get("tripped"))
        f["spur_outage"] = bool(o) and not o.get("tripped")   # a fuse has operated on a spur; the feeder stays in service
        f["customers_off"] = o.get("customers_off", 0)
        f["ties"] = [dict(nop=t["nop"], to=t["tie_feeder"], other_zone=t["other_zone"], transfer_amps=t["transfer_amps"], transfer_share=t["transfer_share"])
                     for t in ties if t["feeder_id"] == f["feeder_id"]]
    for l in lines + tlines:
        l["geo"] = _geo(l["geo"])
    return dict(bsps=bsps, transmission=transmission, tlines=tlines, lines=lines, zones=zones, feeders=feeders, suburbs=suburbs)


def impact_assets():
    """Distribution substations for the asset hierarchy view of Impact analysis (each under its feeder), with the cohorts that matter in an
    outage counted from the customers each supplies, and how many are off supply now (behind a device that has operated)."""
    ds = pq("""SELECT n.ds_id, n.feeder_id, count(*) AS customers, count(*) FILTER (WHERE n.life_support) AS life_support,
                      count(*) FILTER (WHERE n.critical_site IS NOT NULL) AS critical_sites,
                      count(*) FILTER (WHERE n.critical_site IS NOT NULL AND NOT coalesce(n.backup_generator, false)) AS critical_no_generator,
                      count(*) FILTER (WHERE n.solar_kw > 0) AS solar
               FROM gld_nmi n WHERE n.ds_id IS NOT NULL GROUP BY n.ds_id, n.feeder_id ORDER BY n.feeder_id, n.ds_id""", ttl=3600)
    off = {r["ds_id"]: int(r["n"]) for r in pg.q("""SELECT n.ds_id, count(*) n FROM gld_nmi n JOIN atlas.live_outage o
                                                     ON o.status = 'active' AND o.device_id = n.protective_device GROUP BY n.ds_id""")}
    for d in ds:
        d["customers_off"] = off.get(d["ds_id"], 0)
    return dict(ds=ds)


def map_live():
    outages = pg.q("SELECT outage_id, feeder_id, device_id, customers_off, life_support_off, lat, lon, start_time, situation_id "
                   "FROM atlas.live_outage WHERE status = 'active'")
    return dict(outages=outages, crews=RP.positions())


def map_fire():
    fires = pq("""SELECT incident_id, title, alert_level, status, incident_type, size, lat, lon, perimeter_geojson, updated, captured_at
                  FROM gld_fire_incident WHERE lat BETWEEN -35.2 AND -32.0 AND lon BETWEEN 149.0 AND 152.5""", ttl=300)
    hs = pq("SELECT hotspot_id, satellite, observed_at, lat, lon, confidence, hours_since FROM gld_hotspot", ttl=600)
    return dict(fires=fires, hotspots=hs, attribution={
        "fires": "State of New South Wales (NSW Rural Fire Service), CC BY 4.0", "hotspots": "DEA Hotspots, Geoscience Australia, CC BY 4.0"})


def map_weather(hour=None):
    times = pq("SELECT DISTINCT forecast_time t FROM gld_weather_cell ORDER BY t", ttl=900)
    t = hour or (times[min(len(times) - 1, 12)]["t"] if times else None)
    cells = pq("""SELECT h3, geo, round(max(gust_kmh)::numeric) gust, round(max(rain_mm)::numeric, 1) rain, round(max(temp_c)::numeric) temp
                  FROM gld_weather_cell WHERE forecast_time = %(t)s GROUP BY h3, geo""", {"t": t}, ttl=900) if t else []
    cells = [dict(c, geo=_geo(c["geo"])) for c in cells]
    return dict(time=t, times=[x["t"] for x in times], cells=cells, attribution="Open-Meteo forecast (CC BY 4.0)")


# ------------------------------------------------------------------ an area on the map: who and what is inside it
# Every shape is a PostGIS geography (metres on the ellipsoid). Points are ST_Covers(area, point); lines cross the area with
# ST_Intersects. The gld_asset and gld_nmi expressions match the GiST indexes laid over serving.* in serving/tables.py.
AREA_CUSTOMERS_SQL = """-- Customers inside the area, by suburb and feeder (PostGIS on Lakebase: geography in metres, ST_Covers on each customer point)
WITH area AS (SELECT {area} AS g)
SELECT n.suburb, n.postcode, n.feeder_id, count(*) AS customers,
       count(*) FILTER (WHERE n.life_support) AS life_support, count(n.critical_site) AS critical_sites,
       count(*) FILTER (WHERE n.critical_site IS NOT NULL AND NOT coalesce(n.backup_generator, false)) AS critical_no_generator,
       count(*) FILTER (WHERE n.solar_kw > 0) AS solar,
       count(*) FILTER (WHERE n.sms_opt_in) AS sms, count(*) FILTER (WHERE n.email_opt_in) AS email,
       count(*) FILTER (WHERE n.facebook_opt_in) AS facebook,
       count(*) FILTER (WHERE NOT (n.sms_opt_in OR n.email_opt_in OR n.facebook_opt_in)) AS no_channel,
       count(*) FILTER (WHERE n.life_support AND n.sms_opt_in) AS ls_sms, count(*) FILTER (WHERE n.life_support AND n.email_opt_in) AS ls_email,
       count(*) FILTER (WHERE n.life_support AND n.facebook_opt_in) AS ls_facebook
FROM gld_nmi n JOIN area a ON ST_Covers(a.g, ST_SetSRID(ST_MakePoint(n.lon, n.lat), 4326)::geography)
GROUP BY n.suburb, n.postcode, n.feeder_id"""

AREA_ASSETS_SQL = """-- Network assets inside the area: lines that cross it, and poles, substations and devices that stand in it (PostGIS on Lakebase)
WITH area AS (SELECT {area} AS g),
hit AS (
  SELECT x.asset_class, x.asset_class_label, x.feeder_id, x.customers_downstream
  FROM gld_asset x JOIN area a ON ST_Intersects(a.g, ST_GeogFromText(x.geom_wkt))
  WHERE x.asset_class IN ('overhead_span', 'cable_section')
  UNION ALL
  SELECT x.asset_class, x.asset_class_label, x.feeder_id, x.customers_downstream
  FROM gld_asset x JOIN area a ON ST_Covers(a.g, ST_SetSRID(ST_MakePoint(x.lon, x.lat), 4326)::geography)
  WHERE x.asset_class NOT IN ('overhead_span', 'cable_section')
)
SELECT asset_class, asset_class_label, feeder_id, count(*) AS assets, max(customers_downstream) AS customers_behind
FROM hit GROUP BY asset_class, asset_class_label, feeder_id"""

AREA_DS_SQL = """-- Distribution substations inside the area (PostGIS on Lakebase)
WITH area AS (SELECT {area} AS g)
SELECT d.ds_id, d.feeder_id, d.ds_type, d.kva, d.customers, d.status
FROM gld_ds_state d JOIN area a ON ST_Covers(a.g, ST_SetSRID(ST_MakePoint(d.lon, d.lat), 4326)::geography)
ORDER BY d.customers DESC LIMIT 200"""


def _area_expr(spec):
    """The area as a geography: a circle around a point (10 m to 100 km) or a polygon drawn on the map (up to 200 vertices). Returns the
    SQL expression with its %(name)s parameters, the parameter values and a description for the screen. Values are bound, never written
    into the SQL text."""
    if spec.get("shape") == "polygon":
        pts = [(float(p[0]), float(p[1])) for p in spec["points"]][:200]
        if len(pts) < 3:
            raise ValueError("An area needs at least three points")
        ring = pts + [pts[0]]
        wkt = "SRID=4326;POLYGON((" + ", ".join(f"{lon:.6f} {lat:.6f}" for lat, lon in ring) + "))"
        return "ST_GeogFromText(%(wkt)s)", dict(wkt=wkt), dict(shape="polygon", points=len(pts))
    lat, lon, r = float(spec["lat"]), float(spec["lon"]), int(min(100000, max(10, float(spec["radius_m"]))))
    return ("ST_Buffer(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(r)s)", dict(lon=lon, lat=lat, r=r),
            dict(shape="circle", lat=lat, lon=lon, radius_m=r))


def _shown_sql(text, params):
    """The query as the screen shows it: the bound values written in place. For display only; the query itself runs with parameters."""
    lit = {k: f"'{v}'" if isinstance(v, str) else repr(v) for k, v in params.items()}
    return text % lit


def area_impact(spec):
    expr, params, desc = _area_expr(spec)
    sqls = [t.format(area=expr) for t in (AREA_CUSTOMERS_SQL, AREA_ASSETS_SQL, AREA_DS_SQL)]
    with ThreadPoolExecutor(3) as ex:
        cust, assets, dss = ex.map(lambda q: pq(q, params, ttl=120), sqls)
    keys = ("customers", "life_support", "critical_sites", "critical_no_generator", "solar", "sms", "email", "facebook", "no_channel",
            "ls_sms", "ls_email", "ls_facebook")
    total = {k: sum(int(r[k] or 0) for r in cust) for k in keys}
    subs = {}
    for r in cust:
        s = subs.setdefault((r["suburb"], r["postcode"]), dict(suburb=r["suburb"], postcode=r["postcode"], customers=0, life_support=0, critical_sites=0, feeders=set()))
        for k in ("customers", "life_support", "critical_sites"):
            s[k] += int(r[k] or 0)
        s["feeders"].add(r["feeder_id"])
    suburbs = sorted(({**v, "feeders": len(v["feeders"])} for v in subs.values()), key=lambda x: -x["customers"])
    fstate = {f["feeder_id"]: f for f in pq("SELECT feeder_id, feeder_name, status FROM gld_feeder_state", ttl=600)}
    feeders = {}
    for r in cust:
        f = feeders.setdefault(r["feeder_id"], dict(feeder_id=r["feeder_id"], customers=0, life_support=0, spans=0, poles=0))
        f["customers"] += int(r["customers"] or 0); f["life_support"] += int(r["life_support"] or 0)
    classes = {}
    for a in assets:
        classes[a["asset_class_label"]] = classes.get(a["asset_class_label"], 0) + int(a["assets"] or 0)
        if a["feeder_id"]:
            f = feeders.setdefault(a["feeder_id"], dict(feeder_id=a["feeder_id"], customers=0, life_support=0, spans=0, poles=0))
            if a["asset_class"] in ("overhead_span", "cable_section"):
                f["spans"] += int(a["assets"] or 0)
            if a["asset_class"] == "pole":
                f["poles"] += int(a["assets"] or 0)
    for f in feeders.values():
        f.update(feeder_name=(fstate.get(f["feeder_id"]) or {}).get("feeder_name"), status=(fstate.get(f["feeder_id"]) or {}).get("status"))
    zones = [a for a in assets if a["asset_class"] == "zone_substation"]
    return dict(area=desc, totals=total, suburbs=suburbs[:80], suburb_count=len(suburbs),
                feeders=sorted(feeders.values(), key=lambda x: -x["customers"]),
                assets=sorted(({"asset": k, "count": v} for k, v in classes.items()), key=lambda x: -x["count"]),
                distribution_substations=dss, zone_substations=len(zones), sql=[_shown_sql(q, params) for q in sqls])


def notify_area(spec, body, channels, audience, actor):
    """Queue a notice to every customer inside the area on each chosen channel they have registered for (delivery is simulated)."""
    expr, params, desc = _area_expr(spec)
    channels = [c for c in channels if c in ("sms", "email", "facebook")]
    if not channels or not (body or "").strip():
        return dict(sent=0, message="Choose at least one channel and write the message.")
    flag = {"sms": "sms_opt_in", "email": "email_opt_in", "facebook": "facebook_opt_in"}
    only_ls = "WHERE n.life_support" if audience == "life_support" else ""
    rows = pq(f"""WITH area AS (SELECT {expr} AS g)
                  SELECT n.nmi, n.life_support, n.sms_opt_in, n.email_opt_in, n.facebook_opt_in
                  FROM gld_nmi n JOIN area a ON ST_Covers(a.g, ST_SetSRID(ST_MakePoint(n.lon, n.lat), 4326)::geography) {only_ls}""", params)
    batch = f"AREA-{uuid.uuid4().hex[:8].upper()}"
    msgs = [dict(m=f"MSG-{uuid.uuid4().hex[:12]}", n=r["nmi"], a="life_support" if r["life_support"] else "customer", c=c)
            for r in rows for c in channels if r[flag[c]]]
    pg.q("""INSERT INTO atlas.notification_outbox (msg_id, situation_id, rec_id, nmi, audience, channel, body, status, sent_at)
            SELECT x ->> 'm', %s, NULL, x ->> 'n', x ->> 'a', x ->> 'c', %s, 'sent', now() FROM jsonb_array_elements(%s::jsonb) x""",
         (batch, body.strip(), json.dumps(msgs)))
    by = {c: sum(1 for m in msgs if m["c"] == c) for c in channels}
    names = {"sms": "SMS", "email": "email", "facebook": "Facebook Messenger"}
    summary = ", ".join(f"{v:,} {names.get(k, k)}" for k, v in by.items())
    reached = len({m["n"] for m in msgs})
    core.audit(actor, "notify_area", batch, dict(area=desc, audience=audience, by_channel=by, customers=reached))
    return dict(sent=len(msgs), batch=batch, by_channel=by, customers_reached=reached, customers_in_area=len(rows),
                message=f"{len(msgs):,} {'notice' if len(msgs) == 1 else 'notices'} sent to {reached:,} of {len(rows):,} {'customer' if len(rows) == 1 else 'customers'} ({summary}).")


# ------------------------------------------------------------------ Network
def network(stop):
    live = (pg.q("SELECT value FROM app.live_state WHERE key = 'feeders'", one=True) or {}).get("value") or {}
    livez = (pg.q("SELECT value FROM app.live_state WHERE key = 'zone_transformers'", one=True) or {}).get("value") or {}
    if stop == "bulk_supply":
        bsps = pq("SELECT bsp_id, name, area, kv, capacity_mva, transformers, transmission_substations FROM gld_bulk_supply ORDER BY bsp_id", ttl=3600)
        lines = pq("SELECT bsp_id, zs_id FROM gld_subtransmission", ttl=3600)
        feeds = {}
        for l in lines:
            feeds.setdefault(l["zs_id"], set()).add(l["bsp_id"])
        out = []
        for b in bsps:
            zones = sorted(z for z, f in feeds.items() if b["bsp_id"] in f)
            # a zone fed from two bulk supply points draws on each about equally in normal running
            mw = sum(((livez.get(f"{z}-TX1") or {}).get("mw", 0) + (livez.get(f"{z}-TX2") or {}).get("mw", 0)) / len(feeds[z]) for z in zones)
            out.append(dict(bsp_id=b["bsp_id"], name=b["name"], zones=len(zones), mw_now=round(mw, 1), capacity_mva=b["capacity_mva"],
                            transformers=b["transformers"], transmission_substations=b["transmission_substations"], status="green"))
        return dict(rows=out)
    if stop == "transmission_substations":
        rows = pq("SELECT * FROM gld_transmission_substation ORDER BY status = 'red' DESC, peak_utilisation_30d DESC NULLS LAST", ttl=600)
        tx = pq("SELECT * FROM gld_transmission_transformer_state ORDER BY ts_id, transformer_id", ttl=600)
        for r in rows:
            r["transformer_rows"] = [t for t in tx if t["ts_id"] == r["ts_id"]]   # (transformers is the count)
        return dict(rows=rows)
    if stop == "zone_substations":
        rows = pq("SELECT * FROM gld_zone_transformer_state ORDER BY status = 'red' DESC, peak_utilisation_30d DESC", ttl=600)
        for r in rows:
            lv = livez.get(r["transformer_id"]) or {}
            r["mw_live"], r["top_oil_live"] = lv.get("mw"), lv.get("top_oil")
        return dict(rows=rows)
    if stop == "hv_feeders":
        rows = pq("SELECT * FROM gld_feeder_state ORDER BY CASE status WHEN 'red' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END, cmos_12m DESC", ttl=600)
        out = core.feeder_outages()
        for r in rows:
            lv = live.get(r["feeder_id"]) or {}
            r["amps_live"], r["cb_closed"] = lv.get("amps"), lv.get("cb", 1) == 1
            o = out.get(r["feeder_id"]) or {}
            r["tripped"], r["spur_outage"] = bool(o.get("tripped")), bool(o) and not o.get("tripped")
        return dict(rows=rows)
    if stop == "distribution_lv":
        rows = pq("""SELECT * FROM gld_ds_state ORDER BY CASE status WHEN 'red' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END,
                     greatest(est_peak_utilisation, est_midday_reverse_flow) DESC LIMIT 200""", ttl=900)
        summary = pq("SELECT status, count(*) n FROM gld_ds_state GROUP BY status", ttl=900)
        return dict(rows=rows, summary=summary)
    if stop == "customers_der":
        by_zone = pq("""SELECT n.zs_id, count(*) customers, sum(n.life_support::int) life_support, count(n.critical_site) critical,
                               round(sum(n.solar_kw)::numeric / 1000, 1) solar_mw, sum(n.has_battery) batteries, sum(n.has_ev_charger) ev_chargers,
                               count(DISTINCT n.feeder_id) feeders, count(DISTINCT n.ds_id) ds, count(DISTINCT n.suburb) suburbs, count(m.nmi) mismatched
                        FROM gld_nmi n LEFT JOIN gld_dq_meter_mismatch m USING (nmi) GROUP BY n.zs_id ORDER BY customers DESC""", ttl=3600)
        return dict(rows=by_zone)
    return dict(rows=[])


def zone_customers(zs):
    """One zone substation's customers for the Network drawer: by customer type, the largest suburbs, and its critical sites."""
    p = {"z": zs}
    types = pq("""SELECT customer_type_label label, count(*) customers, sum(life_support::int) life_support,
                  round(sum(solar_kw)::numeric / 1000, 1) solar_mw FROM gld_nmi WHERE zs_id = %(z)s
                  GROUP BY customer_type_label ORDER BY customers DESC""", p, ttl=3600)
    suburbs = pq("""SELECT suburb, count(*) customers, sum(life_support::int) life_support FROM gld_nmi
                    WHERE zs_id = %(z)s GROUP BY suburb ORDER BY customers DESC LIMIT 12""", p, ttl=3600)
    sites = pq("""SELECT n.site_name, l.label site_type, n.suburb, n.backup_generator FROM gld_nmi n
                  LEFT JOIN ref_label l ON l.domain = 'critical_site' AND l.code = n.critical_site
                  WHERE n.zs_id = %(z)s AND n.critical_site IS NOT NULL ORDER BY n.site_name""", p, ttl=3600)
    return dict(types=types, suburbs=suburbs, sites=sites)


# the historian and the interval marts end at the twin's as-of day; the windows below count back from it
def _since(days):
    return core.AS_OF - dt.timedelta(days=days)


def series_transformer(tx):
    h = pq("""SELECT hour t, attribute, round(value::numeric, 2) v FROM gld_historian_hourly
              WHERE element = %(e)s AND attribute IN ('MW', 'TOP_OIL_TEMP') AND hour >= %(s)s ORDER BY t""", {"e": tx, "s": _since(14)}, ttl=1800)
    live = pq("""SELECT date_trunc('minute', ts) t, attribute, round(avg(value)::numeric, 2) v FROM live.scada
                 WHERE element = %(e)s AND ts > now() - interval '3 hours'
                 GROUP BY date_trunc('minute', ts), attribute ORDER BY t""", {"e": tx}, ttl=15)
    return dict(history=h, live=live)


def series_feeder(fid):
    h = pq("""SELECT hour t, round(value) amps FROM gld_historian_hourly
              WHERE element = %(e)s AND attribute = 'AMPS' AND hour >= %(s)s ORDER BY t""", {"e": fid, "s": _since(14)}, ttl=1800)
    live = pq("""SELECT date_trunc('minute', ts) t, round(avg(value)) amps FROM live.scada
                 WHERE element = %(e)s AND attribute = 'AMPS' AND ts > now() - interval '3 hours'
                 GROUP BY date_trunc('minute', ts) ORDER BY t""", {"e": fid}, ttl=15)
    out = pq("""SELECT outage_id, start_time, duration_min, customers_interrupted, cause_label, device_label, momentary FROM gld_outage
                WHERE feeder_id = %(f)s ORDER BY start_time DESC LIMIT 12""", {"f": fid}, ttl=1800)
    return dict(history=h, live=live, outages=out)


# ------------------------------------------------------------------ search (Asset management, Search tab)
def search(q):
    """One search across the asset register, customers (NMI or street address) and crews, read from the synced gold in Lakebase."""
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    # assets: the exact ID first, then IDs that start with the text, then the rest
    rows = pq("""SELECT asset_id id, asset_class_label kind, name, feeder_id, zs_id FROM gld_asset
                 WHERE asset_id ILIKE %(l)s OR name ILIKE %(l)s OR sap_equnr ILIKE %(l)s
                 ORDER BY upper(asset_id) = upper(%(q)s) DESC, asset_id ILIKE %(s)s DESC, asset_id LIMIT 12""",
              {"l": like, "q": q, "s": f"{q}%"}, ttl=300)
    if not q.isdigit() or len(q) >= 6:   # an NMI matches from its start; an address anywhere
        cond = "nmi LIKE %(p)s" if q.isdigit() else "address ILIKE %(l)s"
        rows += pq(f"""SELECT nmi id, 'Customer (NMI)' kind, address name, feeder_id, zs_id FROM gld_nmi WHERE {cond}
                       ORDER BY nmi = %(q)s DESC, nmi LIMIT 12""",
                   {"p": f"{q}%", "l": like, "q": q}, ttl=300)
    u = q.upper()
    rows += [dict(id=c["crew_id"], kind="Crew", name=f"{c['crew_name']}, {c['persona']}, {RD.region_name(c['region_id'])}", feeder_id=None, zs_id=None)
             for c in RD.data()["crews"] if u in c["crew_id"].upper() or u in c["crew_name"].upper()][:12]
    return rows


def related_assets(aid):
    """The asset and the one it works with in the field: a pole and the overhead span that runs into it share their number (P215001 and
    S215001), so work on either shows on both."""
    if len(aid) > 1 and aid[0] in "PS" and aid[1:].isdigit():
        return [aid, ("S" if aid[0] == "P" else "P") + aid[1:]]
    return [aid]


def asset(aid):
    if aid.isdigit() and len(aid) == 10:
        n = pq("SELECT * FROM gld_nmi WHERE nmi = %(n)s", {"n": aid}, ttl=300)
        if not n:
            return None
        n = n[0]
        iv = pq("""SELECT interval_end t, register, kwh FROM gld_meter_interval_recent WHERE nmi = %(n)s AND interval_date >= %(d)s
                   ORDER BY t""", {"n": aid, "d": _since(3)}, ttl=600)
        return dict(kind="nmi", record=n, intervals=iv, systems=[("Market standing data", aid), ("Connectivity model", f"UP-{aid}"),
                                                                  ("GIS service point", aid), ("Supplied by", n.get("ds_id"))])
    a = pq("SELECT * FROM gld_asset WHERE asset_id = %(a)s", {"a": aid}, ttl=300)
    if not a and aid.startswith("ZS-") and "-TX" in aid:
        t = pq("SELECT * FROM gld_zone_transformer_state WHERE transformer_id = %(a)s", {"a": aid}, ttl=300)
        if not t:
            return None
        t = t[0]
        return dict(kind="asset", record=dict(asset_id=aid, name=t["name"], asset_class="zone_transformer", asset_class_label="Zone substation transformer",
                                              feeder_id=None, zs_id=t["zs_id"], install_year=t["year_built"], material=t["manufacturer"], rating=t["rating_mva"],
                                              rating_unit="MVA", open_defects=0, customers_downstream=None),
                    defects=[], orders=[], vegetation=None, fault_probability=None, outages=[],
                    systems=[("Twin", aid), ("Asset register (SAP equipment)", t.get("sap_equnr")), ("Historian element", aid),
                             ("Connectivity model (CIM mRID)", aid), ("Health", f"{t['status']}: H2 {t['h2_now']} ppm, up {t['h2_rise_90d']} in 90 days")])
    if not a:
        return _circuit(aid)
    a = a[0]
    site = pq("SELECT * FROM gld_transmission_substation WHERE ts_id = %(a)s", {"a": aid}, ttl=300) if a["asset_class"] == "transmission_substation" else []
    txs = pq("SELECT * FROM gld_transmission_transformer_state WHERE transformer_id = %(a)s", {"a": aid}, ttl=300) \
        if a["asset_class"] == "transmission_transformer" else []
    # a defect a reviewer closed from a field photo is no longer open, whatever the synced register says
    defects = pq("""SELECT * FROM gld_open_defect d WHERE asset_id = %(a)s
                    AND NOT EXISTS (SELECT 1 FROM atlas.defect_closure c WHERE c.notification_id = d.notification_id)""", {"a": aid}, ttl=30)
    # status in words from ref_label (domain order_status); the two SAP statuses in the extract have the same words until that label sync
    orders = pq("""SELECT w.order_id, w.asset_id, w.order_type_label, w.description, w.created_date, w.completed_date, w.cost_aud,
                          coalesce(l.label, CASE w.status WHEN 'TECO' THEN 'Complete' WHEN 'REL' THEN 'Released, work under way' END, w.status) AS status_label
                   FROM gld_work_order w LEFT JOIN ref_label l ON l.domain = 'order_status' AND l.code = w.status
                   WHERE w.asset_id = ANY(%(ids)s) ORDER BY w.created_date DESC LIMIT 12""", {"ids": related_assets(aid)}, ttl=300)
    veg = pq("SELECT * FROM gld_vegetation_risk WHERE span_id = %(a)s", {"a": aid}, ttl=300) if a["asset_class"] == "overhead_span" else []
    risk = pq("SELECT fault_probability FROM ml_span_risk WHERE span_id = %(a)s", {"a": aid}, ttl=300) if a["asset_class"] == "overhead_span" else []
    outages = pq("""SELECT outage_id, start_time, duration_min, customers_interrupted, cause_label FROM gld_outage
                    WHERE device_id = %(a)s OR span_id = %(a)s ORDER BY start_time DESC LIMIT 6""", {"a": aid}, ttl=300)
    systems = [("Twin", a["asset_id"]), ("Asset register (SAP equipment)", a.get("sap_equnr")), ("GIS feature", a.get("gis_fid")),
               ("Connectivity model (CIM mRID)", a.get("cim_mrid")), ("Protected by", a.get("protective_device")),
               ("Sectionalised by", a.get("sectionalising_device"))]
    return dict(kind="asset", record=a, defects=defects, orders=orders, vegetation=veg[0] if veg else None,
                fault_probability=risk[0]["fault_probability"] if risk else None, outages=outages, systems=[s for s in systems if s[1]],
                site=site[0] if site else None, transformer=txs[0] if txs else None, **asset_story(aid, a))


def _circuit(aid):
    """A sub-transmission (33 or 66 kV, transmission to zone) or 132 kV (bulk supply to transmission) circuit: they are lines, not point
    assets, so they live in their own marts rather than gld_asset. The drawer shows the circuit's ends, voltage, rating and route."""
    s = pq("""SELECT l.*, t.name from_name, a.name to_name, a.customers_downstream customers FROM gld_subtransmission l
              LEFT JOIN gld_transmission_substation t ON t.ts_id = l.ts_id LEFT JOIN gld_asset a ON a.asset_id = l.zs_id
              WHERE l.line_id = %(a)s""", {"a": aid}, ttl=300)
    if s:
        c, cls, label = s[0], "subtransmission_circuit", "Sub-transmission circuit"
    else:
        t = pq("""SELECT l.*, b.name from_name, t.name to_name, t.customers FROM gld_transmission_line l
                  LEFT JOIN gld_bulk_supply b ON b.bsp_id = l.bsp_id LEFT JOIN gld_transmission_substation t ON t.ts_id = l.ts_id
                  WHERE l.line_id = %(a)s""", {"a": aid}, ttl=300)
        if not t:
            return None
        c, cls, label = t[0], "transmission_circuit", "132 kV transmission circuit"
    return dict(kind="asset", record=dict(asset_id=aid, name=f"{c['kv']} kV circuit {c['circuit']}, {c['from_name']} to {c['to_name']}",
                                          asset_class=cls, asset_class_label=label, feeder_id=None, zs_id=c.get("zs_id"),
                                          install_year=c.get("install_year"), material=None, rating=c["rating_mva"], rating_unit="MVA",
                                          open_defects=0, customers_downstream=c.get("customers")),
                defects=[], orders=[], vegetation=None, fault_probability=None, outages=[],
                systems=[("Twin", aid), ("From", c["from_name"]), ("To", c["to_name"]), ("Route", f"{c['route_km']} km")])


def asset_story(aid, a):
    """Everything happening on and around one asset, for its drawer: the jobs (raised in the app and planned, with their field status
    and crew), the crews' field reports, the photos and their review, the open situations on its feeder or naming it, and the
    work order spend."""
    from fieldreport import label, shape
    raised = pg.q("""SELECT r.job_id, r.title, r.job_type, r.priority, r.job_date, r.region_id, r.source, r.is_emergency, r.created_at,
                            f.status, o.payload->>'crew_name' crew_name, o.payload->>'crew_id' crew_id, (o.payload->>'dispatched')::boolean dispatched,
                            EXISTS (SELECT 1 FROM rm.parked k WHERE k.job_id = r.job_id) parked
                     FROM rm.raised_job r LEFT JOIN rm.field_status f ON f.job_id = r.job_id AND f.region_id = r.region_id
                     LEFT JOIN rm.assignment_override o ON o.job_id = r.job_id AND o.region_id = r.region_id
                     WHERE r.asset_id = %s ORDER BY r.created_at DESC LIMIT 20""", (aid,))
    planned = pq("""SELECT j.job_id, j.title, j.job_type, j.priority, j.region_id, j.source_label, j.job_date - %(asof)s::date day_offset,
                           f.status, o.payload->>'crew_name' crew_name
                    FROM gld_field_job j LEFT JOIN rm.field_status f ON f.job_id = j.job_id AND f.region_id = j.region_id
                    LEFT JOIN rm.assignment_override o ON o.job_id = j.job_id AND o.region_id = j.region_id
                    WHERE j.asset_id = %(a)s ORDER BY j.job_date LIMIT 20""", {"a": aid, "asof": core.AS_OF.isoformat()})
    types = {t["code"]: t["label"] for t in pq("SELECT code, label FROM ref_label WHERE domain = 'job_type'", ttl=600)}
    rosters = {}

    def crew_on_plan(region, job_id):   # the crew today's plan gives the job, when no one has been sent by hand
        try:
            if region not in rosters:
                rosters[region] = {x["job_id"]: x for x in RP.live_roster(region)["assignments"]}
            x = rosters[region].get(job_id)
            return x and (x.get("crew_name") or x.get("crew_id"))
        except Exception:
            return None
    jobs = []
    today = RD.today()
    for j in [dict(x, from_app=True) for x in raised] + planned:
        st = j.get("status") or ("parked" if j.get("parked") else None)
        crew = j.get("crew_name") or (crew_on_plan(j["region_id"], j["job_id"]) if st != "done" else None)
        when = j.get("job_date") or (today + dt.timedelta(days=int(j["day_offset"]))).isoformat()
        jobs.append(dict(job_id=j["job_id"], title=j["title"], job_type_label=types.get(j["job_type"], j["job_type"]), priority=j["priority"],
                         region_id=j["region_id"], region_name=RD.region_name(j["region_id"]), when=str(when), raised_at=j.get("created_at"),
                         origin="Raised in the app" if j.get("from_app") else (j.get("source_label") or "Planned work order"),
                         emergency=bool(j.get("is_emergency")), status=st or "planned",
                         status_label=label("ops_job_status", st or "planned") if crew or st else "Waiting for a crew",
                         crew_name=crew, dispatched=bool(j.get("dispatched")), open=(st or "planned") != "done"))
    job_ids = [j["job_id"] for j in jobs]
    rep_rows = [shape(r) for r in pg.q("""SELECT r.*, g.crew_name, g.persona FROM rm.field_report r LEFT JOIN gld_crew g ON g.crew_id = r.crew_id
                                           WHERE r.asset_id = %s OR r.job_id = ANY(%s) ORDER BY r.reported_at DESC LIMIT 20""", (aid, job_ids))]
    photos = pg.q("""SELECT photo_id, status, note, submitted_by, submitted_at, review_decision, review_notes, image_deleted, ai_result->>'summary' ai_summary
                     FROM atlas.asset_photo WHERE asset_id = %s ORDER BY submitted_at DESC LIMIT 12""", (aid,))
    for p in photos:
        p["status_label"] = label("asset_photo_status", p["status"])
        p["image_url"] = None if p["image_deleted"] else f"/api/assets/photo/{p['photo_id']}/image"
    current = pg.q("SELECT photo_id FROM atlas.asset_current_photo WHERE asset_id = %s", (aid,), one=True)
    sits = pg.q("""SELECT situation_id, title, severity, status, type, updated_at,
                          CASE WHEN payload::text LIKE %(like)s THEN 'names this asset' ELSE 'on its feeder' END how
                   FROM atlas.situation WHERE status <> 'closed' AND (payload::text LIKE %(like)s OR %(f)s = ANY(feeders))
                   ORDER BY updated_at DESC LIMIT 6""", {"like": f'%"{aid}"%', "f": a.get("feeder_id") or ""})
    spend = pq("""SELECT count(*) n, coalesce(sum(cost_aud), 0) total, max(created_date) last FROM gld_work_order WHERE asset_id = ANY(%(ids)s)""",
               {"ids": related_assets(aid)}, ttl=300)
    return dict(jobs=jobs, reports=rep_rows, photos=photos, current_photo_id=current and current["photo_id"], situations=sits,
                order_summary=spend[0] if spend else None)
